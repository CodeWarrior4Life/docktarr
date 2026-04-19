"""Tests for arr_services — ARR app reachability checks."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from doctarr.arr_services import run_arr_services
from doctarr.arrclient import ArrClient
from doctarr.config import ArrAppConfig
from doctarr.notifier import Notifier


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
) -> ArrClient:
    """Return an ArrClient backed by a MockTransport."""

    def handler(request: httpx.Request) -> httpx.Response:
        if connect_error:
            raise httpx.ConnectError("Connection refused")
        if body is not None:
            return httpx.Response(status_code, json=body)
        return httpx.Response(status_code)

    cfg = ArrAppConfig(url="http://localhost:8989", api_key="testkey", name=name)
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


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
