"""HTTP responsiveness probes for arbitrary services.

Catches the class of bug where a container is technically ``running`` and even
``healthy`` per docker, but its HTTP listener has become unresponsive — e.g.
Node's event loop is blocked retrying a failed upstream, or its API is being
hammered by another container's batch job. From outside, that looks like:

* Caddy reverse-proxy returns 502 (its 3-second upstream timeout fires before
  the container can answer).
* The browser sees "This page isn't working".
* The app's own ``/health`` endpoint, when hit at a quiet moment, returns 200
  and 80ms — so docker healthchecks pass and ``arr_services``-style boolean
  probes don't catch the problem.

Caught in production S107 2026-05-03: Seer (Overseerr-fork) at
``request.matrixmedia.fun`` started returning 502 after a Sonarr
``MissingEpisodeSearch`` over 10,360 wanted items saturated the ARR APIs that
Seer was calling internally. Seer's Node process kept running, healthcheck
kept passing, but actual HTTP responses crossed the 3-second mark. The fix
was a simple ``docker restart Seer``; what was missing was the *detection*.

This module probes arbitrary URLs (no auth required, just GET), measures
wall-clock latency, and restarts the named container after K consecutive ticks
above a configurable ``slow_ms`` threshold. The restart action is the same
``DockerManager.restart`` arr_services already uses, with the same cooldown
semantics so we never thrash.

Configuration is one ``SERVICE_PROBES`` env var with a small DSL:

    SERVICE_PROBES="<probe>;<probe>;..."

where each probe is a comma-separated tuple:

    name,url,container,slow_ms,consecutive_threshold

``slow_ms`` and ``consecutive_threshold`` are optional (defaults 3000 and 3).
Example:

    SERVICE_PROBES="Seer,http://Seer:5055/api/v1/status,Seer,3000,3;Plex,http://Plex:32400/identity,Plex,4000,3"
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import httpx

from docktarr.docker_manager import DockerManager
from docktarr.notifier import Notifier

if TYPE_CHECKING:
    from docktarr.http_health import HealthState

log = logging.getLogger("docktarr.responsiveness")

_DEFAULT_SLOW_MS = 3000
_DEFAULT_CONSECUTIVE = 3
_DEFAULT_COOLDOWN = timedelta(minutes=15)


@dataclass(frozen=True)
class ServiceProbeConfig:
    name: str
    url: str
    container_name: str
    slow_ms: int = _DEFAULT_SLOW_MS
    consecutive_threshold: int = _DEFAULT_CONSECUTIVE


@dataclass
class ServiceProbeState:
    consecutive_slow: int = 0
    last_restart_attempt: datetime | None = None
    last_latency_ms: int | None = None
    last_status: int | None = None


def parse_probes_env(raw: str) -> list[ServiceProbeConfig]:
    """Parse the ``SERVICE_PROBES`` DSL into a list of configs.

    Empty/blank input returns an empty list. Malformed entries are logged and
    skipped (won't crash startup).
    """
    out: list[ServiceProbeConfig] = []
    if not raw or not raw.strip():
        return out
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(",")]
        if len(parts) < 3:
            log.warning(
                "SERVICE_PROBES: skipping malformed entry %r (need at least name,url,container)",
                chunk,
            )
            continue
        name, url, container = parts[0], parts[1], parts[2]
        slow_ms = _DEFAULT_SLOW_MS
        consecutive = _DEFAULT_CONSECUTIVE
        try:
            if len(parts) >= 4 and parts[3]:
                slow_ms = int(parts[3])
            if len(parts) >= 5 and parts[4]:
                consecutive = int(parts[4])
        except ValueError:
            log.warning(
                "SERVICE_PROBES: %s has non-integer threshold, using defaults", name
            )
        out.append(
            ServiceProbeConfig(
                name=name,
                url=url,
                container_name=container,
                slow_ms=slow_ms,
                consecutive_threshold=consecutive,
            )
        )
    return out


async def _probe_one(
    client: httpx.AsyncClient,
    probe: ServiceProbeConfig,
    state: ServiceProbeState,
    docker_manager: DockerManager,
    notifier: Notifier,
    now: datetime,
    cooldown: timedelta,
) -> dict[str, Any]:
    """Probe a single service. Returns the result row for /health."""
    timeout = max(probe.slow_ms / 1000.0 * 2, 10.0)
    latency_ms: int | None = None
    status: int | None = None
    error: str | None = None
    slow = False

    t0 = time.perf_counter()
    try:
        resp = await client.get(probe.url, timeout=timeout)
        latency_ms = int((time.perf_counter() - t0) * 1000)
        status = resp.status_code
        # 5xx counts as "slow / unhealthy upstream" too. Auth challenges (401/403)
        # are fine — the service IS responding.
        slow = latency_ms >= probe.slow_ms or status >= 500
    except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
        latency_ms = int((time.perf_counter() - t0) * 1000)
        error = f"{type(exc).__name__}: {exc}"[:200]
        slow = True
    except Exception as exc:  # noqa: BLE001
        latency_ms = int((time.perf_counter() - t0) * 1000)
        error = f"{type(exc).__name__}: {exc}"[:200]
        slow = True

    state.last_latency_ms = latency_ms
    state.last_status = status

    base: dict[str, Any] = {
        "name": probe.name,
        "url": probe.url,
        "container": probe.container_name,
        "latency_ms": latency_ms,
        "http_status": status,
        "error": error,
        "slow_ms_threshold": probe.slow_ms,
        "consecutive_slow": state.consecutive_slow,
        "consecutive_threshold": probe.consecutive_threshold,
        "last_action": None,
    }

    if not slow:
        if state.consecutive_slow:
            log.info(
                "%s responsive again after %d slow tick(s) (latency=%dms)",
                probe.name,
                state.consecutive_slow,
                latency_ms,
            )
        state.consecutive_slow = 0
        base["last_action"] = "ok"
        base["consecutive_slow"] = 0
        return base

    state.consecutive_slow += 1
    base["consecutive_slow"] = state.consecutive_slow
    log.warning(
        "%s slow: latency=%dms status=%s err=%s (%d/%d)",
        probe.name,
        latency_ms,
        status,
        error,
        state.consecutive_slow,
        probe.consecutive_threshold,
    )

    if state.consecutive_slow < probe.consecutive_threshold:
        base["last_action"] = "slow_observed"
        return base

    # Threshold breached. Cooldown check.
    if state.last_restart_attempt and (now - state.last_restart_attempt) < cooldown:
        base["last_action"] = "cooldown"
        return base

    state.last_restart_attempt = now
    try:
        await docker_manager.restart(probe.container_name)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "service_responsiveness: restart of %s container %r FAILED: %s",
            probe.name,
            probe.container_name,
            exc,
        )
        await notifier.emit(
            "service.slow_restart_failed",
            {
                "name": probe.name,
                "container_name": probe.container_name,
                "latency_ms": latency_ms,
                "error": str(exc)[:200],
            },
        )
        base["last_action"] = "restart_failed"
        return base

    log.warning(
        "service_responsiveness: restarted %s container %r after %d slow ticks "
        "(latency=%dms threshold=%dms)",
        probe.name,
        probe.container_name,
        state.consecutive_slow,
        latency_ms,
        probe.slow_ms,
    )
    await notifier.emit(
        "service.slow_threshold_restart",
        {
            "name": probe.name,
            "container_name": probe.container_name,
            "latency_ms": latency_ms,
            "consecutive_ticks": state.consecutive_slow,
            "threshold_ms": probe.slow_ms,
            "consecutive_threshold": probe.consecutive_threshold,
            "http_status": status,
        },
    )
    state.consecutive_slow = 0
    base["last_action"] = "slow_threshold_restart"
    base["consecutive_slow"] = 0
    return base


async def run_service_responsiveness(
    probes: list[ServiceProbeConfig],
    docker_manager: DockerManager,
    notifier: Notifier,
    *,
    state: dict[str, ServiceProbeState] | None = None,
    health_state: "HealthState | None" = None,
    restart_cooldown: timedelta = _DEFAULT_COOLDOWN,
) -> list[dict[str, Any]]:
    """Run a single sweep of all probes."""
    if state is None:
        state = {}

    now = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(follow_redirects=False) as client:
        for probe in probes:
            ps = state.setdefault(probe.name, ServiceProbeState())
            try:
                results.append(
                    await _probe_one(
                        client,
                        probe,
                        ps,
                        docker_manager,
                        notifier,
                        now,
                        restart_cooldown,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("probe %s crashed: %s", probe.name, exc)
                results.append(
                    {
                        "name": probe.name,
                        "url": probe.url,
                        "container": probe.container_name,
                        "latency_ms": None,
                        "http_status": None,
                        "error": f"probe crashed: {exc}",
                        "last_action": "probe_crashed",
                    }
                )

    if health_state is not None and hasattr(
        health_state, "record_service_responsiveness"
    ):
        health_state.record_service_responsiveness(results)
    return results
