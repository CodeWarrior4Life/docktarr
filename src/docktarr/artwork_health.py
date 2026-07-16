"""Artwork-health watchdog for the *arr media stack.

Born from the 2026-07-15 incident: Plex/Jellyfin were missing episode and
series artwork because Sonarr's **"Kodi (XBMC) / Emby"** metadata consumer
(implementation ``XbmcMetadata``) had been *disabled*. With that consumer
off, Sonarr/Radarr never write ``poster.jpg`` / ``fanart.jpg`` / season art /
``*-thumb.jpg`` / ``.nfo`` sidecars to disk, so any media player that reads
on-disk artwork (Plex/Jellyfin/Kodi/Emby) shows blank posters. Re-enabling
the consumer fixes it; but nothing was watching to keep it enabled.

This module makes that class of failure non-recurring. On each scheduled
tick it performs two responsibilities:

(A) **Consumer-drift guard** (the root-cause fix). For each configured
    Sonarr/Radarr it GETs ``/api/v3/metadata``, finds the consumer whose
    ``implementation`` is ``XbmcMetadata`` ("Kodi (XBMC) / Emby"), and:

      * if ``enable`` is ``False`` → emit ``artwork_health.consumer_disabled``
        (names the service) AND, when ``auto_heal`` is on (default), GET the
        full consumer object, flip ``enable=true``, PUT it back to
        ``/api/v3/metadata/{id}`` preserving every other field, then emit
        ``artwork_health.consumer_reenabled``.
      * optionally (``check_image_fields``, default off) verify the image
        sub-fields (``seriesImages`` / ``seasonImages`` / ``episodeImages``
        for Sonarr; ``movieImages`` for Radarr) are truthy, and emit
        ``artwork_health.consumer_images_disabled`` if not (alert-only —
        auto-heal only touches the top-level ``enable``, per spec).

(B) **Artwork presence spot-check** (alert-only). For the N most-recently
    added series/movies (``presence_sample_size``, default 10), read each
    item's ``path`` from the arr API and, via ``docker exec`` into the arr
    container (same mechanism :mod:`docktarr.mount_audit` uses — no host
    mount assumed), confirm the expected root artwork (``poster.jpg`` /
    ``fanart.jpg``) is present on disk. Emits ``artwork_health.artwork_missing``
    with a count + sample when any item is missing artwork. NO auto-heal for
    presence in v1; when ``presence_auto_refresh`` is on (default off) a
    ``RefreshSeries`` / ``RefreshMovie`` command is triggered for the missing
    items so the consumer re-writes the sidecars. The presence check is
    skipped cleanly when no ``DockerManager`` is available.

Alerts are **deduped**: a per-service/per-signal counter tracks consecutive
breaching ticks; the alert fires once when the counter crosses ``debounce``
(default 1 → alert on first detection) and is suppressed while the condition
persists, re-arming only after it clears. Auto-heal actions
(``consumer_reenabled``) always fire when they happen.

Every network/docker call is timeout-bounded and wrapped — an unreachable
service degrades to an ``artwork_health.error`` event, never a crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from docktarr.arrclient import ArrClient
    from docktarr.docker_manager import DockerManager
    from docktarr.http_health import HealthState
    from docktarr.notifier import Notifier

log = logging.getLogger("docktarr.artwork_health")

# The metadata consumer we care about. Sonarr AND Radarr both expose the
# "Kodi (XBMC) / Emby" consumer under this implementation name.
XBMC_IMPLEMENTATION = "XbmcMetadata"

# Per-service image toggle field names inside the consumer's ``fields`` array.
_IMAGE_FIELDS = {
    "Sonarr": ("seriesImages", "seasonImages", "episodeImages"),
    "Radarr": ("movieImages",),
}

# Root-folder artwork files XbmcMetadata writes for each item, checked on disk.
_EXPECTED_ARTWORK = ("poster.jpg", "fanart.jpg")


@dataclass(frozen=True)
class ArtworkHealthConfig:
    """Tunables for the artwork-health watchdog."""

    enabled: bool = False
    # (A) consumer-drift guard
    auto_heal: bool = True
    check_image_fields: bool = False
    # (B) presence spot-check
    presence_check: bool = True
    presence_sample_size: int = 10
    presence_auto_refresh: bool = False
    # alert dedup: fire once the condition has held for this many ticks,
    # then suppress until it clears.
    debounce: int = 1


@dataclass
class ArtworkHealthState:
    """Mutable per-instance state shared across scheduler ticks.

    Keyed by ``"{service}:{signal}"`` (e.g. ``"Sonarr:consumer_disabled"``)
    it counts consecutive breaching ticks so alerts can be deduped.
    """

    breach_ticks: dict[str, int] = field(default_factory=dict)
    debounce: int = 1

    def note(self, key: str, breaching: bool) -> bool:
        """Advance the counter for ``key`` and return True iff an alert should
        fire on THIS tick (i.e. the counter just crossed the debounce
        threshold). Non-breaching ticks reset the counter; while the condition
        persists past the threshold the alert is suppressed (deduped)."""
        if not breaching:
            self.breach_ticks.pop(key, None)
            return False
        count = self.breach_ticks.get(key, 0) + 1
        self.breach_ticks[key] = count
        return count == max(1, self.debounce)


@dataclass
class ServiceArtworkReport:
    service: str
    consumer_found: bool = False
    consumer_enabled: bool | None = None
    consumer_reenabled: bool = False
    image_fields_ok: bool | None = None
    presence_checked: int = 0
    presence_missing: int = 0
    missing_samples: list[str] = field(default_factory=list)
    refresh_triggered: int = 0
    last_action: str = "ok"  # "ok" | "alerted" | "healed" | "error"
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "consumer_found": self.consumer_found,
            "consumer_enabled": self.consumer_enabled,
            "consumer_reenabled": self.consumer_reenabled,
            "image_fields_ok": self.image_fields_ok,
            "presence_checked": self.presence_checked,
            "presence_missing": self.presence_missing,
            "missing_samples": self.missing_samples,
            "refresh_triggered": self.refresh_triggered,
            "last_action": self.last_action,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Metadata-consumer helpers (API)
# ---------------------------------------------------------------------------


def _find_xbmc_consumer(consumers: list[dict]) -> dict | None:
    for c in consumers:
        if c.get("implementation") == XBMC_IMPLEMENTATION:
            return c
    return None


def _image_fields_ok(consumer: dict, service: str) -> bool | None:
    """Return True if all expected image toggles are truthy, False if any is
    off, None if the fields aren't present to evaluate."""
    names = _IMAGE_FIELDS.get(service)
    if not names:
        return None
    fields = {f.get("name"): f.get("value") for f in consumer.get("fields") or []}
    seen = [n for n in names if n in fields]
    if not seen:
        return None
    return all(bool(fields.get(n)) for n in seen)


