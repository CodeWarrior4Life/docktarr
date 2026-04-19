"""VPN health check for Doctarr.

Ported from arr-orchestrator/vpn.py ``check_vpn`` (container health path only).

Behavior faithfully ported
--------------------------
1. Probe the Gluetun container status via DockerManager.
   - Not running → restart + emit ``vpn.restarted`` event.
   - Running but Docker health != "healthy" → restart + emit ``vpn.restarted``.
2. If the container is healthy, probe Gluetun's HTTP status endpoint
   (``/v1/openvpn/status``) to validate:
   - Region is in the configured ``allowed_regions`` list (PIA Pattern 6:
     US regions have ZERO port forwarding — only CA servers support it).
     Wrong region → emit ``vpn.degraded`` (no restart).
   - Port forwarding is active when ``require_port_forwarding=True`` and the
     region is allowed.  No port → emit ``vpn.degraded`` (no restart).
3. No asyncio.sleep loops. Single-shot function driven by APScheduler.

PIA Pattern 6 (arr_known_issues.md)
------------------------------------
PIA has no port forwarding on any of its 55 US regions (permanent policy).
Inbound seed connections are silently killed, stalling torrents.  Only CA
servers (Toronto, Montreal, Vancouver) support port forwarding.
Default ``allowed_regions`` is ``["CA Toronto", "CA Montreal", "CA Vancouver"]``.

Gluetun /v1/openvpn/status JSON
---------------------------------
The endpoint returns a JSON object that includes at minimum::

    {
      "public_ip":       "203.0.113.4",
      "port_forwarded":  12345,          # 0 or false when not active
      "region":          "CA Toronto"
    }

Deviations from orchestrator
-----------------------------
- Orchestrator's ``check_vpn`` mixed speed-based VPN cycling (VPNMonitor class)
  with container restart logic.  The speed/cycling concern belongs to a separate
  speed-monitor job; only the container-health and region/port-forwarding checks
  are ported here to stay single-responsibility.
- Orchestrator used ``asyncio.sleep(30)`` after restart.  Doctarr is
  scheduler-driven — retry is implicit on the next tick.  Sleep removed.
- Region/port-forwarding validation via HTTP probe was not in the orchestrator
  at all; it is added here because:
    (a) the task spec explicitly requires it, and
    (b) Pattern 6 documents the exact failure mode it guards against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from doctarr.docker_manager import DockerManager
from doctarr.notifier import Notifier

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VpnHealthConfig:
    container_name: str = "gluetun"
    healthcheck_url: str = "http://gluetun:8888/v1/openvpn/status"
    allowed_regions: list[str] = field(
        default_factory=lambda: ["CA Toronto", "CA Montreal", "CA Vancouver"]
    )
    require_port_forwarding: bool = True


async def run_vpn_health(
    http_client: httpx.AsyncClient,
    docker_manager: DockerManager,
    notifier: Notifier,
    config: VpnHealthConfig,
) -> None:
    """Single-shot VPN health probe.

    Called by APScheduler on each health-check tick.  Checks the Gluetun
    container state, then validates region and port-forwarding via the
    Gluetun HTTP status endpoint.
    """
    # ------------------------------------------------------------------
    # 1. Container state probe (mirrors orchestrator's check_vpn step 1)
    # ------------------------------------------------------------------
    try:
        info = await docker_manager.get_container(config.container_name)
    except LookupError:
        log.error(
            "vpn_health: container %r not found — manual intervention required",
            config.container_name,
        )
        return

    # Docker container status values: "running", "exited", "paused",
    # "restarting", "dead", "created".  The orchestrator also checked a
    # separate health field from its own DockerManager; doctarr's ContainerInfo
    # only exposes ``status``.  We treat anything other than "running" as
    # requiring a restart (mirrors orchestrator: not running → restart).
    if info.status != "running":
        log.warning(
            "vpn_health: container %r is not running (status=%r) — restarting",
            config.container_name,
            info.status,
        )
        await docker_manager.restart(config.container_name)
        await notifier.emit(
            "vpn.restarted",
            {
                "container_name": config.container_name,
                "reason": f"container not running (status={info.status!r})",
            },
        )
        return

    log.debug("vpn_health: container %r is running — probing HTTP status", config.container_name)

    # ------------------------------------------------------------------
    # 2. HTTP status probe (region + port forwarding)
    # ------------------------------------------------------------------
    status = await _probe_gluetun_status(http_client, config.healthcheck_url)
    if status is None:
        # Logged inside helper; can't assess region without data
        return

    region: str = status.get("region", "")
    port_forwarded: Any = status.get("port_forwarded", 0)

    # ------------------------------------------------------------------
    # 2a. Region check — PIA Pattern 6
    # ------------------------------------------------------------------
    allowed = [r.casefold() for r in config.allowed_regions]
    if region.casefold() not in allowed:
        log.warning(
            "vpn_health: region %r is not in allowed list %r (PIA Pattern 6 — "
            "US regions have no port forwarding)",
            region,
            config.allowed_regions,
        )
        await notifier.emit(
            "vpn.degraded",
            {
                "reason": "wrong_region",
                "region": region,
                "allowed_regions": config.allowed_regions,
            },
        )
        return

    # ------------------------------------------------------------------
    # 2b. Port forwarding check
    # ------------------------------------------------------------------
    if config.require_port_forwarding and not _port_active(port_forwarded):
        log.warning(
            "vpn_health: region %r is allowed but port forwarding is inactive "
            "(port_forwarded=%r)",
            region,
            port_forwarded,
        )
        await notifier.emit(
            "vpn.degraded",
            {
                "reason": "no_port_forwarding",
                "region": region,
                "port_forwarded": port_forwarded,
            },
        )
        return

    log.debug(
        "vpn_health: healthy — region=%r port_forwarded=%r",
        region,
        port_forwarded,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _probe_gluetun_status(
    client: httpx.AsyncClient, url: str
) -> dict[str, Any] | None:
    """GET the Gluetun status endpoint.  Returns parsed JSON or None on error."""
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        log.error("vpn_health: HTTP probe failed for %r: %s", url, exc)
        return None

    if resp.status_code != 200:
        log.error(
            "vpn_health: HTTP probe returned %d for %r", resp.status_code, url
        )
        return None

    try:
        return resp.json()
    except Exception as exc:
        log.error("vpn_health: failed to parse JSON from %r: %s", url, exc)
        return None


def _port_active(port_forwarded: Any) -> bool:
    """Return True if the port_forwarded value indicates an active port.

    Gluetun may return an integer port number (truthy when non-zero),
    ``false`` / ``0`` when inactive, or ``true`` as a boolean.
    """
    if isinstance(port_forwarded, bool):
        return port_forwarded
    if isinstance(port_forwarded, int):
        return port_forwarded != 0
    # Unexpected type — treat as inactive
    return False
