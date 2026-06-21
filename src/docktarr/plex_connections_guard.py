"""Self-healing Plex server-discovery (``customConnections``) guard.

Born from the 2026-06-21 Plex migration incident. Plex was moved
Zion (10.0.0.16) -> Cypher (10.0.0.111), but Cypher's Plex still had
``customConnections = "http://10.0.0.16:32400"`` — Zion's OLD, now-dead IP —
pinned in its preferences. ``customConnections`` is the comma-separated list of
URLs Plex publishes to plex.tv for client discovery. So plex.tv kept advertising
the DEAD Zion address as the server's local connection; clients tried it and hung
("spinning"). The manual fix that worked:

    PUT {plex}/:/prefs?customConnections=http://10.0.0.111:32400&X-Plex-Token=...
    PUT {plex}/:/prefs?PublishServerOnPlexOnlineKey=0   # force...
    PUT {plex}/:/prefs?PublishServerOnPlexOnlineKey=1   # ...a re-publish

After that, plex.tv listed 10.0.0.111 and clients connected.

This module DETECTS and (by default) self-HEALS that drift. For each configured
endpoint it:

  1. Probes reachability via ``PlexClient.identity()``. Unreachable endpoints are
     skipped silently (debug log) — never a crash.
  2. For each reachable endpoint E (a live Plex server), reads its
     ``customConnections`` pref and splits it into URLs (comma-separated;
     whitespace trimmed — real data had a trailing-space entry).
  3. Probes each published URL for reachability.
  4. DRIFT = the live server publishes NO reachable customConnections URL (so
     clients can't discover a working address). On drift, if ``auto_fix``: PUT
     ``customConnections`` to E (replacing the stale/dead set), then toggle
     ``PublishServerOnPlexOnlineKey`` 0 -> 1 to force a re-publish, and emit
     ``plex_connections_guard.corrected``. Otherwise emit
     ``plex_connections_guard.stale`` (alert only, no change).

Conservatism / idempotency: we only REPLACE customConnections when the published
set contains NO reachable URL. If ANY published URL is already reachable (the
server is publishing a working address — possibly a legitimately-configured
remote URL), we do nothing: no PUT, no re-publish, no churn.

Every network/PUT call is timeout-bounded and wrapped — the check never raises
into the scheduler loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from docktarr.notifier import Notifier
from docktarr.plex_api import PlexClient

log = logging.getLogger(__name__)

_DEFAULT_ENDPOINTS = ["http://10.0.0.16:32400", "http://10.0.0.111:32400"]


@dataclass(frozen=True)
class PlexConnectionsGuardConfig:
    """Tunables for the Plex server-discovery (customConnections) guard."""

    enabled: bool = True
    endpoints: list[str] = field(default_factory=lambda: list(_DEFAULT_ENDPOINTS))
    auto_fix: bool = True
    token: str = ""


def _normalize(url: str) -> str:
    """Trim whitespace and a trailing slash so two spellings of one URL match."""
    return url.strip().rstrip("/")


async def _is_reachable(
    base_url: str, token: str, http: httpx.AsyncClient | None
) -> bool:
    """True if a Plex server answers ``/identity`` at ``base_url``."""
    client = PlexClient(base_url, token, http=http)
    try:
        identity = await client.identity()
    except Exception as exc:
        log.debug(
            "plex_connections_guard: %s not reachable: %s", base_url, exc
        )
        return False
    finally:
        if http is None:
            await client.close()
    # An empty identity (no MediaContainer) means it answered but isn't Plex —
    # treat a non-empty parse as "live".
    return bool(identity)


async def _check_endpoint(
    endpoint: str,
    config: PlexConnectionsGuardConfig,
    notifier: Notifier,
    http: httpx.AsyncClient | None,
) -> dict[str, Any] | None:
    """Check one reachable endpoint for discovery drift; heal if configured.

    Returns a per-endpoint status dict, or None if the endpoint was unreachable
    (and therefore skipped). Never raises.
    """
    endpoint_norm = _normalize(endpoint)

    # 1. Is this endpoint a live Plex server?
    if not await _is_reachable(endpoint, config.token, http):
        return None

    # 2. Read its published customConnections.
    client = PlexClient(endpoint, config.token, http=http)
    try:
        try:
            prefs = await client.get_preferences()
        except Exception as exc:
            log.debug(
                "plex_connections_guard: %s get_preferences failed: %s",
                endpoint,
                exc,
            )
            return {"endpoint": endpoint, "skipped": "prefs_unreadable"}

        raw = prefs.get("customConnections") or ""
        published = [_normalize(u) for u in raw.split(",") if _normalize(u)]

        # 3. Which published URLs are reachable right now?
        reachable_published: list[str] = []
        for url in published:
            if await _is_reachable(url, config.token, http):
                reachable_published.append(url)

        # 4. Drift = server is live but publishes NO usable address. If it already
        #    publishes at least one reachable URL, it's fine — leave it alone
        #    (idempotent; don't fight a legitimately-configured remote URL).
        if reachable_published:
            log.debug(
                "plex_connections_guard: %s OK — publishes reachable %s",
                endpoint,
                reachable_published,
            )
            return {
                "endpoint": endpoint,
                "drift": False,
                "published": published,
                "reachable_published": reachable_published,
            }

        # DRIFT: reachable at E, but none of its customConnections resolve.
        new_value = endpoint_norm
        log.warning(
            "plex_connections_guard: DRIFT — %s is live but publishes no "
            "reachable customConnections (published=%s). Expected to include %s.",
            endpoint,
            published or "<empty>",
            new_value,
        )

        if not config.auto_fix:
            await notifier.emit(
                "plex_connections_guard.stale",
                {
                    "endpoint": endpoint,
                    "old_customConnections": raw or "<empty>",
                    "expected": new_value,
                },
            )
            return {
                "endpoint": endpoint,
                "drift": True,
                "fixed": False,
                "published": published,
            }

        # 5. Auto-fix: replace customConnections with E, then force a re-publish.
        try:
            await client.set_preference("customConnections", new_value)
            await client.set_preference("PublishServerOnPlexOnlineKey", "0")
            await client.set_preference("PublishServerOnPlexOnlineKey", "1")
        except Exception as exc:
            log.warning(
                "plex_connections_guard: auto-fix of %s FAILED: %s",
                endpoint,
                exc,
            )
            return {
                "endpoint": endpoint,
                "drift": True,
                "fixed": False,
                "error": str(exc),
            }

        log.warning(
            "plex_connections_guard: CORRECTED %s — customConnections %r -> %r "
            "and forced re-publish",
            endpoint,
            raw,
            new_value,
        )
        await notifier.emit(
            "plex_connections_guard.corrected",
            {
                "endpoint": endpoint,
                "old_customConnections": raw or "<empty>",
                "new_customConnections": new_value,
            },
        )
        return {
            "endpoint": endpoint,
            "drift": True,
            "fixed": True,
            "old": raw,
            "new": new_value,
        }
    finally:
        if http is None:
            await client.close()


async def run_plex_connections_guard(
    config: PlexConnectionsGuardConfig,
    notifier: Notifier,
    *,
    http: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Single-shot Plex discovery-address guard.

    For each configured endpoint that is a live Plex server, ensure its
    published ``customConnections`` contains at least one reachable address;
    self-heal (or alert) on drift.

    Returns ``{"results": [<per-endpoint status>...]}``. Called by APScheduler on
    each tick. Never raises.
    """
    results: list[dict[str, Any]] = []
    for endpoint in config.endpoints:
        try:
            status = await _check_endpoint(endpoint, config, notifier, http)
        except Exception as exc:
            # Belt-and-suspenders: nothing inside _check_endpoint should raise,
            # but the scheduler loop must never see an exception.
            log.debug(
                "plex_connections_guard: unexpected error on %s: %s",
                endpoint,
                exc,
            )
            status = None
        if status is not None:
            results.append(status)

    return {"results": results}
