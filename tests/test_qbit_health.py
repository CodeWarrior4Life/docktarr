"""Tests for qbit_health — ported qBittorrent health-check logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from doctarr.docker_manager import ContainerInfo
from doctarr.notifier import Notifier
from doctarr.qbittorrent import QBitClient
from doctarr.qbit_health import QbitHealthConfig, run_qbit_health


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
) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        status=status,
        image="lscr.io/linuxserver/qbittorrent:latest",
        exit_code=exit_code,
    )


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
