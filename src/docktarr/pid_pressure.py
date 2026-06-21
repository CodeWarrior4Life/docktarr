"""PID / zombie-process pressure watchdog.

Born from the 2026-06-21 ``surplusrecovery-harvester-1`` incident: a container
ran ``python`` as PID 1 with no init system. A bare interpreter as PID 1 does
not reap its children, so every short-lived ``chrome`` subprocess it spawned
turned into a ``<defunct>`` (state ``Z``) zombie. Over six weeks 439 zombies
accumulated, exhausting the host process table and tripping QTS "RAM disk"
alerts. The fix was ``init: true`` (tini), which reaps orphans.

This module DETECTS that failure mode so the next leak is caught in hours, not
weeks. For each running container it reads ``docker top`` (the Engine
``/containers/{id}/top`` endpoint — the same data as the ``docker top`` CLI),
which lists the container's processes *as seen on the host*, including their
STAT codes. From that single primitive we derive:

  * ``pid_count``   — total processes in the container
  * ``zombie_count`` — processes whose STAT starts with ``Z`` (defunct)

A breach is flagged when, for ``debounce`` consecutive checks, a container's
PID count exceeds ``container_pid_warn`` OR its zombie count exceeds
``zombie_warn_per_container`` (or the host-wide zombie total exceeds
``zombie_warn_total``). Debounce avoids alerting on transient spikes (a burst
of short-lived workers is normal; a *monotonically growing* zombie pile is not).

Default behavior is ALERT-ONLY. ``auto_restart`` is opt-in and, even when
enabled, only fires when a container is over the *hard cap*
(``2 × container_pid_warn`` by default) — a restart is a blunt instrument and
the real fix is ``init: true``, which the alert names explicitly.

This module uses docktarr's existing :class:`docktarr.docker_manager.DockerManager`
docker-socket access. No SSH / ``/proc`` parsing is needed: ``docker top``
attributes processes to containers and exposes STAT for free, which is both
cleaner and host-OS-agnostic compared to scraping the host process table and
re-parenting by PID-1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from docktarr.docker_manager import DockerManager
from docktarr.notifier import Notifier

log = logging.getLogger(__name__)

_DEFAULT_CONTAINER_PID_WARN = 200
_DEFAULT_ZOMBIE_WARN_TOTAL = 50
_DEFAULT_ZOMBIE_WARN_PER_CONTAINER = 30
_DEFAULT_DEBOUNCE = 2


@dataclass(frozen=True)
class PidPressureConfig:
    """Tunables for the PID-pressure watchdog."""

    enabled: bool = True
    container_pid_warn: int = _DEFAULT_CONTAINER_PID_WARN
    zombie_warn_total: int = _DEFAULT_ZOMBIE_WARN_TOTAL
    zombie_warn_per_container: int = _DEFAULT_ZOMBIE_WARN_PER_CONTAINER
    # Alert-only by default. When True, a container over the hard cap is
    # restarted (and the action noted) instead of merely alerted.
    auto_restart: bool = False
    # Consecutive breaching checks required before an alert/restart fires.
    debounce: int = _DEFAULT_DEBOUNCE
    # Hard cap multiplier: auto_restart only triggers above this multiple of
    # container_pid_warn. Keeps restarts reserved for runaway leaks.
    hard_cap_multiplier: float = 2.0


@dataclass
class PidPressureState:
    """Mutable per-instance state shared across scheduler ticks.

    ``consecutive_breaches`` is keyed by container name; it counts how many
    ticks in a row a container has breached a threshold, so debounce can gate
    alerting. Containers that fall back under threshold reset to 0.
    """

    consecutive_breaches: dict[str, int] = field(default_factory=dict)


def _parse_top(top: dict[str, Any]) -> tuple[int, int]:
    """Return ``(pid_count, zombie_count)`` from a ``docker top`` result.

    ``top`` is the Docker dict ``{"Titles": [...], "Processes": [[...], ...]}``.
    We locate the STAT column by title (case-insensitive ``STAT``/``S``) and
    count rows whose state code starts with ``Z`` (zombie/defunct). If no STAT
    column is present we still return the PID count with a zombie count of 0
    (degrade gracefully rather than guess).
    """
    processes = top.get("Processes") or []
    pid_count = len(processes)

    titles = top.get("Titles") or []
    stat_idx: int | None = None
    for i, title in enumerate(titles):
        t = str(title).strip().upper()
        if t in ("STAT", "S", "STATE"):
            stat_idx = i
            break

    zombie_count = 0
    if stat_idx is not None:
        for row in processes:
            if stat_idx < len(row):
                stat = str(row[stat_idx]).strip().upper()
                if stat.startswith("Z"):
                    zombie_count += 1

    return pid_count, zombie_count


async def run_pid_pressure(
    docker_manager: DockerManager,
    notifier: Notifier,
    config: PidPressureConfig | None = None,
    *,
    state: PidPressureState | None = None,
) -> list[dict[str, Any]]:
    """Single-shot PID/zombie pressure probe across all running containers.

    Returns a list of per-container status dicts with keys:
      - container (str)
      - pid_count (int)
      - zombie_count (int)
      - status (str): "ok" | "warning" | "restarted" | "error"

    Called by APScheduler on each health-check tick. Never raises — any failure
    enumerating containers or reading ``docker top`` degrades to a logged
    warning so the scheduler loop is never crashed.
    """
    if config is None:
        config = PidPressureConfig()
    if state is None:
        state = PidPressureState()

    try:
        names = await docker_manager.list_running_containers()
    except Exception as exc:
        log.warning("pid_pressure: failed to list containers: %s", exc)
        return []

    results: list[dict[str, Any]] = []
    zombie_total = 0
    seen: set[str] = set()

    for name in names:
        seen.add(name)
        try:
            top = await docker_manager.top(name)
        except Exception as exc:
            # A container can exit between list and top — that's benign.
            log.debug("pid_pressure: top(%r) failed: %s", name, exc)
            results.append({"container": name, "status": "error", "error": str(exc)})
            continue

        pid_count, zombie_count = _parse_top(top)
        zombie_total += zombie_count

        result = await _evaluate_container(
            name=name,
            pid_count=pid_count,
            zombie_count=zombie_count,
            docker_manager=docker_manager,
            notifier=notifier,
            config=config,
            state=state,
        )
        results.append(result)

    # Drop state for containers that no longer exist so the dict doesn't grow
    # unbounded across the process lifetime.
    for stale in [k for k in state.consecutive_breaches if k not in seen]:
        del state.consecutive_breaches[stale]

    # Host-wide zombie total — independent of any single container exceeding
    # its per-container cap (death by a thousand cuts across many containers).
    if zombie_total >= config.zombie_warn_total:
        log.warning(
            "pid_pressure: host zombie total %d >= %d",
            zombie_total,
            config.zombie_warn_total,
        )
        await notifier.emit(
            "pid_pressure.zombies_total",
            {
                "zombie_total": zombie_total,
                "threshold": config.zombie_warn_total,
            },
        )

    return results


async def _evaluate_container(
    *,
    name: str,
    pid_count: int,
    zombie_count: int,
    docker_manager: DockerManager,
    notifier: Notifier,
    config: PidPressureConfig,
    state: PidPressureState,
) -> dict[str, Any]:
    """Apply thresholds + debounce to one container; alert/restart on breach."""
    payload = {
        "container": name,
        "pid_count": pid_count,
        "zombie_count": zombie_count,
    }

    pid_breach = pid_count >= config.container_pid_warn
    zombie_breach = zombie_count >= config.zombie_warn_per_container
    breach = pid_breach or zombie_breach

    if not breach:
        state.consecutive_breaches.pop(name, None)
        log.debug(
            "pid_pressure: %s ok — %d pids, %d zombies", name, pid_count, zombie_count
        )
        return {**payload, "status": "ok"}

    streak = state.consecutive_breaches.get(name, 0) + 1
    state.consecutive_breaches[name] = streak

    log.debug(
        "pid_pressure: %s breach #%d — %d pids, %d zombies (warn: pid>=%d, zombie>=%d)",
        name,
        streak,
        pid_count,
        zombie_count,
        config.container_pid_warn,
        config.zombie_warn_per_container,
    )

    # Debounce: don't alert until the breach has persisted.
    if streak < config.debounce:
        return {**payload, "status": "warning", "debounced": True}

    cause = (
        "container PID 1 may be a bare interpreter with no init — add "
        "`init: true` (tini) so it reaps child/zombie processes"
    )
    log.warning(
        "pid_pressure: %s BREACH — %d pids, %d zombies (streak=%d). %s",
        name,
        pid_count,
        zombie_count,
        streak,
        cause,
    )

    hard_cap = int(config.container_pid_warn * config.hard_cap_multiplier)
    if config.auto_restart and pid_count >= hard_cap:
        try:
            await docker_manager.restart(name)
            log.warning(
                "pid_pressure: auto-restarted %s (pid_count %d >= hard cap %d)",
                name,
                pid_count,
                hard_cap,
            )
            state.consecutive_breaches.pop(name, None)
            await notifier.emit(
                "pid_pressure.restarted",
                {**payload, "hard_cap": hard_cap, "cause": cause},
            )
            return {**payload, "status": "restarted", "hard_cap": hard_cap}
        except Exception as exc:
            log.warning("pid_pressure: restart of %s failed: %s", name, exc)
            await notifier.emit(
                "pid_pressure.restart_failed",
                {**payload, "error": str(exc)},
            )
            return {**payload, "status": "error", "error": str(exc)}

    await notifier.emit(
        "pid_pressure.breach",
        {
            **payload,
            "pid_warn": config.container_pid_warn,
            "zombie_warn": config.zombie_warn_per_container,
            "cause": cause,
        },
    )
    return {**payload, "status": "warning"}
