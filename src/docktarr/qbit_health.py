"""qBittorrent health check for Docktarr.

Originally ported from arr-orchestrator/qbittorrent.py ``check_qbittorrent``;
extended in 0.5.1 to detect a stale gluetun network namespace (Pattern 1
variant where qBit's container survives a gluetun restart but its API is
unreachable because the network namespace was torn down).

Behavior
--------
1. Probe qBit reachability via ``login()`` + ``/app/version``.
2. Look up the qBittorrent container.
3. Decision tree, in order:

   a. **Container exited with exit_code == 137** (OOM / SIGKILL from gluetun
      network teardown): restart, emit ``qbit.restarted``.
   b. **Container running but qBit API unreachable, gluetun.started_at >
      qbit.started_at** (gluetun restarted while qBit kept running -> stale
      network namespace): restart, emit ``qbit.stale_namespace_restart``.
   c. **Container running but qBit API unreachable** for ``N`` consecutive
      ticks (configurable, default 2): restart, emit
      ``qbit.unreachable_threshold_restart``. This is the safety net for cases
      where (b) cannot decide — VPN container missing, clock skew, etc.
   d. **Otherwise** (running but unreachable, first tick): log and increment
      the consecutive counter; reschedule.
   e. **Exited with any code other than 137**: log, no auto-action.

Diagnostics
-----------
When a ``HealthState`` is provided, every tick records a structured snapshot
to ``health_state.qbit_health`` so operators can see, via ``/health`` or
``/health/qbit``, exactly why qBit is or isn't healthy and whether docktarr
took an action.

MAM Compliance
--------------
This module does NOT iterate or mutate torrents. ``protected_categories`` is
preserved on ``QbitHealthConfig`` for documentation; no torrent in those
categories will ever be touched here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from docktarr.docker_manager import ContainerInfo, DockerManager
from docktarr.notifier import Notifier
from docktarr.qbittorrent import QBitClient

if TYPE_CHECKING:
    from docktarr.http_health import HealthState

log = logging.getLogger(__name__)

_OOM_EXIT_CODE = 137  # SIGKILL — gluetun network teardown kills qBit (Pattern 1)
_DEFAULT_COOLDOWN = timedelta(minutes=15)


@dataclass(frozen=True)
class QbitHealthConfig:
    container_name: str
    protected_categories: list[str] = field(default_factory=lambda: ["MAM"])
    vpn_container_name: str | None = "gluetun"
    running_unreachable_threshold: int = 2
    restart_cooldown: timedelta = _DEFAULT_COOLDOWN


@dataclass
class QbitHealthState:
    """Mutable per-instance state shared across scheduler ticks."""

    consecutive_unreachable: int = 0
    last_restart_attempt: datetime | None = None


async def run_qbit_health(
    qbit: QBitClient,
    docker_manager: DockerManager,
    notifier: Notifier,
    config: QbitHealthConfig,
    *,
    state: QbitHealthState | None = None,
    health_state: "HealthState | None" = None,
) -> None:
    """Single-shot qBittorrent health probe."""
    if state is None:
        state = QbitHealthState()

    now = datetime.now(timezone.utc)
    snapshot: dict = {
        "last_check": now.isoformat(),
        "reachable": None,
        "container_status": None,
        "container_exit_code": None,
        "qbit_started_at": None,
        "vpn_container": config.vpn_container_name,
        "vpn_status": None,
        "vpn_started_at": None,
        "stale_namespace_detected": False,
        "consecutive_unreachable": state.consecutive_unreachable,
        "last_action": None,
    }

    def _publish() -> None:
        snapshot["consecutive_unreachable"] = state.consecutive_unreachable
        if health_state is not None:
            health_state.record_qbit_health(snapshot)

    # ------------------------------------------------------------------
    # 1. Liveness probe
    # ------------------------------------------------------------------
    alive = await _is_alive(qbit)
    snapshot["reachable"] = alive

    if alive:
        if state.consecutive_unreachable:
            log.info(
                "qBit health: reachable again after %d unreachable tick(s)",
                state.consecutive_unreachable,
            )
        state.consecutive_unreachable = 0
        snapshot["last_action"] = "ok"
        _publish()
        return

    log.warning("qBit health: qBittorrent is unreachable — inspecting container")

    # ------------------------------------------------------------------
    # 2. qBit container inspection
    # ------------------------------------------------------------------
    try:
        info = await docker_manager.get_container(config.container_name)
    except LookupError:
        log.error(
            "qBit health: container %r not found — manual intervention required",
            config.container_name,
        )
        snapshot["last_action"] = "container_not_found"
        _publish()
        return

    snapshot["container_status"] = info.status
    snapshot["container_exit_code"] = info.exit_code
    snapshot["qbit_started_at"] = (
        info.started_at.isoformat() if info.started_at else None
    )
    log.info(
        "qBit health: container %r status=%r exit_code=%r started_at=%s",
        info.name,
        info.status,
        info.exit_code,
        info.started_at.isoformat() if info.started_at else "?",
    )

    # ------------------------------------------------------------------
    # 3a. Container is not running — restart with cooldown.
    # ------------------------------------------------------------------
    # Pre-0.5.4 only restarted on exit 137 (Pattern 1 OOM kill). In practice
    # other exit codes (e.g. 255 from a failed volume mount during a gluetun
    # cycle) are equally recoverable by a fresh start, and leaving qBit dead
    # cascades into the entire arr stack going dark. Restart on ANY non-running
    # status, with a per-instance cooldown so we don't hammer Docker if the
    # underlying issue (NFS dead, image pull denied, etc.) keeps making
    # restarts fail.
    if info.status != "running":
        last_attempt = state.last_restart_attempt
        if last_attempt and (now - last_attempt) < config.restart_cooldown:
            log.info(
                "qBit health: container %r exited (code=%r) but in restart cooldown "
                "(last attempt %s, cooldown %s) — skipping",
                config.container_name,
                info.exit_code,
                last_attempt.isoformat(),
                config.restart_cooldown,
            )
            snapshot["last_action"] = "cooldown"
            _publish()
            return

        state.last_restart_attempt = now
        try:
            await docker_manager.restart(config.container_name)
        except Exception as exc:
            log.error(
                "qBit health: failed to restart container %r: %s",
                config.container_name,
                exc,
            )
            await notifier.emit(
                "qbit.restart_failed",
                {
                    "container_name": config.container_name,
                    "exit_code": info.exit_code,
                    "error": str(exc),
                },
            )
            snapshot["last_action"] = "restart_failed"
            _publish()
            return

        log.info(
            "qBit health: restarted container %r (was exited code=%r)",
            config.container_name,
            info.exit_code,
        )
        await notifier.emit(
            "qbit.restarted",
            {
                "container_name": config.container_name,
                "exit_code": info.exit_code,
            },
        )
        state.consecutive_unreachable = 0
        snapshot["last_action"] = (
            "restart_exit_137"
            if info.exit_code == _OOM_EXIT_CODE
            else "restart_after_exit"
        )
        _publish()
        return

    # ------------------------------------------------------------------
    # 3b. Stale gluetun namespace: gluetun restarted while qBit kept running
    # ------------------------------------------------------------------
    vpn_info: ContainerInfo | None = None
    if config.vpn_container_name:
        try:
            vpn_info = await docker_manager.get_container(config.vpn_container_name)
        except LookupError:
            log.warning(
                "qBit health: VPN container %r not found — skipping namespace check",
                config.vpn_container_name,
            )

    if vpn_info is not None:
        snapshot["vpn_status"] = vpn_info.status
        snapshot["vpn_started_at"] = (
            vpn_info.started_at.isoformat() if vpn_info.started_at else None
        )
        if (
            info.started_at is not None
            and vpn_info.started_at is not None
            and vpn_info.started_at > info.started_at
        ):
            snapshot["stale_namespace_detected"] = True
            log.warning(
                "qBit health: stale gluetun namespace detected "
                "(gluetun started %s, qbit started %s) — restarting %r",
                vpn_info.started_at.isoformat(),
                info.started_at.isoformat(),
                config.container_name,
            )
            await docker_manager.restart(config.container_name)
            await notifier.emit(
                "qbit.stale_namespace_restart",
                {
                    "container_name": config.container_name,
                    "vpn_container": config.vpn_container_name,
                    "vpn_started_at": vpn_info.started_at.isoformat(),
                    "qbit_started_at": info.started_at.isoformat(),
                },
            )
            state.consecutive_unreachable = 0
            snapshot["last_action"] = "stale_namespace_restart"
            _publish()
            return

    # ------------------------------------------------------------------
    # 3c. Threshold fallback: N consecutive running-but-unreachable ticks
    # ------------------------------------------------------------------
    state.consecutive_unreachable += 1
    if state.consecutive_unreachable >= config.running_unreachable_threshold:
        log.warning(
            "qBit health: %d consecutive unreachable ticks (threshold=%d) — "
            "restarting %r",
            state.consecutive_unreachable,
            config.running_unreachable_threshold,
            config.container_name,
        )
        await docker_manager.restart(config.container_name)
        await notifier.emit(
            "qbit.unreachable_threshold_restart",
            {
                "container_name": config.container_name,
                "consecutive_ticks": state.consecutive_unreachable,
                "threshold": config.running_unreachable_threshold,
            },
        )
        state.consecutive_unreachable = 0
        snapshot["last_action"] = "unreachable_threshold_restart"
        _publish()
        return

    log.error(
        "qBit health: container %r is running but qBit API is unreachable "
        "(tick %d/%d before restart)",
        config.container_name,
        state.consecutive_unreachable,
        config.running_unreachable_threshold,
    )
    snapshot["last_action"] = "running_unreachable_grace"
    _publish()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _is_alive(qbit: QBitClient) -> bool:
    """Return True if qBit login + /app/version both succeed."""
    try:
        await qbit.login()
    except Exception as exc:
        log.debug("qBit health: login failed: %s", exc)
        return False

    try:
        resp = await qbit._client.get(
            f"{qbit._base_url}/api/v2/app/version",
            cookies=qbit._cookies(),
        )
        return resp.status_code == 200
    except Exception as exc:
        log.debug("qBit health: version probe failed: %s", exc)
        return False
