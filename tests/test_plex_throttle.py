"""Tests for plex_throttle — Plex-aware qBit download cap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from docktarr.http_health import HealthState
from docktarr.notifier import Notifier
from docktarr.plex_api import PlexClient
from docktarr.plex_throttle import (
    PlexThrottleConfig,
    PlexThrottleState,
    STATE_DIRECTPLAY,
    STATE_IDLE,
    STATE_TRANSCODE,
    STATE_UNKNOWN,
    UNLIMITED,
    run_plex_throttle,
)
from docktarr.qbittorrent import QBitClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_XML_IDLE = '<?xml version="1.0"?><MediaContainer size="0"></MediaContainer>'

_XML_DIRECTPLAY_1 = (
    '<?xml version="1.0"?><MediaContainer size="1">'
    '<Video title="Show A"><Player state="playing"/></Video>'
    "</MediaContainer>"
)

_XML_TRANSCODE_1 = (
    '<?xml version="1.0"?><MediaContainer size="1">'
    '<Video title="Show A"><TranscodeSession videoDecision="transcode"/>'
    '<Player state="playing"/></Video></MediaContainer>'
)

_XML_MIXED_3 = (
    '<?xml version="1.0"?><MediaContainer size="3">'
    '<Video title="A"><Player state="playing"/></Video>'
    '<Video title="B"><TranscodeSession videoDecision="transcode"/>'
    '<Player state="playing"/></Video>'
    '<Track title="C"><Player state="playing"/></Track>'
    "</MediaContainer>"
)


def _make_plex(xml_or_exc) -> PlexClient:
    """Return a PlexClient whose http transport returns ``xml_or_exc``.

    If ``xml_or_exc`` is an Exception, every request raises it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(xml_or_exc, Exception):
            raise xml_or_exc
        return httpx.Response(200, text=xml_or_exc)

    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return PlexClient("http://plex:32400", "tok", http=http)


def _make_qbit(*, fail: bool = False) -> tuple[QBitClient, list[int]]:
    """Return a QBitClient that captures setDownloadLimit calls.

    Returns (client, captured_calls). Each captured call is the bytes/sec value.
    """
    captured: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/auth/login" in path:
            resp = httpx.Response(200, text="Ok.")
            resp.headers["set-cookie"] = "SID=t; path=/"
            return resp
        if "setDownloadLimit" in path:
            if fail:
                return httpx.Response(500, text="boom")
            limit = int(
                request.url.params.get("limit") or _form_value(request, "limit")
            )
            captured.append(limit)
            return httpx.Response(200, text="")
        return httpx.Response(404)

    client = QBitClient("http://qbit:8082", "u", "p")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client._sid = "t"
    return client, captured


def _form_value(req: httpx.Request, key: str) -> str:
    body = req.content.decode() if req.content else ""
    for kv in body.split("&"):
        if kv.startswith(f"{key}="):
            return kv.split("=", 1)[1]
    return "0"


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


def _cfg(**overrides) -> PlexThrottleConfig:
    base = dict(
        plex_url="http://plex:32400",
        plex_token="tok",
        interval=timedelta(seconds=30),
        idle_limit_kbps=UNLIMITED,
        directplay_limit_kbps=30_000,
        transcode_limit_kbps=5_000,
        grace=timedelta(seconds=60),
    )
    base.update(overrides)
    return PlexThrottleConfig(**base)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_applies_idle_limit_on_first_run():
    plex = _make_plex(_XML_IDLE)
    qbit, calls = _make_qbit()
    notifier, _ = _make_notifier()
    state = PlexThrottleState()
    health = HealthState()

    await run_plex_throttle(
        plex, qbit, notifier, _cfg(), state=state, health_state=health
    )

    assert calls == [0]  # UNLIMITED -> bytes/sec 0
    assert state.current_limit_kbps == 0
    assert health.plex_throttle is not None
    assert health.plex_throttle["plex_state"] == STATE_IDLE
    assert health.plex_throttle["last_action"] == "applied"


@pytest.mark.asyncio
async def test_directplay_applies_directplay_limit():
    plex = _make_plex(_XML_DIRECTPLAY_1)
    qbit, calls = _make_qbit()
    notifier, events = _make_notifier()
    state = PlexThrottleState()
    health = HealthState()

    await run_plex_throttle(
        plex, qbit, notifier, _cfg(), state=state, health_state=health
    )

    assert calls == [30_000 * 1024]
    assert state.current_limit_kbps == 30_000
    assert health.plex_throttle["plex_state"] == STATE_DIRECTPLAY
    assert health.plex_throttle["active_sessions"] == 1
    assert health.plex_throttle["transcode_sessions"] == 0
    assert any(e["event"] == "plex_throttle.applied" for e in events)


