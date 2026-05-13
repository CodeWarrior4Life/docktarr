"""Tests for download_client_health — 8 spec scenarios."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from docktarr.docker_manager import ContainerInfo
from docktarr.download_client_health import (
    DownloadClientHealthConfig,
    _classify_host,
    run_download_client_health,
)
from docktarr.notifier import Notifier


def _make_notifier() -> tuple[Notifier, list[dict]]:
    events: list[dict] = []

    class Capturing(Notifier):
        async def emit(self, event, payload):
            events.append({"event": event, "payload": payload})

    transport = httpx.MockTransport(lambda r: httpx.Response(204))
    n = Capturing(
        httpx.AsyncClient(transport=transport), webhook_url=None, enabled_events=[]
    )
    return n, events


def _make_arr_client(
    *,
    name: str = "Sonarr",
    host: str = "gluetun",
    port: int = 8082,
    client_id: int = 1,
    test_result: tuple[bool, int, str] = (True, 200, "{}"),
):
    c = MagicMock()
    c.name = name
    c.container_name = name.lower()
    c.get_download_clients = AsyncMock(
        return_value=[
            {
                "id": client_id,
                "name": "qBittorrent",
                "enable": True,
                "fields": [
                    {"name": "host", "value": host},
                    {"name": "port", "value": port},
                ],
            },
        ]
    )
    c.test_download_client = AsyncMock(return_value=test_result)
    c.put_download_client_host = AsyncMock(return_value=True)
    return c


def _make_docker(
    *, vpn_ip: str | None = "172.29.0.7", vpn_container_name: str = "gluetun"
) -> MagicMock:
    dm = MagicMock()
    if vpn_ip is None:
        dm.get_container = AsyncMock(side_effect=LookupError("not found"))
    else:
        info = ContainerInfo(
            name=vpn_container_name,
            status="running",
            image="qmcgaw/gluetun:latest",
            ip_addresses={"arr_default": vpn_ip},
        )
        dm.get_container = AsyncMock(return_value=info)
    dm.exec_run = AsyncMock(return_value=(0, ""))
    return dm


_DEFAULT_CFG = DownloadClientHealthConfig(vpn_container="gluetun", auto_patch=False)


def test_classify_host_recognizes_dns_literal_ip_unknown():
    assert _classify_host("gluetun") == "dns"
    assert _classify_host("172.29.0.7") == "literal_ip"
    assert _classify_host("gluetun.local") == "unknown"
    assert _classify_host("") == "unknown"


@pytest.mark.asyncio
async def test_scenario_1_all_dns_no_events():
    sonarr = _make_arr_client(name="Sonarr", host="gluetun")
    radarr = _make_arr_client(name="Radarr", host="gluetun")
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    report = await run_download_client_health(
        {"Sonarr": sonarr, "Radarr": radarr},
        docker_manager=dm,
        notifier=notifier,
        config=_DEFAULT_CFG,
    )

    assert events == []
    assert all(r["status"] == "ok" for r in report["results"])
    assert all(r["literal_ip"] is False for r in report["results"])


@pytest.mark.asyncio
async def test_scenario_2_literal_ip_current_emits_literal_ip_with_alias():
    sonarr = _make_arr_client(name="Sonarr", host="172.29.0.7")
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr},
        docker_manager=dm,
        notifier=notifier,
        config=_DEFAULT_CFG,
    )

    assert len(events) == 1
    assert events[0]["event"] == "dc_health.literal_ip"
    assert events[0]["payload"]["host"] == "172.29.0.7"
    assert events[0]["payload"]["suggested_alias"] == "gluetun"


@pytest.mark.asyncio
async def test_scenario_3_stale_literal_ip_emits_both_literal_and_unreachable():
    sonarr = _make_arr_client(
        name="Sonarr",
        host="172.29.0.2",
        test_result=(False, 400, '[{"errorMessage":"Unable to connect"}]'),
    )
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr},
        docker_manager=dm,
        notifier=notifier,
        config=_DEFAULT_CFG,
    )

    event_names = [e["event"] for e in events]
    assert "dc_health.literal_ip" in event_names
    assert "dc_health.unreachable" in event_names
    unreachable = next(e for e in events if e["event"] == "dc_health.unreachable")
    assert unreachable["payload"]["host"] == "172.29.0.2"
    assert "Unable to connect" in unreachable["payload"]["test_response"]


@pytest.mark.asyncio
async def test_scenario_6_qbit_down_emits_unreachable_dns_host():
    sonarr = _make_arr_client(
        name="Sonarr",
        host="gluetun",
        test_result=(
            False,
            400,
            '[{"errorMessage":"Unable to connect to qBittorrent"}]',
        ),
    )
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr},
        docker_manager=dm,
        notifier=notifier,
        config=_DEFAULT_CFG,
    )

    event_names = [e["event"] for e in events]
    assert "dc_health.unreachable" in event_names
    assert "dc_health.literal_ip" not in event_names


@pytest.mark.asyncio
async def test_scenario_7_wrong_creds_emits_unreachable_with_auth_body():
    sonarr = _make_arr_client(
        name="Sonarr",
        host="gluetun",
        test_result=(False, 200, '[{"errorMessage":"Failed to authenticate"}]'),
    )
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr},
        docker_manager=dm,
        notifier=notifier,
        config=_DEFAULT_CFG,
    )

    unreachable = [e for e in events if e["event"] == "dc_health.unreachable"]
    assert len(unreachable) == 1
    assert "authenticate" in unreachable[0]["payload"]["test_response"].lower()
