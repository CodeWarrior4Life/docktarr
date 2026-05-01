"""Tests for arr_services — ARR app reachability checks."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from docktarr.arr_services import ArrServicesState, run_arr_services
from docktarr.arrclient import ArrClient
from docktarr.config import ArrAppConfig
from docktarr.docker_manager import ContainerInfo
from docktarr.http_health import HealthState
from docktarr.notifier import Notifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_notifier() -> tuple[Notifier, list[dict]]:
    events: list[dict] = []

    class CapturingNotifier(Notifier):
        async def emit(self, event: str, payload: dict) -> None:
            events.append({"event": event, "payload": payload})

    transport = httpx.MockTransport(lambda r: httpx.Response(204))
    n = CapturingNotifier(
        httpx.AsyncClient(transport=transport),
        webhook_url=None,
        enabled_events=[],
    )
    return n, events


def _make_client(
    name: str,
    *,
    status_code: int = 200,
    body: list | None = None,
    connect_error: bool = False,
    container_name: str | None = None,
) -> ArrClient:
    """Return an ArrClient backed by a MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if connect_error:
            raise httpx.ConnectError("Connection refused")
        if body is not None:
            return httpx.Response(status_code, json=body)
        return httpx.Response(status_code)

    cfg = ArrAppConfig(
        url="http://localhost:8989",
        api_key="testkey",
        name=name,
        container_name=container_name,
    )
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _make_docker_multi(containers: dict[str, ContainerInfo]) -> MagicMock:
    """DockerManager mock that dispatches by container name."""
    dm = MagicMock()

    async def _get(name: str) -> ContainerInfo:
        if name not in containers:
            raise LookupError(f"container {name!r} not found")
        return containers[name]

    dm.get_container = AsyncMock(side_effect=_get)
    dm.restart = AsyncMock()
    return dm


def _container(
    *,
    name: str,
    status: str,
    exit_code: int | None = None,
    started_at: datetime | None = None,
) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        status=status,
        image="example:latest",
        exit_code=exit_code,
        started_at=started_at,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_services_healthy_no_events():
    """All services return 200 → no events, all status 'ok'."""
    clients = {
        "Sonarr": _make_client("Sonarr", status_code=200, body=[]),
        "Radarr": _make_client("Radarr", status_code=200, body=[]),
    }
    notifier, events = _make_notifier()

    results = await run_arr_services(clients, notifier)

    assert len(results) == 2
    assert all(r["status"] == "ok" for r in results)
    assert events == []


@pytest.mark.asyncio
async def test_service_unreachable_emits_service_down():
    """Connection error → emit service.down, status 'down'."""
    clients = {
        "Sonarr": _make_client("Sonarr", connect_error=True),
    }
    notifier, events = _make_notifier()

    results = await run_arr_services(clients, notifier)

    assert results[0]["status"] == "down"
    assert results[0]["http_status"] is None
    assert "Connection refused" in results[0]["error"]
    assert len(events) == 1
    assert events[0]["event"] == "service.down"
    assert events[0]["payload"]["name"] == "Sonarr"


@pytest.mark.asyncio
async def test_service_non_200_emits_service_down():
    """Non-200 HTTP status → emit service.down, status 'down'."""
    clients = {
        "Radarr": _make_client("Radarr", status_code=503),
    }
    notifier, events = _make_notifier()

    results = await run_arr_services(clients, notifier)

    assert results[0]["status"] == "down"
    assert results[0]["http_status"] == 503
    assert results[0]["error"] is None
    assert len(events) == 1
    assert events[0]["event"] == "service.down"
    assert events[0]["payload"]["name"] == "Radarr"
    assert "503" in events[0]["payload"]["reason"]


@pytest.mark.asyncio
async def test_mixed_services_partial_failure():
    """One healthy, one down → only the down service emits service.down."""
    clients = {
        "Sonarr": _make_client("Sonarr", status_code=200, body=[]),
        "Readarr": _make_client("Readarr", connect_error=True),
    }
    notifier, events = _make_notifier()

    results = await run_arr_services(clients, notifier)

    sonarr = next(r for r in results if r["name"] == "Sonarr")
    readarr = next(r for r in results if r["name"] == "Readarr")

    assert sonarr["status"] == "ok"
    assert readarr["status"] == "down"
    assert len(events) == 1
    assert events[0]["payload"]["name"] == "Readarr"


@pytest.mark.asyncio
async def test_download_error_in_health_items_logged_not_emitted():
    """Health item with download source + error type → logged warning, no service.down event."""
    health_body = [
        {
            "source": "DownloadClientCheck",
            "type": "error",
            "message": "Unable to connect to download client",
        }
    ]
    clients = {
        "Sonarr": _make_client("Sonarr", status_code=200, body=health_body),
    }
    notifier, events = _make_notifier()

    results = await run_arr_services(clients, notifier)

    # Service is reachable (200) so status is ok, but the health item is logged
    assert results[0]["status"] == "ok"
    # No service.down event — this is a logged warning, not an outage
    assert events == []


