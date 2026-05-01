"""ARR service reachability + container recovery checks for Docktarr.

Originally ported from arr-orchestrator/services.py ``check_arr_services``
(reachability path only). Extended in 0.5.2 to take corrective action when
an arr container has exited or its API has been unreachable for too long —
docktarr's mandate is to keep ALL configured arr members alive.

Behavior
--------
For each ``ArrClient`` in ``arr_clients``:

1. Probe the ``/api/{version}/health`` endpoint with the app's API key.
2. If reachable (HTTP 200): reset the consecutive-unreachable counter, log
   any download-related health items as warnings, return ``status="ok"``.
3. If unreachable (connection error or non-200) AND a ``DockerManager`` is
   provided, look up the container by ``ArrClient.container_name``:

   a. **Container exited**: restart it (subject to a per-service cooldown so
      we don't hammer Docker every 5 minutes if the underlying issue —
      e.g. a stale NFS volume — keeps making restarts fail). Emit
      ``arr.restarted`` on success or ``arr.restart_failed`` on exception.
   b. **Container running but API unreachable**: increment the consecutive
      counter; once it reaches ``running_unreachable_threshold`` (default 3),
      restart and emit ``arr.unreachable_threshold_restart``. The counter is
      higher than qbit_health's because arr apps have legitimately slow
      startups (database upgrades, library scans).
   c. **Container not found**: log error, no action.

4. Always emit ``service.down`` on unreachability (existing behavior, for
   backwards compatibility with anyone listening to that event).

5. If a ``HealthState`` is provided, publish a per-service snapshot to
   ``health_state.arr_services`` for ``/health/arr_services``.

Backwards compatibility
-----------------------
``run_arr_services(arr_clients, notifier)`` (the old 2-arg signature) keeps
working unchanged — when ``docker_manager`` is None, no recovery action is
taken and the function behaves exactly as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from docktarr.arrclient import ArrClient
from docktarr.docker_manager import DockerManager
from docktarr.notifier import Notifier

if TYPE_CHECKING:
    from docktarr.http_health import HealthState

log = logging.getLogger(__name__)


@dataclass
class ArrServicesState:
    """Mutable per-instance state shared across scheduler ticks.

    Keyed by service name (e.g. "Sonarr"), not container name, since service
    names are stable in config while container names can be overridden.
    """

    consecutive_unreachable: dict[str, int] = field(default_factory=dict)
    last_restart_attempt: dict[str, datetime] = field(default_factory=dict)


_DEFAULT_THRESHOLD = 3
_DEFAULT_COOLDOWN = timedelta(minutes=15)


async def run_arr_services(
    arr_clients: dict[str, ArrClient],
    notifier: Notifier,
    *,
    docker_manager: DockerManager | None = None,
    state: ArrServicesState | None = None,
    health_state: "HealthState | None" = None,
    running_unreachable_threshold: int = _DEFAULT_THRESHOLD,
    restart_cooldown: timedelta = _DEFAULT_COOLDOWN,
) -> list[dict[str, Any]]:
    """Single-shot ARR service reachability + recovery probe.

    See module docstring for the decision tree.
    """
    if state is None:
        state = ArrServicesState()

    results: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    for name, client in arr_clients.items():
        result = await _check_service(
            name=name,
            client=client,
            notifier=notifier,
            docker_manager=docker_manager,
            state=state,
            now=now,
            threshold=running_unreachable_threshold,
            cooldown=restart_cooldown,
        )
        results.append(result)

    if health_state is not None:
        health_state.record_arr_services(results)

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _check_service(
    *,
    name: str,
    client: ArrClient,
    notifier: Notifier,
    docker_manager: DockerManager | None,
    state: ArrServicesState,
    now: datetime,
    threshold: int,
    cooldown: timedelta,
) -> dict[str, Any]:
    version = client._api_version()
    url = f"{client._url}/api/{version}/health"
    params = {"apikey": client._api_key}

    reachable = False
    http_status: int | None = None
    error: str | None = None

    try:
        resp = await client._client.get(url, params=params, timeout=10.0)
        http_status = resp.status_code
        reachable = resp.status_code == 200
        if not reachable:
            log.warning(
                "arr_services: %s health endpoint returned HTTP %d — %s",
                name,
                resp.status_code,
                url,
            )
    except Exception as exc:
        error = str(exc)
        log.warning("arr_services: %s unreachable at %s — %s", name, url, exc)

    if reachable:
        # Reset counter; log any download-related health warnings.
        state.consecutive_unreachable.pop(name, None)
        try:
            for item in resp.json():
                source = (item.get("source") or "").lower()
                item_type = (item.get("type") or "").lower()
                if "download" in source and item_type == "error":
                    log.warning(
                        "arr_services: %s health warning — source=%s type=%s message=%s",
                        name,
                        item.get("source", ""),
                        item.get("type", ""),
                        item.get("message", ""),
                    )
        except Exception:
            pass
        return {
            "name": name,
            "url": url,
            "status": "ok",
            "http_status": 200,
            "error": None,
            "container_name": client.container_name,
            "container_status": None,
            "last_action": "ok",
        }

    # Unreachable. Always emit service.down for legacy listeners.
    await notifier.emit(
        "service.down",
        {
            "name": name,
            "url": url,
            "reason": error or f"HTTP {http_status}",
        },
    )

    base_result: dict[str, Any] = {
        "name": name,
        "url": url,
        "status": "down",
        "http_status": http_status,
        "error": error,
        "container_name": client.container_name,
        "container_status": None,
        "last_action": None,
    }

    if docker_manager is None:
        # Legacy / lab mode: no recovery wired up.
        base_result["last_action"] = "no_recovery_configured"
        return base_result

    # Container inspection + decision.
    try:
        info = await docker_manager.get_container(client.container_name)
    except LookupError:
        log.error(
            "arr_services: container %r for service %s not found — manual intervention required",
            client.container_name,
            name,
        )
        base_result["last_action"] = "container_not_found"
        return base_result

    base_result["container_status"] = info.status

    if info.status != "running":
        # Exited (any code). Try to restart, subject to per-service cooldown.
        last_attempt = state.last_restart_attempt.get(name)
        if last_attempt and (now - last_attempt) < cooldown:
            log.info(
                "arr_services: %s exited but in restart cooldown "
                "(last attempt %s, cooldown %s) — skipping",
                name,
                last_attempt.isoformat(),
                cooldown,
            )
            base_result["last_action"] = "cooldown"
            return base_result

        state.last_restart_attempt[name] = now
        try:
            await docker_manager.restart(client.container_name)
        except Exception as exc:
            log.error(
                "arr_services: failed to restart %s container %r: %s",
                name,
                client.container_name,
                exc,
            )
            await notifier.emit(
                "arr.restart_failed",
                {
                    "name": name,
                    "container_name": client.container_name,
                    "exit_code": info.exit_code,
                    "error": str(exc),
                },
            )
            base_result["last_action"] = "restart_failed"
            return base_result

        log.info(
            "arr_services: restarted %s container %r (was exited code=%r)",
            name,
            client.container_name,
            info.exit_code,
        )
        await notifier.emit(
            "arr.restarted",
            {
                "name": name,
                "container_name": client.container_name,
                "exit_code": info.exit_code,
            },
        )
        state.consecutive_unreachable.pop(name, None)
        base_result["last_action"] = "restarted_after_exit"
        return base_result

    # Container running but API unreachable. Threshold-based restart.
    count = state.consecutive_unreachable.get(name, 0) + 1
    state.consecutive_unreachable[name] = count

    if count >= threshold:
        last_attempt = state.last_restart_attempt.get(name)
        if last_attempt and (now - last_attempt) < cooldown:
            base_result["last_action"] = "cooldown"
            return base_result

        state.last_restart_attempt[name] = now
        try:
            await docker_manager.restart(client.container_name)
        except Exception as exc:
            log.error(
                "arr_services: failed to threshold-restart %s container %r: %s",
                name,
                client.container_name,
                exc,
            )
            await notifier.emit(
                "arr.restart_failed",
                {
                    "name": name,
                    "container_name": client.container_name,
                    "consecutive_ticks": count,
                    "error": str(exc),
                },
            )
            base_result["last_action"] = "restart_failed"
            return base_result

        log.warning(
            "arr_services: %s unreachable for %d consecutive ticks (threshold=%d) — "
            "restarted container %r",
            name,
            count,
            threshold,
            client.container_name,
        )
        await notifier.emit(
            "arr.unreachable_threshold_restart",
            {
                "name": name,
                "container_name": client.container_name,
                "consecutive_ticks": count,
                "threshold": threshold,
            },
        )
        state.consecutive_unreachable[name] = 0
        base_result["last_action"] = "unreachable_threshold_restart"
        return base_result

    log.info(
        "arr_services: %s running but unreachable (tick %d/%d before restart)",
        name,
        count,
        threshold,
    )
    base_result["last_action"] = "running_unreachable_grace"
    return base_result
