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

log = logging.getLogger("docktarr.arr_command_queue")


@dataclass(frozen=True)
class ArrCommandQueueConfig:
    """Tunables for the command-queue drainer.

    Defaults are deliberately conservative: 50+ stale ``unspecified``
    commands older than 10 minutes is well outside normal operation and
    should never trip on legitimate user activity.
    """

    enabled: bool = True
    poll_interval_seconds: int = 60
    drain_threshold_count: int = 50
    drain_age_seconds: int = 600
    drain_command_names: list[str] = field(
        default_factory=lambda: ["EpisodeSearch", "SeasonSearch", "MovieSearch"]
    )
    elevated_warn_count: int = 200


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
) -> list[ArrCommandQueueReport]:
    """One-shot tick of the command-queue drainer."""
    if not config.enabled:
        return []

    drain_names = set(config.drain_command_names)
    now = datetime.now(timezone.utc)
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

        should_drain = (
            len(drain_candidates) >= config.drain_threshold_count
            and oldest_drain_age is not None
            and oldest_drain_age >= config.drain_age_seconds
        )

        last_action = "ok"
        drained_count = 0
        sample_ids: list[int] = []

        if should_drain:
            ids_to_drain = [c["id"] for c in drain_candidates]
            sample_ids = ids_to_drain[:3]
            log.warning(
                "arr_command_queue[%s]: draining %d stale %s command(s) "
                "(trigger=unspecified, oldest %.0fs old)",
                service,
                len(ids_to_drain),
                "/".join(sorted(drain_names)),
                oldest_drain_age or 0.0,
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
                },
            )
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
                "drain_candidates=%d oldest=%s)",
                service,
                len(started),
                len(queued),
                len(drain_candidates),
                f"{oldest_age:.0f}s" if oldest_age else "n/a",
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
        )
        reports.append(report)

    if health_state is not None and hasattr(
        health_state, "record_arr_command_queue"
    ):
        health_state.record_arr_command_queue([r.to_dict() for r in reports])

    return reports
