"""ARR scheduler liveness probe for Docktarr.

Watches Sonarr / Radarr ``/api/v3/system/task`` and flags wedged
short-interval tasks as the **primary** signal that the scheduler is
starved.

Why this exists
---------------
The companion :mod:`docktarr.arr_command_queue` module reads the
*symptom* of a scheduler wedge: a wall of stale ``trigger=unspecified``
``EpisodeSearch`` commands. By the time that signal trips, the damage
(missed RssSync / ImportListSync cycles, etc.) is already done.

The disease is faster to detect: an internal scheduled task whose
``lastExecution`` age has exceeded its declared ``interval`` by a wide
margin (e.g. RssSync hasn't fired in 56 cycles). That signal arrives as
the wedge *starts*, not after.

We probe each Sonarr/Radarr at the same cadence as the queue drainer and
flag any *critical* task whose ``overdue_ratio = age_seconds /
(interval_minutes * 60)`` exceeds ``wedge_threshold`` (default 3.0,
i.e. tripled normal interval).

Critical tasks are short-interval ones whose wedging signals real
damage. Longer tasks (``Backup``, ``Refresh Series``) have hour-to-day
intervals and naturally show high overdue ratios outside their windows;
they're filtered out by an explicit allowlist.

When any critical task is wedged we:

1. Emit ``arr_scheduler.wedged`` (warn) with the offending names + ratios.
2. **Trigger an immediate command-queue drain** via the queue module's
   ``force_drain`` parameter, even if count/age thresholds aren't met.
   The wedge itself is the symptom that overrides the count-based gate.

Composition with ``arr_command_queue``
--------------------------------------
The two probes are designed to compose on the same tick:

- :func:`run_arr_scheduler_health` returns a list of
  :class:`ArrSchedulerHealthReport` plus a set of services that need a
  forced drain.
- ``main.py`` calls this first, then passes ``force_drain_services`` into
  ``run_arr_command_queue`` so both signals correlate within a single
  poll cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docktarr.arrclient import ArrClient
    from docktarr.http_health import HealthState
    from docktarr.notifier import Notifier

log = logging.getLogger("docktarr.arr_scheduler_health")


# Default critical-task allowlist. These are the short-interval scheduled
# tasks whose wedging means the scheduler is starved. Longer tasks
# (Backup, Refresh Series) intentionally excluded — their long intervals
# make overdue_ratio meaningless during off-cycle windows.
_DEFAULT_CRITICAL_TASKS = (
    "Rss Sync",
    "Import List Sync",
    "Refresh Monitored Downloads",
    "Messaging Cleanup",
)


@dataclass(frozen=True)
class ArrSchedulerHealthConfig:
    enabled: bool = True
    wedge_threshold: float = 3.0
    critical_tasks: list[str] = field(
        default_factory=lambda: list(_DEFAULT_CRITICAL_TASKS)
    )


@dataclass
class ArrSchedulerTaskReport:
    name: str
    interval_minutes: int
    last_execution_age_seconds: float
    overdue_ratio: float
    is_wedged: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "interval_minutes": self.interval_minutes,
            "last_execution_age_seconds": self.last_execution_age_seconds,
            "overdue_ratio": self.overdue_ratio,
            "is_wedged": self.is_wedged,
        }


@dataclass
class ArrSchedulerHealthReport:
    service: str
    tasks: list[ArrSchedulerTaskReport] = field(default_factory=list)
    wedged_count: int = 0
    last_action: str = "ok"  # "ok" | "alerted" | "error"
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "tasks": [t.to_dict() for t in self.tasks],
            "wedged_count": self.wedged_count,
            "last_action": self.last_action,
            "error": self.error,
        }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.debug("arr_scheduler_health: cannot parse timestamp %r", value)
        return None


def _build_task_report(
    task: dict, *, now: datetime, wedge_threshold: float
) -> ArrSchedulerTaskReport | None:
    """Build a per-task report. Returns None if the task can't be evaluated
    (missing interval / lastExecution)."""
    name = task.get("name")
    interval = task.get("interval")
    last_exec_raw = task.get("lastExecution")
    if not name or not isinstance(interval, (int, float)) or interval <= 0:
        return None
    last_exec = _parse_iso(last_exec_raw)
    if last_exec is None:
        return None
    age_seconds = max(0.0, (now - last_exec).total_seconds())
    interval_seconds = float(interval) * 60.0
    overdue_ratio = age_seconds / interval_seconds if interval_seconds > 0 else 0.0
    return ArrSchedulerTaskReport(
        name=str(name),
        interval_minutes=int(interval),
        last_execution_age_seconds=age_seconds,
        overdue_ratio=overdue_ratio,
        is_wedged=overdue_ratio >= wedge_threshold,
    )


async def run_arr_scheduler_health(
    *,
    arr_clients: list["ArrClient"],
    config: ArrSchedulerHealthConfig,
    notifier: "Notifier",
    health_state: "HealthState | None" = None,
) -> tuple[list[ArrSchedulerHealthReport], set[str]]:
    """One-shot tick of the scheduler liveness probe.

    Returns ``(reports, force_drain_services)`` so the caller can wire the
    second value into :func:`docktarr.arr_command_queue.run_arr_command_queue`
    to override its count/age gate on wedged services.
    """
    if not config.enabled:
        return [], set()

    now = datetime.now(timezone.utc)
    critical = set(config.critical_tasks)
    reports: list[ArrSchedulerHealthReport] = []
    force_drain_services: set[str] = set()

    for client in arr_clients:
        service = getattr(client, "name", "unknown")
        try:
            tasks_raw = await client.list_scheduled_tasks()
        except Exception as exc:
            log.error(
                "arr_scheduler_health[%s]: GET /api/v3/system/task failed: %s",
                service,
                exc,
            )
            reports.append(
                ArrSchedulerHealthReport(
                    service=service,
                    tasks=[],
                    wedged_count=0,
                    last_action="error",
                    error=str(exc),
                )
            )
            await notifier.emit(
                "arr_scheduler.error",
                {"service": service, "error": str(exc)},
            )
            continue

        task_reports: list[ArrSchedulerTaskReport] = []
        wedged: list[ArrSchedulerTaskReport] = []
        for t in tasks_raw:
            tr = _build_task_report(
                t, now=now, wedge_threshold=config.wedge_threshold
            )
            if tr is None:
                continue
            task_reports.append(tr)
            # Wedge gating: only count critical (short-interval) tasks. The
            # long-interval ones (Backup, Refresh Series) trip overdue
            # ratios off-cycle by design.
            if tr.is_wedged and tr.name in critical:
                wedged.append(tr)

        last_action = "ok"
        if wedged:
            last_action = "alerted"
            force_drain_services.add(service)
            offenders = ", ".join(
                f"{t.name} {t.overdue_ratio:.0f}x overdue" for t in wedged
            )
            log.warning(
                "arr_scheduler_health[%s]: wedged tasks (%s). "
                "Triggering forced command-queue drain.",
                service,
                offenders,
            )
            await notifier.emit(
                "arr_scheduler.wedged",
                {
                    "service": service,
                    "offenders": offenders,
                    "wedged_count": len(wedged),
                },
            )

        reports.append(
            ArrSchedulerHealthReport(
                service=service,
                tasks=task_reports,
                wedged_count=len(wedged),
                last_action=last_action,
                error=None,
            )
        )

    if health_state is not None and hasattr(
        health_state, "record_arr_scheduler_health"
    ):
        health_state.record_arr_scheduler_health([r.to_dict() for r in reports])

    return reports, force_drain_services