@pytest.mark.asyncio
async def test_non_download_health_errors_ignored():
    """Health items without 'download' in source are ignored."""
    health_body = [
        {
            "source": "IndexerRssCheck",
            "type": "error",
            "message": "All RSS feeds are failing",
        }
    ]
    clients = {
        "Sonarr": _make_client("Sonarr", status_code=200, body=health_body),
    }
    notifier, events = _make_notifier()

    results = await run_arr_services(clients, notifier)

    assert results[0]["status"] == "ok"
    assert events == []


@pytest.mark.asyncio
async def test_empty_clients_returns_empty():
    """Empty clients dict → empty results, no events."""
    notifier, events = _make_notifier()
    results = await run_arr_services({}, notifier)
    assert results == []
    assert events == []


@pytest.mark.asyncio
async def test_sonarr_uses_v3_api_version():
    """Sonarr client probes /api/v3/health (v3 for Sonarr/Radarr)."""
    probed_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        probed_urls.append(str(request.url))
        return httpx.Response(200, json=[])

    cfg = ArrAppConfig(url="http://sonarr:8989", api_key="key", name="Sonarr")
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    notifier, events = _make_notifier()
    await run_arr_services({"Sonarr": client}, notifier)

    assert any("/api/v3/health" in u for u in probed_urls)


@pytest.mark.asyncio
async def test_readarr_uses_v1_api_version():
    """Readarr client probes /api/v1/health (v1 for non-Sonarr/Radarr)."""
    probed_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        probed_urls.append(str(request.url))
        return httpx.Response(200, json=[])

    cfg = ArrAppConfig(url="http://readarr:8787", api_key="key", name="Readarr")
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    notifier, events = _make_notifier()
    await run_arr_services({"Readarr": client}, notifier)

    assert any("/api/v1/health" in u for u in probed_urls)


# ---------------------------------------------------------------------------
# Container recovery (0.5.2): restart exited / threshold-restart unreachable
# ---------------------------------------------------------------------------