async def _get_metadata_consumers(client: "ArrClient") -> list[dict]:
    v = client._api_version()
    resp = await client._client.get(
        f"{client._url}/api/{v}/metadata",
        headers=client._headers(),
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else data.get("records", [])


async def _put_metadata_consumer(client: "ArrClient", consumer: dict) -> None:
    """PUT the (mutated) full consumer object back, preserving all fields."""
    v = client._api_version()
    cid = consumer.get("id")
    resp = await client._client.put(
        f"{client._url}/api/{v}/metadata/{cid}",
        json=consumer,
        headers=client._headers(),
        timeout=15.0,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Presence spot-check helpers (arr API + docker exec)
# ---------------------------------------------------------------------------


async def _recent_items(client: "ArrClient", limit: int) -> list[dict]:
    """Return the N most-recently-added items (series for Sonarr, movies for
    Radarr) as raw arr objects, sorted by ``added`` descending."""
    v = client._api_version()
    endpoint = "series" if client.name == "Sonarr" else "movie"
    resp = await client._client.get(
        f"{client._url}/api/{v}/{endpoint}",
        headers=client._headers(),
        timeout=30.0,
    )
    resp.raise_for_status()
    items = resp.json()
    if not isinstance(items, list):
        return []
    items.sort(key=lambda it: it.get("added") or "", reverse=True)
    return items[:limit]


async def _artwork_present(dm: "DockerManager", container: str, item_path: str) -> bool:
    """True iff every expected root artwork file exists inside the container."""
    for fname in _EXPECTED_ARTWORK:
        target = f"{item_path.rstrip('/')}/{fname}"
        code, _ = await dm.exec_run(container, ["test", "-f", target])
        if code != 0:
            return False
    return True


async def _trigger_refresh(client: "ArrClient", item_ids: list[int]) -> None:
    v = client._api_version()
    if client.name == "Sonarr":
        body = {"name": "RefreshSeries", "seriesIds": item_ids}
    else:
        body = {"name": "RefreshMovie", "movieIds": item_ids}
    resp = await client._client.post(
        f"{client._url}/api/{v}/command",
        json=body,
        headers=client._headers(),
        timeout=15.0,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Per-service check
# ---------------------------------------------------------------------------


async def _check_service(
    *,
    client: "ArrClient",
    config: ArtworkHealthConfig,
    notifier: "Notifier",
    state: ArtworkHealthState,
    docker_manager: "DockerManager | None",
) -> ServiceArtworkReport:
    service = client.name
    report = ServiceArtworkReport(service=service)

    # --- (A) Consumer-drift guard --------------------------------------
    try:
        consumers = await _get_metadata_consumers(client)
    except Exception as exc:
        log.error("artwork_health[%s]: GET /metadata failed: %s", service, exc)
        report.last_action = "error"
        report.error = str(exc)
        await notifier.emit(
            "artwork_health.error",
            {"service": service, "error": str(exc)},
        )
        return report

    consumer = _find_xbmc_consumer(consumers)
    if consumer is None:
        # No XbmcMetadata consumer at all — cannot heal (nothing to toggle).
        # Treat as disabled-class drift so the operator is alerted.
        log.warning(
            "artwork_health[%s]: no %s metadata consumer present",
            service,
            XBMC_IMPLEMENTATION,
        )
        report.consumer_found = False
        report.consumer_enabled = False
        if state.note(f"{service}:consumer_disabled", True):
            report.last_action = "alerted"
            await notifier.emit(
                "artwork_health.consumer_disabled",
                {"service": service, "detail": "no XbmcMetadata consumer present"},
            )
        return report

    report.consumer_found = True
    enabled = bool(consumer.get("enable"))
    report.consumer_enabled = enabled

    if not enabled:
        # Fire the drift alert (deduped) naming the service.
        if state.note(f"{service}:consumer_disabled", True):
            report.last_action = "alerted"
            log.warning(
                "artwork_health[%s]: Kodi (XBMC) / Emby metadata consumer is "
                "DISABLED — Plex/Jellyfin artwork will not be written to disk.",
                service,
            )
            await notifier.emit(
                "artwork_health.consumer_disabled",
                {"service": service, "detail": "enable=false"},
            )
        if config.auto_heal:
            try:
                consumer["enable"] = True
                await _put_metadata_consumer(client, consumer)
                report.consumer_reenabled = True
                report.consumer_enabled = True
                report.last_action = "healed"
                # condition healed → re-arm the dedup counter
                state.note(f"{service}:consumer_disabled", False)
                log.info(
                    "artwork_health[%s]: re-enabled Kodi (XBMC) / Emby consumer",
                    service,
                )
                await notifier.emit(
                    "artwork_health.consumer_reenabled",
                    {"service": service},
                )
            except Exception as exc:
                log.error(
                    "artwork_health[%s]: failed to re-enable consumer: %s",
                    service,
                    exc,
                )
                report.error = str(exc)
                await notifier.emit(
                    "artwork_health.error",
                    {"service": service, "error": f"re-enable failed: {exc}"},
                )
    else:
        # Enabled → clear any prior disabled-alert dedup state.
        state.note(f"{service}:consumer_disabled", False)

    # --- (A') advanced image-field check (alert-only) ------------------
    if config.check_image_fields and report.consumer_enabled:
        fields_ok = _image_fields_ok(consumer, service)
        report.image_fields_ok = fields_ok
        if fields_ok is False:
            if state.note(f"{service}:images_disabled", True):
                if report.last_action == "ok":
                    report.last_action = "alerted"
                await notifier.emit(
                    "artwork_health.consumer_images_disabled",
                    {"service": service},
                )
        else:
            state.note(f"{service}:images_disabled", False)

    # --- (B) presence spot-check (alert-only) --------------------------
    if config.presence_check and docker_manager is not None:
        container = client.container_name
        try:
            items = await _recent_items(client, config.presence_sample_size)
        except Exception as exc:
            log.warning(
                "artwork_health[%s]: presence spot-check list failed: %s",
                service,
                exc,
            )
            items = []

        missing_ids: list[int] = []
        for item in items:
            path = item.get("path")
            if not path:
                continue
            report.presence_checked += 1
            try:
                present = await _artwork_present(docker_manager, container, path)
            except Exception as exc:
                log.debug(
                    "artwork_health[%s]: docker exec probe failed for %s: %s",
                    service,
                    path,
                    exc,
                )
                continue
            if not present:
                title = item.get("title", path)
                report.presence_missing += 1
                if len(report.missing_samples) < 5:
                    report.missing_samples.append(str(title))
                if item.get("id") is not None:
                    missing_ids.append(int(item["id"]))

        if report.presence_missing:
            if state.note(f"{service}:artwork_missing", True):
                if report.last_action == "ok":
                    report.last_action = "alerted"
                log.warning(
                    "artwork_health[%s]: %d/%d recent items missing artwork on "
                    "disk (sample: %s)",
                    service,
                    report.presence_missing,
                    report.presence_checked,
                    ", ".join(report.missing_samples),
                )
                await notifier.emit(
                    "artwork_health.artwork_missing",
                    {
                        "service": service,
                        "missing": report.presence_missing,
                        "checked": report.presence_checked,
                        "samples": ", ".join(report.missing_samples) or "(none)",
                    },
                )
            if config.presence_auto_refresh and missing_ids:
                try:
                    await _trigger_refresh(client, missing_ids)
                    report.refresh_triggered = len(missing_ids)
                    log.info(
                        "artwork_health[%s]: triggered refresh for %d item(s)",
                        service,
                        len(missing_ids),
                    )
                except Exception as exc:
                    log.warning(
                        "artwork_health[%s]: refresh trigger failed: %s",
                        service,
                        exc,
                    )
        else:
            state.note(f"{service}:artwork_missing", False)

    return report


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def run_artwork_health(
    *,
    arr_clients: list["ArrClient"],
    config: ArtworkHealthConfig,
    notifier: "Notifier",
    state: ArtworkHealthState | None = None,
    docker_manager: "DockerManager | None" = None,
    health_state: "HealthState | None" = None,
) -> list[ServiceArtworkReport]:
    """Single-shot tick of the artwork-health watchdog. Never raises."""
    if not config.enabled:
        return []
    if state is None:
        state = ArtworkHealthState()
    # keep the state's debounce in sync with config
    state.debounce = config.debounce

    reports: list[ServiceArtworkReport] = []
    for client in arr_clients:
        if getattr(client, "name", None) not in ("Sonarr", "Radarr"):
            continue
        report = await _check_service(
            client=client,
            config=config,
            notifier=notifier,
            state=state,
            docker_manager=docker_manager,
        )
        reports.append(report)

    if health_state is not None and hasattr(health_state, "record_artwork_health"):
        health_state.record_artwork_health([r.to_dict() for r in reports])

    return reports