@pytest.mark.asyncio
async def test_transcode_overrides_directplay_classification():
    """Mixed sessions: even with 3 active, 1 transcode -> transcode tier."""
    plex = _make_plex(_XML_MIXED_3)
    qbit, calls = _make_qbit()
    notifier, _ = _make_notifier()
    state = PlexThrottleState()

    await run_plex_throttle(plex, qbit, notifier, _cfg(), state=state)

    assert calls == [5_000 * 1024]
    assert state.current_limit_kbps == 5_000
    assert state.last_state_label == STATE_TRANSCODE


@pytest.mark.asyncio
async def test_transcode_single_session():
    plex = _make_plex(_XML_TRANSCODE_1)
    qbit, calls = _make_qbit()
    notifier, _ = _make_notifier()
    state = PlexThrottleState()

    await run_plex_throttle(plex, qbit, notifier, _cfg(), state=state)

    assert calls == [5_000 * 1024]


@pytest.mark.asyncio
async def test_idempotent_no_qbit_call_when_unchanged():
    """Two ticks in identical state -> only one apply call."""
    plex = _make_plex(_XML_DIRECTPLAY_1)
    qbit, calls = _make_qbit()
    notifier, _ = _make_notifier()
    state = PlexThrottleState()
    health = HealthState()

    await run_plex_throttle(
        plex, qbit, notifier, _cfg(), state=state, health_state=health
    )
    await run_plex_throttle(
        plex, qbit, notifier, _cfg(), state=state, health_state=health
    )

    assert calls == [30_000 * 1024]  # still only one
    assert health.plex_throttle["last_action"] == "noop_unchanged"


@pytest.mark.asyncio
async def test_grace_window_blocks_immediate_lift():
    """Stream ends -> within grace, keep prior cap; after grace, restore idle."""
    cfg = _cfg(grace=timedelta(seconds=60))

    qbit, calls = _make_qbit()
    notifier, _ = _make_notifier()
    state = PlexThrottleState()

    # T=0: directplay active -> apply 30,000
    plex_active = _make_plex(_XML_DIRECTPLAY_1)
    t0 = datetime(2026, 5, 3, 21, 0, 0, tzinfo=timezone.utc)
    await run_plex_throttle(plex_active, qbit, notifier, cfg, state=state, now=t0)
    assert calls == [30_000 * 1024]

    # T=30s: stream ends, but within 60s grace -> hold prior cap (no new apply)
    plex_idle = _make_plex(_XML_IDLE)
    t1 = t0 + timedelta(seconds=30)
    await run_plex_throttle(plex_idle, qbit, notifier, cfg, state=state, now=t1)
    assert calls == [30_000 * 1024]  # unchanged

    # T=120s: past grace -> restore idle limit (UNLIMITED -> 0)
    t2 = t0 + timedelta(seconds=120)
    await run_plex_throttle(plex_idle, qbit, notifier, cfg, state=state, now=t2)
    assert calls == [30_000 * 1024, 0]


@pytest.mark.asyncio
async def test_plex_unreachable_does_not_change_qbit():
    plex = _make_plex(httpx.ConnectError("refused"))
    qbit, calls = _make_qbit()
    notifier, _ = _make_notifier()
    state = PlexThrottleState(current_limit_kbps=12345)  # pretend prior state
    health = HealthState()

    await run_plex_throttle(
        plex, qbit, notifier, _cfg(), state=state, health_state=health
    )

    assert calls == []  # no qBit calls
    assert state.current_limit_kbps == 12345  # unchanged
    assert health.plex_throttle["plex_state"] == STATE_UNKNOWN
    assert health.plex_throttle["last_action"] == "plex_unreachable"
    assert "plex:" in health.plex_throttle["error"]


@pytest.mark.asyncio
async def test_qbit_failure_does_not_crash_and_records_error():
    plex = _make_plex(_XML_DIRECTPLAY_1)
    qbit, _ = _make_qbit(fail=True)
    notifier, _ = _make_notifier()
    state = PlexThrottleState()
    health = HealthState()

    # Should not raise
    await run_plex_throttle(
        plex, qbit, notifier, _cfg(), state=state, health_state=health
    )

    assert state.current_limit_kbps is None  # never applied
    assert health.plex_throttle["last_action"] == "qbit_apply_failed"
    assert "qbit:" in health.plex_throttle["error"]
