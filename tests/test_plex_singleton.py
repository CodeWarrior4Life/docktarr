"""Tests for plex_singleton — split-brain Plex detection."""

from __future__ import annotations

import httpx
import pytest

from docktarr.notifier import Notifier
from docktarr.plex_singleton import PlexSingletonConfig, run_plex_singleton


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


def _identity_xml(machine_id: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<MediaContainer size="0" claimed="1" '
        f'machineIdentifier="{machine_id}" version="1.40.0.1234"/>'
    )


def _client_for(host_to_id: dict[str, str | None]) -> httpx.AsyncClient:
    """Build an httpx client whose transport maps host -> machineIdentifier.

    A value of None makes that host appear unreachable (connection error).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        mid = host_to_id.get(host)
        if mid is None:
            raise httpx.ConnectError("unreachable", request=request)
        if request.url.path == "/identity":
            return httpx.Response(200, text=_identity_xml(mid))
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_split_brain_same_id_two_endpoints_fires():
    cfg = PlexSingletonConfig(
        endpoints=["http://10.0.0.16:32400", "http://10.0.0.111:32400"],
        token="tok",
    )
    notifier, events = _make_notifier()
    http = _client_for({"10.0.0.16": "984febb", "10.0.0.111": "984febb"})

    result = await run_plex_singleton(cfg, notifier, http=http)

    assert result["split_brain"] is True
    assert "984febb" in result["collisions"]
    assert len(result["collisions"]["984febb"]) == 2
    assert len(events) == 1
    assert events[0]["event"] == "plex_singleton.split_brain"
    assert events[0]["payload"]["count"] == 2
    assert events[0]["payload"]["machine_identifier"] == "984febb"


@pytest.mark.asyncio
async def test_distinct_ids_stays_silent():
    cfg = PlexSingletonConfig(
        endpoints=["http://10.0.0.16:32400", "http://10.0.0.111:32400"],
        token="tok",
    )
    notifier, events = _make_notifier()
    http = _client_for({"10.0.0.16": "aaa111", "10.0.0.111": "bbb222"})

    result = await run_plex_singleton(cfg, notifier, http=http)

    assert result["split_brain"] is False
    assert result["collisions"] == {}
    assert events == []


@pytest.mark.asyncio
async def test_single_reachable_endpoint_stays_silent():
    """One endpoint up, one down — no collision possible."""
    cfg = PlexSingletonConfig(
        endpoints=["http://10.0.0.16:32400", "http://10.0.0.111:32400"],
        token="tok",
    )
    notifier, events = _make_notifier()
    http = _client_for({"10.0.0.16": "984febb", "10.0.0.111": None})

    result = await run_plex_singleton(cfg, notifier, http=http)

    assert result["split_brain"] is False
    assert result["reachable"] == {"http://10.0.0.16:32400": "984febb"}
    assert events == []


@pytest.mark.asyncio
async def test_all_endpoints_unreachable_stays_silent():
    cfg = PlexSingletonConfig(
        endpoints=["http://10.0.0.16:32400", "http://10.0.0.111:32400"],
        token="tok",
    )
    notifier, events = _make_notifier()
    http = _client_for({"10.0.0.16": None, "10.0.0.111": None})

    result = await run_plex_singleton(cfg, notifier, http=http)

    assert result["reachable"] == {}
    assert result["split_brain"] is False
    assert events == []


@pytest.mark.asyncio
async def test_three_endpoints_two_share_id():
    cfg = PlexSingletonConfig(
        endpoints=[
            "http://10.0.0.16:32400",
            "http://10.0.0.111:32400",
            "http://10.0.0.200:32400",
        ],
        token="tok",
    )
    notifier, events = _make_notifier()
    http = _client_for(
        {"10.0.0.16": "dup", "10.0.0.111": "dup", "10.0.0.200": "unique"}
    )

    result = await run_plex_singleton(cfg, notifier, http=http)

    assert result["split_brain"] is True
    assert set(result["collisions"]["dup"]) == {
        "http://10.0.0.16:32400",
        "http://10.0.0.111:32400",
    }
    assert "unique" not in result["collisions"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_default_endpoints_are_zion_and_cypher():
    cfg = PlexSingletonConfig()
    assert cfg.endpoints == ["http://10.0.0.16:32400", "http://10.0.0.111:32400"]
    assert cfg.enabled is True
