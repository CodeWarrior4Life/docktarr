"""Tests for qbit_health — ported qBittorrent health-check logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from docktarr.docker_manager import ContainerInfo
from docktarr.http_health import HealthState
from docktarr.notifier import Notifier
from docktarr.qbittorrent import QBitClient
from docktarr.qbit_health import QbitHealthConfig, QbitHealthState, run_qbit_health


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_qbit(*, alive: bool) -> QBitClient:
    """Return a QBitClient whose _client is mocked to appear alive or dead."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/auth/login" in path:
            if alive:
                resp = httpx.Response(200, text="Ok.")
                resp.headers["set-cookie"] = "SID=test; path=/"
                return resp
            # Simulate connection refused by returning 500
            return httpx.Response(500, text="")
        if "/app/version" in path:
            return httpx.Response(200, text="5.0.0") if alive else httpx.Response(500)
        return httpx.Response(404)

    client = QBitClient("http://qbit:8082", "user", "pass")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    if alive:
        client._sid = "test"
    return client


def _make_qbit_unreachable() -> QBitClient:
    """QBitClient that raises a connection error on every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    client = QBitClient("http://qbit:8082", "user", "pass")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _make_container(
    *,
    status: str,
    exit_code: int | None,
    name: str = "qbittorrent",
    started_at: datetime | None = None,
) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        status=status,
        image="lscr.io/linuxserver/qbittorrent:latest",
        exit_code=exit_code,
        started_at=started_at,
    )


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


def _make_docker(container: ContainerInfo | None) -> MagicMock:
    """Return a DockerManager mock. Pass None to simulate container not found."""
    dm = MagicMock()
    if container is None:
        dm.get_container = AsyncMock(side_effect=LookupError("not found"))
    else:
        dm.get_container = AsyncMock(return_value=container)
    dm.restart = AsyncMock()
    return dm


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_no_action():
    """When qBit API is reachable, no restart is issued."""
    qbit = _make_qbit(alive=True)
    dm = _make_docker(_make_container(status="running", exit_code=0))
    notifier, events = _make_notifier()
    config = QbitHealthConfig(container_name="qbittorrent")

    await run_qbit_health(qbit, dm, notifier, config)

    dm.restart.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_unreachable_exit_137_triggers_restart():
    """When qBit is unreachable AND container exited with 137, restart is issued."""
    qbit = _make_qbit_unreachable()
    container = _make_container(status="exited", exit_code=137)
    dm = _make_docker(container)
    notifier, events = _make_notifier()
    config = QbitHealthConfig(container_name="qbittorrent")

    await run_qbit_health(qbit, dm, notifier, config)

    dm.restart.assert_awaited_once_with("qbittorrent")
    assert len(events) == 1
    assert events[0]["event"] == "qbit.restarted"
    assert events[0]["payload"]["container_name"] == "qbittorrent"
    assert events[0]["payload"]["exit_code"] == 137


@pytest.mark.asyncio
async def test_unreachable_container_running_no_restart():
    """When qBit is unreachable but container is running, do NOT restart (mid-startup)."""
    qbit = _make_qbit_unreachable()
    container = _make_container(status="running", exit_code=None)
    dm = _make_docker(container)
    notifier, events = _make_notifier()
    config = QbitHealthConfig(container_name="qbittorrent")

    await run_qbit_health(qbit, dm, notifier, config)

    dm.restart.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_unreachable_exit_non_137_no_restart():
    """Exited with a code other than 137 — do not auto-restart."""
    qbit = _make_qbit_unreachable()
    container = _make_container(status="exited", exit_code=1)
    dm = _make_docker(container)
    notifier, events = _make_notifier()
    config = QbitHealthConfig(container_name="qbittorrent")

    await run_qbit_health(qbit, dm, notifier, config)

    dm.restart.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_container_not_found_no_restart():
    """If container cannot be found, log and do not crash or restart."""
    qbit = _make_qbit_unreachable()
    dm = _make_docker(None)  # LookupError
    notifier, events = _make_notifier()
    config = QbitHealthConfig(container_name="qbittorrent")

    await run_qbit_health(qbit, dm, notifier, config)

    dm.restart.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_mam_protected_categories_preserved():
    """The MAM protected_categories default is ['MAM'] and is present on config."""
    config = QbitHealthConfig(container_name="qbittorrent")
    assert "MAM" in config.protected_categories

    # run_qbit_health does not iterate torrents, so no MAM torrent will be touched.
    # Verify restart path does NOT attempt any torrent listing.
    qbit = _make_qbit_unreachable()
    container = _make_container(status="exited", exit_code=137)
    dm = _make_docker(container)
    notifier, events = _make_notifier()

    await run_qbit_health(qbit, dm, notifier, config)

    # Only restart called — no get_torrents
    dm.restart.assert_awaited_once_with("qbittorrent")
    # QBitClient.get_torrents should never have been called
    # (there's no mock on it, so if it were called it would fail with a real HTTP error)
    assert events[0]["event"] == "qbit.restarted"


# ---------------------------------------------------------------------------
# Stale-namespace detection (gluetun restarted after qbit)
# ---------------------------------------------------------------------------


_BASE = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_stale_namespace_triggers_restart():
    """qBit unreachable + gluetun.started_at > qbit.started_at -> restart qbit."""
    qbit_client = _make_qbit_unreachable()
    qbit_container = _make_container(status="running", exit_code=0, started_at=_BASE)
    vpn_container = ContainerInfo(
        name="gluetun",
        status="running",
        image="qmcgaw/gluetun:latest",
        started_at=_BASE + timedelta(minutes=30),
    )
    dm = _make_docker_multi({"qbittorrent": qbit_container, "gluetun": vpn_container})
    notifier, events = _make_notifier()
    state = QbitHealthState()
    config = QbitHealthConfig(
        container_name="qbittorrent", vpn_container_name="gluetun"
    )

    await run_qbit_health(qbit_client, dm, notifier, config, state=state)

    dm.restart.assert_awaited_once_with("qbittorrent")
    assert any(e["event"] == "qbit.stale_namespace_restart" for e in events)
    assert state.consecutive_unreachable == 0


@pytest.mark.asyncio
async def test_vpn_older_than_qbit_does_not_restart_first_tick():
    """Gluetun started BEFORE qbit -> not a stale-namespace case; no restart yet."""
    qbit_client = _make_qbit_unreachable()
    qbit_container = _make_container(
        status="running", exit_code=0, started_at=_BASE + timedelta(minutes=30)
    )
    vpn_container = ContainerInfo(
        name="gluetun",
        status="running",
        image="qmcgaw/gluetun:latest",
        started_at=_BASE,
    )
    dm = _make_docker_multi({"qbittorrent": qbit_container, "gluetun": vpn_container})
    notifier, events = _make_notifier()
    state = QbitHealthState()
    config = QbitHealthConfig(
        container_name="qbittorrent",
        vpn_container_name="gluetun",
        running_unreachable_threshold=3,
    )

    await run_qbit_health(qbit_client, dm, notifier, config, state=state)

    dm.restart.assert_not_called()
    assert state.consecutive_unreachable == 1
    assert events == []


@pytest.mark.asyncio
async def test_consecutive_unreachable_threshold_triggers_restart():
    """After N consecutive running-but-unreachable ticks, restart even without stale namespace."""
    qbit_client = _make_qbit_unreachable()
    qbit_container = _make_container(
        status="running", exit_code=0, started_at=_BASE + timedelta(minutes=30)
    )
    vpn_container = ContainerInfo(
        name="gluetun",
        status="running",
        image="qmcgaw/gluetun:latest",
        started_at=_BASE,  # older than qbit -> stale-namespace path is N/A
    )
    dm = _make_docker_multi({"qbittorrent": qbit_container, "gluetun": vpn_container})
    notifier, events = _make_notifier()
    state = QbitHealthState()
    config = QbitHealthConfig(
        container_name="qbittorrent",
        vpn_container_name="gluetun",
        running_unreachable_threshold=2,
    )

    # Tick 1: counter -> 1, no restart
    await run_qbit_health(qbit_client, dm, notifier, config, state=state)
    assert state.consecutive_unreachable == 1
    dm.restart.assert_not_called()

    # Tick 2: counter -> 2 (== threshold), restart
    await run_qbit_health(qbit_client, dm, notifier, config, state=state)
    dm.restart.assert_awaited_once_with("qbittorrent")
    assert any(e["event"] == "qbit.unreachable_threshold_restart" for e in events)
    assert state.consecutive_unreachable == 0  # reset after restart


@pytest.mark.asyncio
async def test_alive_resets_consecutive_counter():
    """A successful tick after failures resets the counter."""
    qbit_unreachable = _make_qbit_unreachable()
    qbit_alive = _make_qbit(alive=True)
    qbit_container = _make_container(
        status="running", exit_code=0, started_at=_BASE + timedelta(minutes=30)
    )
    vpn_container = ContainerInfo(
        name="gluetun",
        status="running",
        image="qmcgaw/gluetun:latest",
        started_at=_BASE,
    )
    dm = _make_docker_multi({"qbittorrent": qbit_container, "gluetun": vpn_container})
    notifier, _events = _make_notifier()
    state = QbitHealthState()
    config = QbitHealthConfig(
        container_name="qbittorrent",
        vpn_container_name="gluetun",
        running_unreachable_threshold=5,
    )

    await run_qbit_health(qbit_unreachable, dm, notifier, config, state=state)
    assert state.consecutive_unreachable == 1
    await run_qbit_health(qbit_alive, dm, notifier, config, state=state)
    assert state.consecutive_unreachable == 0


@pytest.mark.asyncio
async def test_vpn_container_not_found_falls_back_to_threshold():
    """If the configured VPN container is not found, fall back to consecutive-failure threshold."""
    qbit_client = _make_qbit_unreachable()
    qbit_container = _make_container(status="running", exit_code=0, started_at=_BASE)
    dm = _make_docker_multi({"qbittorrent": qbit_container})  # no gluetun
    notifier, _events = _make_notifier()
    state = QbitHealthState()
    config = QbitHealthConfig(
        container_name="qbittorrent",
        vpn_container_name="gluetun",
        running_unreachable_threshold=2,
    )

    await run_qbit_health(qbit_client, dm, notifier, config, state=state)
    dm.restart.assert_not_called()
    assert state.consecutive_unreachable == 1


@pytest.mark.asyncio
async def test_health_state_records_snapshot():
    """When a HealthState is provided, qbit_health snapshot is published for /health."""
    qbit_client = _make_qbit_unreachable()
    qbit_container = _make_container(status="running", exit_code=0, started_at=_BASE)
    vpn_container = ContainerInfo(
        name="gluetun",
        status="running",
        image="qmcgaw/gluetun:latest",
        started_at=_BASE + timedelta(minutes=30),
    )
    dm = _make_docker_multi({"qbittorrent": qbit_container, "gluetun": vpn_container})
    notifier, _events = _make_notifier()
    state = QbitHealthState()
    health_state = HealthState()
    config = QbitHealthConfig(
        container_name="qbittorrent", vpn_container_name="gluetun"
    )

    await run_qbit_health(
        qbit_client, dm, notifier, config, state=state, health_state=health_state
    )

    snap = health_state.qbit_health
    assert snap is not None
    assert snap["reachable"] is False
    assert snap["container_status"] == "running"
    assert snap["stale_namespace_detected"] is True
    assert snap["last_action"] == "stale_namespace_restart"
    assert "last_check" in snap


@pytest.mark.asyncio
async def test_health_state_appears_in_snapshot():
    """HealthState.snapshot() includes qbit_health when set."""
    health_state = HealthState()
    snap = health_state.snapshot()
    assert "qbit_health" in snap
    assert snap["qbit_health"] is None  # no tick yet

    health_state.record_qbit_health({"reachable": True, "last_action": "ok"})
    snap2 = health_state.snapshot()
    assert snap2["qbit_health"] == {"reachable": True, "last_action": "ok"}
