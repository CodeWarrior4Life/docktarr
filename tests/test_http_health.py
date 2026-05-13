import pytest
import aiohttp
from docktarr.http_health import HealthServer, HealthState


@pytest.mark.asyncio
async def test_health_endpoint_returns_current_state():
    state = HealthState()
    state.record_hw({"zion": [{"kind": "quicksync", "model": "UHD 630"}]})
    state.record_audit_findings(
        [
            {"container": "Plex", "host": "zion", "status": "aligned", "reason": "ok"},
        ]
    )

    server = HealthServer(state=state)
    body = server.snapshot()
    assert "hw_capability" in body
    assert body["hw_capability"]["zion"][0]["kind"] == "quicksync"
    assert body["media_container_audit"][0]["status"] == "aligned"


def test_health_state_drift_pct_calculation():
    state = HealthState()
    state.record_permission_findings(
        [
            {"path": "/data/Video/Movies", "total": 1000, "drift": 47},
            {"path": "/data/Video/Shows", "total": 500, "drift": 150},
        ]
    )
    snap = state.snapshot()
    perms = {p["path"]: p for p in snap["permissions"]}
    assert perms["/data/Video/Movies"]["drift_pct"] == pytest.approx(4.7, rel=0.01)
    assert perms["/data/Video/Shows"]["drift_pct"] == pytest.approx(30.0, rel=0.01)


@pytest.mark.asyncio
async def test_dc_health_endpoint_returns_recorded_report():
    state = HealthState()
    sample = {
        "ts": "2026-05-12T23:34:00Z",
        "results": [
            {
                "app": "Sonarr",
                "client_id": 1,
                "name": "qBittorrent",
                "host": "gluetun",
                "port": 8082,
                "status": "ok",
                "literal_ip": False,
            }
        ],
    }
    state.record_dc_health(sample)
    assert state.dc_health == sample
    assert state.snapshot()["download_clients"] == sample

    server = HealthServer(state=state, host="127.0.0.1", port=18095)
    await server.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:18095/health/download_clients") as r:
                assert r.status == 200
                assert await r.json() == sample
    finally:
        await server.stop()