_BASE = datetime(2026, 5, 1, 6, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_arr_app_default_container_name_is_lowercase():
    """ArrAppConfig.container_name defaults to name.lower() when not provided."""
    cfg = ArrAppConfig(url="http://localhost:8989", api_key="k", name="Sonarr")
    assert cfg.effective_container_name == "sonarr"


@pytest.mark.asyncio
async def test_arr_app_container_name_override():
    """ArrAppConfig.container_name override is honored."""
    cfg = ArrAppConfig(
        url="http://localhost:8788",
        api_key="k",
        name="Readarr",
        container_name="readarr-audiobooks",
    )
    assert cfg.effective_container_name == "readarr-audiobooks"


@pytest.mark.asyncio
async def test_exited_container_triggers_restart():
    """Container exited + service unreachable -> restart, emit arr.restarted."""
    client = _make_client("Bookshelf", connect_error=True, container_name="bookshelf")
    dm = _make_docker_multi(
        {"bookshelf": _container(name="bookshelf", status="exited", exit_code=255)}
    )
    notifier, events = _make_notifier()
    state = ArrServicesState()

    results = await run_arr_services(
        {"Bookshelf": client}, notifier, docker_manager=dm, state=state
    )

    dm.restart.assert_awaited_once_with("bookshelf")
    assert any(e["event"] == "arr.restarted" for e in events)
    restart_event = next(e for e in events if e["event"] == "arr.restarted")
    assert restart_event["payload"]["name"] == "Bookshelf"
    assert restart_event["payload"]["container_name"] == "bookshelf"
    assert results[0]["last_action"] == "restarted_after_exit"


@pytest.mark.asyncio
async def test_running_unreachable_grace_first_tick():
    """Container running but unreachable -> no restart on first tick, counter increments."""
    client = _make_client("Sonarr", connect_error=True, container_name="sonarr")
    dm = _make_docker_multi(
        {"sonarr": _container(name="sonarr", status="running", started_at=_BASE)}
    )
    notifier, events = _make_notifier()
    state = ArrServicesState()

    await run_arr_services(
        {"Sonarr": client},
        notifier,
        docker_manager=dm,
        state=state,
        running_unreachable_threshold=3,
    )

    dm.restart.assert_not_called()
    assert state.consecutive_unreachable["Sonarr"] == 1


@pytest.mark.asyncio
async def test_running_unreachable_consecutive_threshold_triggers_restart():
    """N consecutive running-but-unreachable ticks -> restart with threshold event."""
    client = _make_client("Sonarr", connect_error=True, container_name="sonarr")
    dm = _make_docker_multi(
        {"sonarr": _container(name="sonarr", status="running", started_at=_BASE)}
    )
    notifier, events = _make_notifier()
    state = ArrServicesState()

    # tick 1
    await run_arr_services(
        {"Sonarr": client},
        notifier,
        docker_manager=dm,
        state=state,
        running_unreachable_threshold=2,
    )
    dm.restart.assert_not_called()
    # tick 2 hits threshold
    await run_arr_services(
        {"Sonarr": client},
        notifier,
        docker_manager=dm,
        state=state,
        running_unreachable_threshold=2,
    )
    dm.restart.assert_awaited_once_with("sonarr")
    assert any(e["event"] == "arr.unreachable_threshold_restart" for e in events)
    assert state.consecutive_unreachable["Sonarr"] == 0


@pytest.mark.asyncio
async def test_alive_resets_consecutive_counter():
    """Successful probe after failures resets the counter."""
    sick = _make_client("Sonarr", connect_error=True, container_name="sonarr")
    well = _make_client("Sonarr", status_code=200, body=[], container_name="sonarr")
    dm = _make_docker_multi(
        {"sonarr": _container(name="sonarr", status="running", started_at=_BASE)}
    )
    notifier, _events = _make_notifier()
    state = ArrServicesState()

    await run_arr_services(
        {"Sonarr": sick},
        notifier,
        docker_manager=dm,
        state=state,
        running_unreachable_threshold=5,
    )
    assert state.consecutive_unreachable["Sonarr"] == 1

    await run_arr_services(
        {"Sonarr": well},
        notifier,
        docker_manager=dm,
        state=state,
        running_unreachable_threshold=5,
    )
    assert state.consecutive_unreachable.get("Sonarr", 0) == 0


@pytest.mark.asyncio
async def test_container_not_found_no_restart():
    """If container can't be found via docker, log and don't crash or restart."""
    client = _make_client("Sonarr", connect_error=True, container_name="sonarr")
    dm = _make_docker_multi({})  # no containers
    notifier, events = _make_notifier()
    state = ArrServicesState()

    results = await run_arr_services(
        {"Sonarr": client}, notifier, docker_manager=dm, state=state
    )

    dm.restart.assert_not_called()
    assert results[0]["last_action"] == "container_not_found"


@pytest.mark.asyncio
async def test_restart_failure_emits_event_and_continues():
    """Restart raising (e.g. NFS mount fails) -> emit arr.restart_failed, no crash."""
    client = _make_client("Bookshelf", connect_error=True, container_name="bookshelf")
    dm = _make_docker_multi(
        {"bookshelf": _container(name="bookshelf", status="exited", exit_code=255)}
    )
    dm.restart = AsyncMock(
        side_effect=RuntimeError("failed to mount volume MediaMegaCityNFS")
    )
    notifier, events = _make_notifier()
    state = ArrServicesState()

    results = await run_arr_services(
        {"Bookshelf": client}, notifier, docker_manager=dm, state=state
    )

    assert any(e["event"] == "arr.restart_failed" for e in events)
    failure = next(e for e in events if e["event"] == "arr.restart_failed")
    assert "MediaMegaCityNFS" in failure["payload"]["error"]
    assert results[0]["last_action"] == "restart_failed"


@pytest.mark.asyncio
async def test_restart_cooldown_prevents_hammering():
    """A second exited tick within cooldown does NOT issue a second restart."""
    client = _make_client("Bookshelf", connect_error=True, container_name="bookshelf")
    dm = _make_docker_multi(
        {"bookshelf": _container(name="bookshelf", status="exited", exit_code=255)}
    )
    dm.restart = AsyncMock(side_effect=RuntimeError("nfs busy"))
    notifier, _events = _make_notifier()
    state = ArrServicesState()

    # First tick: attempts restart (and fails)
    await run_arr_services(
        {"Bookshelf": client},
        notifier,
        docker_manager=dm,
        state=state,
        restart_cooldown=timedelta(minutes=15),
    )
    assert dm.restart.await_count == 1

    # Second tick immediately after: in cooldown, should NOT call restart again
    await run_arr_services(
        {"Bookshelf": client},
        notifier,
        docker_manager=dm,
        state=state,
        restart_cooldown=timedelta(minutes=15),
    )
    assert dm.restart.await_count == 1  # unchanged


@pytest.mark.asyncio
async def test_health_state_records_per_service_snapshot():
    """When health_state passed, snapshot per service is recorded."""
    sonarr = _make_client("Sonarr", status_code=200, body=[], container_name="sonarr")
    bookshelf = _make_client(
        "Bookshelf", connect_error=True, container_name="bookshelf"
    )
    dm = _make_docker_multi(
        {
            "sonarr": _container(name="sonarr", status="running"),
            "bookshelf": _container(name="bookshelf", status="exited", exit_code=255),
        }
    )
    notifier, _events = _make_notifier()
    state = ArrServicesState()
    health_state = HealthState()

    await run_arr_services(
        {"Sonarr": sonarr, "Bookshelf": bookshelf},
        notifier,
        docker_manager=dm,
        state=state,
        health_state=health_state,
    )

    snap = health_state.snapshot()
    assert "arr_services" in snap
    services = {s["name"]: s for s in snap["arr_services"]}
    assert services["Sonarr"]["status"] == "ok"
    assert services["Bookshelf"]["status"] == "down"
    assert services["Bookshelf"]["last_action"] == "restarted_after_exit"
