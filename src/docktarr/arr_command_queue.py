"""ARR command queue manager for Docktarr.

Watches Sonarr / Radarr ``/api/v3/command`` queues and auto-drains runaway
external-API search batches before they wedge the scheduler.

Motivation
----------
Sonarr's task scheduler is a single FIFO queue. A burst of API-posted
``EpisodeSearch`` commands without a ``trigger`` field (which Sonarr
materialises as ``trigger=unspecified``) sits on top of internal heartbeats
like ``RssSync`` and ``ImportListSync`` and starves them indefinitely.

Real-world wedge: 891 ``EpisodeSearch`` commands queued in a 24-second burst
left the scheduler stuck for 15 hours until they were manually drained via
``DELETE /api/v3/command/{id}``.

Discriminator
-------------
For each ARR service we partition ``/api/v3/command`` results by status and
the ``trigger`` field:

- ``trigger='manual'``   — user clicked Sonarr's Search button → **preserve**.
- ``trigger='scheduled'``— Sonarr's own internal heartbeat → **preserve**.
- ``trigger='unspecified'`` — bare API POST with no ``trigger`` set. This is
  the runaway pattern. Drain if it exceeds count + age thresholds.

Only commands in ``config.drain_command_names`` are eligible; default list
covers the search families (``EpisodeSearch``, ``SeasonSearch``,
``MovieSearch``). ``RssSync`` and ``ImportListSync`` are never touched, even
if they happen to share the ``unspecified`` trigger.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docktarr.arrclient import ArrClient
    from docktarr.http_health import HealthState
    from docktarr.notifier import Notifier
    from docktarr.state import StateStore

log = logging.getLogger("docktarr.arr_command_queue")


@dataclass(frozen=True)
class ArrCommandQueueConfig:
    """Tunables for the command-queue drainer.

    Defaults are tuned to catch a burst inside a single Sonarr
    scheduled-task cycle (5 min minimum). The 2026-05-13 wedge ran for
    *15 hours* because the prior defaults (60s poll, 50/600s gate) only
    fired well after the damage was done. We now poll every 15s, drain at
    30 candidates / 120s, and have a burst-rate side gate that triggers
    immediately on classic floods (e.g. 891 commands in 24s = 37/sec).
    Scheduler-liveness (see :mod:`docktarr.arr_scheduler_health`) is the
    primary signal and overrides the count/age gate entirely.
    """

    enabled: bool = True
    poll_interval_seconds: int = 15
    drain_threshold_count: int = 30
    drain_age_seconds: int = 120
    drain_command_names: list[str] = field(
        default_factory=lambda: ["EpisodeSearch", "SeasonSearch", "MovieSearch"]
    )
    elevated_warn_count: int = 200
    # --- burst detector (0.7.2) -----------------------------------------
    # If the count of drain-candidates jumped by at least ``burst_threshold``
    # in a single poll, AND the per-second rate of that delta exceeds
    # ``burst_rate_threshold``, drain immediately regardless of the age
    # gate. Rationale: 891 commands in 24s = 37/sec — that pattern is
    # unmistakable and slow defaults shouldn't let it pile up further.
    burst_threshold: int = 20
    burst_rate_threshold: float = 0.5  # candidates/sec


@dataclass
class ArrCommandQueueReport:
    service: str  # "Sonarr" / "Radarr" / etc.
    queued_count: int
    started_count: int
    oldest_queued_age_seconds: float | None
    drained_count: int
    last_action: str  # "ok" | "drained" | "elevated" | "error"
    error: str | None = None
    sample_drained_ids: list[int] = field(default_factory=list)
    # 0.7.2: burst-detector telemetry. ``delta`` is the change in
    # drain-candidate count since the last tick; ``delta_rate`` is that
    # delta divided by the elapsed seconds; ``triggered_by`` records the
    # discriminator that fired ("age" | "burst" | "forced" | "" if no
    # drain).
    delta: int = 0
    delta_rate: float = 0.0
    triggered_by: str = ""

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "queued_count": self.queued_count,
            "started_count": self.started_count,
            "oldest_queued_age_seconds": self.oldest_queued_age_seconds,
            "drained_count": self.drained_count,
            "last_action": self.last_action,
            "error": self.error,
            "sample_drained_ids": list(self.sample_drained_ids),
            "delta": self.delta,
            "delta_rate": self.delta_rate,
            "triggered_by": self.triggered_by,
        }


_DRAIN_BATCH_SIZE = 16  # parallel DELETEs per asyncio.gather batch


def _parse_queued_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    # Sonarr/Radarr return ISO 8601 with a trailing 'Z' for UTC. Python's
    # fromisoformat accepts 'Z' from 3.11+.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.debug("arr_command_queue: cannot parse timestamp %r", value)
        return None


def _classify(
    commands: list[dict],
    *,
    drain_names: set[str],
    now: datetime,
) -> tuple[list[dict], list[dict], list[dict], float | None]:
    """Partition the command list.

    Returns
    -------
    started, queued, drain_candidates, oldest_queued_age_seconds
    """
    started: list[dict] = []
    queued: list[dict] = []
    drain_candidates: list[dict] = []
    oldest_age: float | None = None

    for cmd in commands:
        status = cmd.get("status")
        if status == "started":
            started.append(cmd)
            continue
        if status != "queued":
            # In-flight statuses we don't care about (completed/failed/aborted
            # generally aren't returned anyway, but be defensive).
            continue

        queued.append(cmd)
        queued_dt = _parse_queued_iso(cmd.get("queued"))
        age_seconds: float | None = None
        if queued_dt is not None:
            age_seconds = max(0.0, (now - queued_dt).total_seconds())
            if oldest_age is None or age_seconds > oldest_age:
                oldest_age = age_seconds

        trigger = cmd.get("trigger")
        name = cmd.get("name")
        if (
            trigger == "unspecified"
            and name in drain_names
            and age_seconds is not None
        ):
            drain_candidates.append({**cmd, "_age_seconds": age_seconds})

    # Sort drain candidates oldest-first so the "oldest age" check on the
    # head of the list matches operator intuition.
    drain_candidates.sort(key=lambda c: c["_age_seconds"], reverse=True)
    return started, queued, drain_candidates, oldest_age


async def _drain(
    client: "ArrClient", ids: list[int]
) -> tuple[int, list[tuple[int, str]]]:
    """Delete ``ids`` in batches of 16 in parallel. Returns (succeeded, errors)."""
    succeeded = 0
    errors: list[tuple[int, str]] = []
    for start in range(0, len(ids), _DRAIN_BATCH_SIZE):
        batch = ids[start : start + _DRAIN_BATCH_SIZE]
        results = await asyncio.gather(
            *(client.delete_command(cid) for cid in batch), return_exceptions=True
        )
        for cid, res in zip(batch, results):
            if isinstance(res, Exception):
                errors.append((cid, str(res)))
            else:
                succeeded += 1
    return succeeded, errors


async def run_arr_command_queue(
    *,
    arr_clients: list["ArrClient"],
    config: ArrCommandQueueConfig,
    notifier: "Notifier",
    health_state: "HealthState | None" = None,
    state_store: "StateStore | None" = None,
    force_drain_services: set[str] | None = None,
) -> list[ArrCommandQueueReport]:
    """One-shot tick of the command-queue drainer.

    Parameters
    ----------
    force_drain_services
        Services for which the count/age gate is overridden. Used by
        :mod:`docktarr.arr_scheduler_health` to drain immediately on any
        wedge signal even if the queue isn't yet large/old enough.
    state_store
        Optional :class:`docktarr.state.StateStore`. When provided, the
        module persists per-service drain-candidate counts (timestamped)
        so the burst detector survives docktarr restarts.
    """
    if not config.enabled:
        return []

    drain_names = set(config.drain_command_names)
    now = datetime.now(timezone.utc)
    force = force_drain_services or set()
    reports: list[ArrCommandQueueReport] = []

    for client in arr_clients:
        service = getattr(client, "name", "unknown")
        try:
            commands = await client.list_commands()
        except Exception as exc:
            log.error(
                "arr_command_queue[%s]: GET /api/v3/command failed: %s", service, exc
            )
            report = ArrCommandQueueReport(
                service=service,
                queued_count=0,
                started_count=0,
                oldest_queued_age_seconds=None,
                drained_count=0,
                last_action="error",
                error=str(exc),
            )
            reports.append(report)
            await notifier.emit(
                "arr_command_queue.error",
                {"service": service, "error": str(exc)},
            )
            continue

        started, queued, drain_candidates, oldest_age = _classify(
            commands, drain_names=drain_names, now=now
        )

        oldest_drain_age = (
            drain_candidates[0]["_age_seconds"] if drain_candidates else None
        )

        # --- burst detector (0.7.2) -------------------------------------
        # Compare drain-candidate count to the previous tick's count.
        # delta_rate uses the elapsed wall time between ticks (NOT the
        # configured poll_interval, which is the *target* cadence — actual
        # cadence drifts) so the rate reflects reality even after
        # docktarr restarts.
        #
        # Cold-start semantics: if we have no prior baseline (no state
        # store, or the store is empty for this service), the burst
        # detector stays silent. Rationale: the first tick after startup
        # observes the FULL queue depth as a "delta from zero", which is
        # not a burst — it's just an initial reading. The next tick will
        # have a real baseline to compare against. We still record the
        # baseline so the second tick can detect a fresh burst.
        prev = (
            _load_previous_burst_state(state_store, service)
            if state_store is not None
            else None
        )
        if prev and prev[1] is not None:
            delta = len(drain_candidates) - prev[0]
            elapsed = max(1e-6, (now - prev[1]).total_seconds())
            delta_rate = delta / elapsed if elapsed > 0 else 0.0
            burst_detected = (
                delta >= config.burst_threshold
                and delta_rate >= config.burst_rate_threshold
            )
        else:
            delta = 0
            delta_rate = 0.0
            burst_detected = False

        # Persist new count + timestamp for next tick (always, regardless
        # of action — we want the rolling window to keep flowing).
        if state_store is not None:
            _save_previous_burst_state(
                state_store, service, len(drain_candidates), now
            )

        age_drain = (
            len(drain_candidates) >= config.drain_threshold_count
            and oldest_drain_age is not None
            and oldest_drain_age >= config.drain_age_seconds
        )
        forced = service in force and len(drain_candidates) > 0

        should_drain = age_drain or burst_detected or forced
        triggered_by = (
            "forced" if forced else ("burst" if burst_detected else
            ("age" if age_drain else ""))
        )

        last_action = "ok"
        drained_count = 0
        sample_ids: list[int] = []

        # Burst event fires BEFORE the drained event so operators see
        # "burst → drain" causality in the log/Telegram stream.
        if burst_detected:
            await notifier.emit(
                "arr_command_queue.burst_detected",
                {
                    "service": service,
                    "delta": delta,
                    "delta_rate": round(delta_rate, 2),
                    "current_count": len(drain_candidates),
                },
            )
            log.warning(
                "arr_command_queue[%s]: burst detected (+%d in %.1fs = "
                "%.2f/sec). Draining immediately.",
                service,
                delta,
                elapsed,
                delta_rate,
            )

        if should_drain:
            ids_to_drain = [c["id"] for c in drain_candidates]
            sample_ids = ids_to_drain[:3]
            log.warning(
                "arr_command_queue[%s]: draining %d stale %s command(s) "
                "(trigger=unspecified, oldest %.0fs old, triggered_by=%s)",
                service,
                len(ids_to_drain),
                "/".join(sorted(drain_names)),
                oldest_drain_age or 0.0,
                triggered_by,
            )
            drained_count, errors = await _drain(client, ids_to_drain)
            if errors:
                log.warning(
                    "arr_command_queue[%s]: %d DELETE(s) failed during drain "
                    "(succeeded=%d). First error: id=%d %s",
                    service,
                    len(errors),
                    drained_count,
                    errors[0][0],
                    errors[0][1],
                )
            last_action = "drained"
            await notifier.emit(
                "arr_command_queue.drained",
                {
                    "service": service,
                    "count": drained_count,
                    "sample_ids": sample_ids,
                    "oldest_age_seconds": int(oldest_drain_age or 0),
                    "triggered_by": triggered_by,
                },
            )
            # After a drain the candidate count drops to ~0; reset the
            # baseline so the next tick's delta isn't a huge negative
            # number that masks the next burst.
            if state_store is not None:
                _save_previous_burst_state(state_store, service, 0, now)
        elif (
            len(queued) > config.elevated_warn_count
            or len(drain_candidates) > config.elevated_warn_count
        ):
            last_action = "elevated"
            log.warning(
                "arr_command_queue[%s]: elevated queue depth (queued=%d, "
                "drain_candidates=%d, oldest_age=%s) — thresholds not met, "
                "warn-only",
                service,
                len(queued),
                len(drain_candidates),
                f"{oldest_age:.0f}s" if oldest_age else "n/a",
            )
            await notifier.emit(
                "arr_command_queue.elevated",
                {
                    "service": service,
                    "count": len(queued),
                    "oldest_age_seconds": int(oldest_age or 0),
                },
            )
        else:
            log.debug(
                "arr_command_queue[%s]: ok (started=%d queued=%d "
                "drain_candidates=%d oldest=%s delta=%+d rate=%.2f/s)",
                service,
                len(started),
                len(queued),
                len(drain_candidates),
                f"{oldest_age:.0f}s" if oldest_age else "n/a",
                delta,
                delta_rate,
            )

        report = ArrCommandQueueReport(
            service=service,
            queued_count=len(queued),
            started_count=len(started),
            oldest_queued_age_seconds=oldest_age,
            drained_count=drained_count,
            last_action=last_action,
            error=None,
            sample_drained_ids=sample_ids,
            delta=delta,
            delta_rate=round(delta_rate, 4),
            triggered_by=triggered_by,
        )
        reports.append(report)

    if health_state is not None and hasattr(
        health_state, "record_arr_command_queue"
    ):
        health_state.record_arr_command_queue([r.to_dict() for r in reports])

    return reports


# ---------------------------------------------------------------------------
# StateStore persistence for burst detector
# ---------------------------------------------------------------------------
#
# The burst detector needs to know the previous tick's drain-candidate
# count, keyed by service, and that count must survive docktarr restarts
# (otherwise a restart during an ongoing burst would reset the baseline
# to 0 and the detector would mis-fire on the recovered count). We piggy-
# back on the existing :class:`docktarr.state.StateStore` JSON file using
# a private attribute namespace (``_arr_burst``); IndexerState's serialiser
# stays untouched.


def _load_previous_burst_state(
    store: "StateStore | None", service: str
) -> tuple[int, datetime | None] | None:
    if store is None:
        return None
    bucket = getattr(store, "_arr_burst", None)
    if not bucket:
        return None
    entry = bucket.get(service)
    if not entry:
        return None
    ts = entry.get("timestamp")
    ts_dt = None
    if ts:
        try:
            ts_dt = datetime.fromisoformat(ts)
        except ValueError:
            ts_dt = None
    return int(entry.get("count", 0)), ts_dt


def _save_previous_burst_state(
    store: "StateStore | None", service: str, count: int, when: datetime
) -> None:
    if store is None:
        return
    bucket = getattr(store, "_arr_burst", None)
    if bucket is None:
        bucket = {}
        store._arr_burst = bucket  # type: ignore[attr-defined]
    bucket[service] = {"count": count, "timestamp": when.isoformat()}
