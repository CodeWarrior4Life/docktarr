"""qBittorrent health check for Doctarr.

Ported from arr-orchestrator/qbittorrent.py ``check_qbittorrent``.

Behavior faithfully ported
--------------------------
1. Probe qBit reachability via login() — if that raises or is_alive() returns
   False the container is assumed unhealthy.
2. Look up the qBittorrent container via DockerManager.get_container().
3. Decision tree:
   - Container exited with exit_code == 137 (OOM / SIGKILL from gluetun network
     teardown — see Pattern 1 in arr_known_issues.md): call restart() and emit
     ``qbit.restarted`` event.
   - Container is running but qBit is unreachable: log an error and do NOT
     restart (could be mid-startup after a recent automatic restart).
   - Container not found: log an error — operator intervention needed.
4. No asyncio.sleep loops. This is a single-shot async function called by
   APScheduler. All retry / recovery logic is handled by re-running on the
   next schedule tick.

MAM Compliance note
-------------------
This module does NOT iterate or mutate torrents. The ``protected_categories``
field in ``QbitHealthConfig`` is present for pass-through documentation and for
future use if torrent-listing is added. No torrent with a category in
``protected_categories`` will ever be touched by this module.

Deviations from orchestrator
-----------------------------
- Orchestrator retried up to ``max_auto_fix_attempts`` times within the same
  run (with asyncio.sleep between), also checking gluetun first, and starting
  qbt_tracker_updater. Doctarr is scheduler-driven so retry is implicit (next
  tick). Single-shot simplifies the function.
- Orchestrator also enforced settings drift and category creation. Those are
  separate concerns and not ported here to keep scope minimal.
- Exit-code-137 is the ONLY condition that triggers a restart. A running
  container that is unreachable is left alone intentionally.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from docktarr.docker_manager import DockerManager
from docktarr.notifier import Notifier
from docktarr.qbittorrent import QBitClient

log = logging.getLogger(__name__)

_OOM_EXIT_CODE = 137  # SIGKILL — gluetun network teardown kills qBit (Pattern 1)


@dataclass(frozen=True)
class QbitHealthConfig:
    container_name: str
    protected_categories: list[str] = field(default_factory=lambda: ["MAM"])


async def run_qbit_health(
    qbit: QBitClient,
    docker_manager: DockerManager,
    notifier: Notifier,
    config: QbitHealthConfig,
) -> None:
    """Single-shot qBittorrent health probe.

    Called by APScheduler on each health-check tick. Checks liveness, and if
    unreachable, inspects the container exit code to decide whether to restart.
    """
    # ------------------------------------------------------------------
    # 1. Liveness probe
    # ------------------------------------------------------------------
    alive = await _is_alive(qbit)

    if alive:
        log.debug("qBit health: reachable — nothing to do")
        return

    log.warning("qBit health: qBittorrent is unreachable — inspecting container")

    # ------------------------------------------------------------------
    # 2. Container inspection
    # ------------------------------------------------------------------
    try:
        info = await docker_manager.get_container(config.container_name)
    except LookupError:
        log.error(
            "qBit health: container %r not found — manual intervention required",
            config.container_name,
        )
        return

    log.info(
        "qBit health: container %r status=%r exit_code=%r",
        info.name,
        info.status,
        info.exit_code,
    )

    # ------------------------------------------------------------------
    # 3. Decision: restart only on exit 137 (OOM / gluetun kill)
    # ------------------------------------------------------------------
    if info.status != "running" and info.exit_code == _OOM_EXIT_CODE:
        log.info(
            "qBit health: container exited with code 137 — restarting %r",
            config.container_name,
        )
        await docker_manager.restart(config.container_name)
        log.info("qBit health: restart issued for %r", config.container_name)
        await notifier.emit(
            "qbit.restarted",
            {
                "container_name": config.container_name,
                "exit_code": info.exit_code,
            },
        )
        return

    if info.status == "running":
        # Running but unreachable — may be still booting. Do NOT restart.
        log.error(
            "qBit health: container %r is running but qBit API is unreachable "
            "(may be mid-startup — will re-check next tick)",
            config.container_name,
        )
        return

    # Exited for any reason other than 137 — log, do not auto-restart.
    log.error(
        "qBit health: container %r exited with code %r (not 137) — "
        "manual intervention required",
        config.container_name,
        info.exit_code,
    )


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
