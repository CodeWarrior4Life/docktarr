"""Tests for vpn_health — VPN health check with PIA CA-only region enforcement."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from docktarr.docker_manager import ContainerInfo
from docktarr.notifier import Notifier
from docktarr.vpn_health import VpnHealthConfig, run_vpn_health


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = VpnHealthConfig(
    container_name="gluetun",
    healthcheck_url="http://gluetun:8888/v1/openvpn/status",
    allowed_regions=["CA Toronto", "CA Montreal", "CA Vancouver"],
    require_port_forwarding=True,
)


def _gluetun_response(
    *,
    region: str = "CA Toronto",
    port_forwarded: int | bool = 54321,
    public_ip: str = "203.0.113.4",
) -> dict:
    return {"public_ip": public_ip, "port_forwarded": port_forwarded, "region": region}


def _make_http_client(status_json: dict | None = None, *, status_code: int = 200) -> httpx.AsyncClient:
    """Return an AsyncClient backed by a MockTransport that returns the given JSON."""

    def handler(request: httpx.Request) -> httpx.Response:
        if status_json is None:
            return httpx.Response(status_code)
        return httpx.Response(status_code, json=status_json)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_http_client_error() -> httpx.AsyncClient:
    """AsyncClient that raises ConnectError on every request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _make_container(
    *,
    status: str = "running",
    exit_code: int | None = None,
    name: str = "gluetun",
) -> ContainerInfo:
    return ContainerInfo(
        name=name,
        status=status,
        image="qmcgaw/gluetun:latest",
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
    """Return a DockerManager mock."""
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
async def test_healthy_ca_region_with_port_forwarding():
    """VPN healthy + CA region + port forwarding active → no action, no events."""
    http = _make_http_client(_gluetun_response(region="CA Toronto", port_forwarded=54321))
    dm = _make_docker(_make_container(status="running"))
    notifier, events = _make_notifier()

    await run_vpn_health(http, dm, notifier, _DEFAULT_CONFIG)

    dm.restart.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_us_region_emits_vpn_degraded():
    """VPN healthy + US region → emit vpn.degraded (PIA Pattern 6)."""
    http = _make_http_client(_gluetun_response(region="US New York", port_forwarded=54321))
    dm = _make_docker(_make_container(status="running"))
    notifier, events = _make_notifier()

    await run_vpn_health(http, dm, notifier, _DEFAULT_CONFIG)

    dm.restart.assert_not_called()
    assert len(events) == 1
    assert events[0]["event"] == "vpn.degraded"
    assert events[0]["payload"]["reason"] == "wrong_region"
    assert events[0]["payload"]["region"] == "US New York"


@pytest.mark.asyncio
async def test_ca_region_no_port_forwarding_emits_vpn_degraded():
    """VPN healthy + CA region + port forwarding inactive → emit vpn.degraded."""
    http = _make_http_client(_gluetun_response(region="CA Montreal", port_forwarded=0))
    dm = _make_docker(_make_container(status="running"))
    notifier, events = _make_notifier()

    await run_vpn_health(http, dm, notifier, _DEFAULT_CONFIG)

    dm.restart.assert_not_called()
    assert len(events) == 1
    assert events[0]["event"] == "vpn.degraded"
    assert events[0]["payload"]["reason"] == "no_port_forwarding"
    assert events[0]["payload"]["region"] == "CA Montreal"


@pytest.mark.asyncio
async def test_container_exited_triggers_restart():
    """Gluetun container exited → restart + emit vpn.restarted."""
    http = _make_http_client()  # should never be called
    dm = _make_docker(_make_container(status="exited", exit_code=1))
    notifier, events = _make_notifier()

    await run_vpn_health(http, dm, notifier, _DEFAULT_CONFIG)

    dm.restart.assert_awaited_once_with("gluetun")
    assert len(events) == 1
    assert events[0]["event"] == "vpn.restarted"
    assert events[0]["payload"]["container_name"] == "gluetun"
    assert "not running" in events[0]["payload"]["reason"]


@pytest.mark.asyncio
async def test_container_not_found_no_action():
    """Container lookup failure → log + no restart, no events."""
    http = _make_http_client()
    dm = _make_docker(None)
    notifier, events = _make_notifier()

    await run_vpn_health(http, dm, notifier, _DEFAULT_CONFIG)

    dm.restart.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_gluetun_http_unreachable_no_crash():
    """HTTP probe connection error → log + no events (can't assess state)."""
    http = _make_http_client_error()
    dm = _make_docker(_make_container(status="running"))
    notifier, events = _make_notifier()

    await run_vpn_health(http, dm, notifier, _DEFAULT_CONFIG)

    dm.restart.assert_not_called()
    assert events == []


@pytest.mark.asyncio
async def test_allowed_regions_via_config():
    """allowed_regions is respected from config — non-default list works."""
    custom_config = VpnHealthConfig(
        container_name="gluetun",
        healthcheck_url="http://gluetun:8888/v1/openvpn/status",
        allowed_regions=["DE Frankfurt", "NL Amsterdam"],
        require_port_forwarding=True,
    )
    # DE Frankfurt is allowed by this custom config — should be healthy
    http_de = _make_http_client(_gluetun_response(region="DE Frankfurt", port_forwarded=55000))
    dm = _make_docker(_make_container(status="running"))
    notifier, events = _make_notifier()

    await run_vpn_health(http_de, dm, notifier, custom_config)

    assert events == [], "DE Frankfurt should be healthy with custom allowed_regions"

    # CA Toronto would be wrong for this config
    http_ca = _make_http_client(_gluetun_response(region="CA Toronto", port_forwarded=55000))
    dm2 = _make_docker(_make_container(status="running"))
    notifier2, events2 = _make_notifier()

    await run_vpn_health(http_ca, dm2, notifier2, custom_config)

    assert len(events2) == 1
    assert events2[0]["event"] == "vpn.degraded"
    assert events2[0]["payload"]["reason"] == "wrong_region"


@pytest.mark.asyncio
async def test_port_forwarding_not_required():
    """When require_port_forwarding=False, inactive port does not emit degraded."""
    config = VpnHealthConfig(
        container_name="gluetun",
        healthcheck_url="http://gluetun:8888/v1/openvpn/status",
        allowed_regions=["CA Toronto"],
        require_port_forwarding=False,
    )
    http = _make_http_client(_gluetun_response(region="CA Toronto", port_forwarded=0))
    dm = _make_docker(_make_container(status="running"))
    notifier, events = _make_notifier()

    await run_vpn_health(http, dm, notifier, config)

    assert events == []


@pytest.mark.asyncio
async def test_region_check_is_case_insensitive():
    """Region comparison is case-insensitive."""
    http = _make_http_client(_gluetun_response(region="ca toronto", port_forwarded=54321))
    dm = _make_docker(_make_container(status="running"))
    notifier, events = _make_notifier()

    await run_vpn_health(http, dm, notifier, _DEFAULT_CONFIG)

    assert events == [], "Lowercase 'ca toronto' should match 'CA Toronto' in allowed list"
