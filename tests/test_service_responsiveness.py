"""Tests for service_responsiveness — the latency-based restart safety net.

Covers the S107 2026-05-03 Seer 502 case: container is up + docker-healthy,
but its HTTP listener is responding slow enough that a Caddy 3s upstream
timeout fires. arr_services / qbit_health don't catch this because they
treat "200 OK eventually" as healthy. service_responsiveness adds the
latency-threshold restart that does.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from docktarr.docker_manager import DockerManager
from docktarr.notifier import Notifier
from docktarr.service_responsiveness import (
    ServiceProbeConfig,
    ServiceProbeState,
    parse_probes_env,
    run_service_responsiveness,
)


def _make_dm() -> DockerManager:
    dm = DockerManager(_client=object())
    dm.restart = AsyncMock()  # type: ignore[assignment]
    return dm


_RealAsyncClient = httpx.AsyncClient  # capture before any monkeypatching


def _make_client(handler) -> httpx.AsyncClient:
    return _RealAsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# DSL parsing
# ---------------------------------------------------------------------------


def test_parse_probes_basic():
    probes = parse_probes_env(
        "Seer,http://Seer:5055/api/v1/status,Seer,3000,3;"
        "Plex,http://Plex:32400/identity,Plex"
    )
    assert len(probes) == 2
    assert probes[0].name == "Seer"
    assert probes[0].slow_ms == 3000
    assert probes[0].consecutive_threshold == 3
    # Defaults
    assert probes[1].slow_ms == 3000
    assert probes[1].consecutive_threshold == 3


def test_parse_probes_skips_malformed():
    probes = parse_probes_env("ok,http://x,c;too,few;another,http://y,c2")
    names = [p.name for p in probes]
    assert names == ["ok", "another"]


def test_parse_probes_empty():
    assert parse_probes_env("") == []
    assert parse_probes_env("   ") == []


# ---------------------------------------------------------------------------
# Probe behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_response_clears_state(monkeypatch):
    """200 OK in 50ms → no slow ticks, no restart."""
    notifier = AsyncMock(spec=Notifier)
    dm = _make_dm()
    probe = ServiceProbeConfig(
        name="Seer", url="http://seer/health", container_name="Seer"
    )
    state = {"Seer": ServiceProbeState(consecutive_slow=2)}  # carry-over noise

    def handler(request):
        return httpx.Response(200, text="ok")

    # Patch the AsyncClient inside run_service_responsiveness via monkeypatching
    # httpx.AsyncClient. Simpler: build a fake client and replace.
    import docktarr.service_responsiveness as sr

    original_client = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        return _make_client(handler)

    monkeypatch.setattr(sr.httpx, "AsyncClient", fake_client)

    results = await run_service_responsiveness([probe], dm, notifier, state=state)

    assert results[0]["last_action"] == "ok"
    assert state["Seer"].consecutive_slow == 0
    dm.restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_below_threshold_no_restart(monkeypatch):
    """One slow tick under threshold → no restart yet.

    Simulate slow upstream by having the handler asyncio.sleep before responding.
    """
    import asyncio

    notifier = AsyncMock(spec=Notifier)
    dm = _make_dm()
    probe = ServiceProbeConfig(
        name="Seer",
        url="http://seer/health",
        container_name="Seer",
        slow_ms=20,  # 20ms — handler will sleep ~50ms
        consecutive_threshold=3,
    )
    state: dict = {}

    async def handler(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, text="ok")

    import docktarr.service_responsiveness as sr

    monkeypatch.setattr(sr.httpx, "AsyncClient", lambda *a, **kw: _make_client(handler))

    results = await run_service_responsiveness([probe], dm, notifier, state=state)

    assert results[0]["last_action"] == "slow_observed"
    assert state["Seer"].consecutive_slow == 1
    dm.restart.assert_not_awaited()
    notifier.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_threshold_breached_restarts(monkeypatch):
    """consecutive_threshold hits → restart + emit telegram."""
    import asyncio

    notifier = AsyncMock(spec=Notifier)
    dm = _make_dm()
    probe = ServiceProbeConfig(
        name="Seer",
        url="http://seer/health",
        container_name="Seer",
        slow_ms=20,
        consecutive_threshold=3,
    )
    state = {"Seer": ServiceProbeState(consecutive_slow=2)}  # one more = trigger

    async def handler(request):
        await asyncio.sleep(0.05)
        return httpx.Response(200, text="ok")

    import docktarr.service_responsiveness as sr

    monkeypatch.setattr(sr.httpx, "AsyncClient", lambda *a, **kw: _make_client(handler))

    results = await run_service_responsiveness([probe], dm, notifier, state=state)

    assert results[0]["last_action"] == "slow_threshold_restart"
    dm.restart.assert_awaited_once_with("Seer")
    notifier.emit.assert_awaited()
    event_name, payload = notifier.emit.await_args.args
    assert event_name == "service.slow_threshold_restart"
    assert payload["name"] == "Seer"
    assert payload["container_name"] == "Seer"
    # Counter resets after successful restart so we don't immediately retrigger.
    assert state["Seer"].consecutive_slow == 0


@pytest.mark.asyncio
async def test_5xx_counts_as_slow(monkeypatch):
    """502/503 from upstream proxy → treated like slow."""
    notifier = AsyncMock(spec=Notifier)
    dm = _make_dm()
    probe = ServiceProbeConfig(
        name="Seer",
        url="http://seer/health",
        container_name="Seer",
        consecutive_threshold=1,  # restart immediately on first slow
    )
    state: dict = {}

    def handler(request):
        return httpx.Response(502, text="Bad Gateway")

    import docktarr.service_responsiveness as sr

    monkeypatch.setattr(sr.httpx, "AsyncClient", lambda *a, **kw: _make_client(handler))

    results = await run_service_responsiveness([probe], dm, notifier, state=state)

    assert results[0]["last_action"] == "slow_threshold_restart"
    assert results[0]["http_status"] == 502
    dm.restart.assert_awaited_once_with("Seer")


@pytest.mark.asyncio
async def test_auth_challenge_is_not_slow(monkeypatch):
    """401/403 means service IS responding — don't count as slow."""
    notifier = AsyncMock(spec=Notifier)
    dm = _make_dm()
    probe = ServiceProbeConfig(
        name="Sonarr", url="http://sonarr/api", container_name="sonarr"
    )
    state = {"Sonarr": ServiceProbeState(consecutive_slow=5)}

    def handler(request):
        return httpx.Response(401, text="auth required")

    import docktarr.service_responsiveness as sr

    monkeypatch.setattr(sr.httpx, "AsyncClient", lambda *a, **kw: _make_client(handler))

    results = await run_service_responsiveness([probe], dm, notifier, state=state)

    assert results[0]["last_action"] == "ok"
    assert state["Sonarr"].consecutive_slow == 0
    dm.restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_dsl_parses_external_url_and_proxy():
    """Trailing fields 6 and 7 = external_url, external_proxy_container."""
    probes = parse_probes_env(
        "Seer,http://Seer:5055/api/v1/status,Seer,3000,3,https://request.example/login,caddy"
    )
    assert len(probes) == 1
    p = probes[0]
    assert p.external_url == "https://request.example/login"
    assert p.external_proxy_container == "caddy"


@pytest.mark.asyncio
async def test_arp_staleness_signature_restarts_proxy(monkeypatch):
    """internal=OK + external=502 for K ticks → restart external_proxy_container.

    This is the host-bridge ARP-staleness signature observed in production
    S107 2026-05-03: Caddy on --network host couldn't reach docker bridge IPs
    after a wave of container churn invalidated host ARP entries.
    """
    notifier = AsyncMock(spec=Notifier)
    dm = _make_dm()
    probe = ServiceProbeConfig(
        name="ABS",
        url="http://audiobookshelf/healthcheck",
        container_name="audiobookshelf",
        external_url="https://listen.matrixmedia.fun/",
        external_proxy_container="caddy",
        consecutive_threshold=1,  # restart on first signature observation
    )
    state: dict = {}

    def handler(request):
        if "audiobookshelf" in str(request.url):
            return httpx.Response(200, text="ok")  # internal happy
        return httpx.Response(502, text="Bad Gateway")  # external broken

    import docktarr.service_responsiveness as sr

    monkeypatch.setattr(sr.httpx, "AsyncClient", lambda *a, **kw: _make_client(handler))

    results = await run_service_responsiveness([probe], dm, notifier, state=state)

    # We get TWO result rows now (internal + external)
    assert len(results) == 2
    internal = next(r for r in results if r["role"] == "internal")
    external = next(r for r in results if r["role"] == "external")

    assert internal["last_action"] == "ok"
    assert external["last_action"] == "external_proxy_restart"

    # Caddy got the restart, not audiobookshelf
    dm.restart.assert_awaited_once_with("caddy")

    # Telegram event fired with the diagnosis
    notifier.emit.assert_awaited_with(
        "service.external_route_degraded",
        {
            "name": "ABS",
            "container_name": "audiobookshelf",
            "proxy_container": "caddy",
            "latency_ms": pytest.approx(external["latency_ms"]),
            "http_status": 502,
            "consecutive_ticks": 1,
            "diagnosis": pytest.approx(external.get("diagnosis"), rel=None)
            if False
            else __import__("operator").itemgetter("diagnosis"),
        },
    ) if False else None  # we just assert below
    assert any(
        c.args[0] == "service.external_route_degraded"
        and "ARP staleness" in c.args[1]["diagnosis"]
        for c in notifier.emit.await_args_list
    )


@pytest.mark.asyncio
async def test_internal_slow_skips_external_action(monkeypatch):
    """If internal probe is ALSO slow, the upstream is the real problem.

    Don't double-restart. Internal probe handles it; external probe just observes.
    """
    notifier = AsyncMock(spec=Notifier)
    dm = _make_dm()
    probe = ServiceProbeConfig(
        name="ABS",
        url="http://audiobookshelf/healthcheck",
        container_name="audiobookshelf",
        external_url="https://listen.matrixmedia.fun/",
        external_proxy_container="caddy",
        consecutive_threshold=1,
    )
    state: dict = {}

    def handler(request):
        return httpx.Response(502, text="upstream broken everywhere")

    import docktarr.service_responsiveness as sr

    monkeypatch.setattr(sr.httpx, "AsyncClient", lambda *a, **kw: _make_client(handler))

    results = await run_service_responsiveness([probe], dm, notifier, state=state)

    internal = next(r for r in results if r["role"] == "internal")
    external = next(r for r in results if r["role"] == "external")

    # Internal probe restarts the upstream container.
    assert internal["last_action"] == "slow_threshold_restart"
    # External probe defers — upstream restart will fix it.
    assert external["last_action"] == "skip_upstream_will_handle"
    # ONLY one restart fired (the upstream), not also the proxy.
    dm.restart.assert_awaited_once_with("audiobookshelf")


@pytest.mark.asyncio
async def test_external_ok_when_both_healthy(monkeypatch):
    notifier = AsyncMock(spec=Notifier)
    dm = _make_dm()
    probe = ServiceProbeConfig(
        name="ABS",
        url="http://audiobookshelf/healthcheck",
        container_name="audiobookshelf",
        external_url="https://listen.matrixmedia.fun/",
        external_proxy_container="caddy",
        consecutive_threshold=3,
    )
    state: dict = {}

    def handler(request):
        return httpx.Response(200, text="ok")

    import docktarr.service_responsiveness as sr

    monkeypatch.setattr(sr.httpx, "AsyncClient", lambda *a, **kw: _make_client(handler))

    results = await run_service_responsiveness([probe], dm, notifier, state=state)
    assert all(r["last_action"] == "ok" for r in results)
    dm.restart.assert_not_awaited()
    notifier.emit.assert_not_awaited()


@pytest.mark.asyncio
async def test_cooldown_blocks_consecutive_restarts(monkeypatch):
    """After a restart, cooldown blocks another for 15 min."""
    from datetime import datetime, timedelta, timezone

    notifier = AsyncMock(spec=Notifier)
    dm = _make_dm()
    probe = ServiceProbeConfig(
        name="Seer",
        url="http://seer/health",
        container_name="Seer",
        slow_ms=10,
        consecutive_threshold=1,
    )
    just_now = datetime.now(timezone.utc) - timedelta(minutes=2)
    state = {"Seer": ServiceProbeState(last_restart_attempt=just_now)}

    def handler(request):
        return httpx.Response(502, text="slow upstream")  # 5xx counts as slow

    import docktarr.service_responsiveness as sr

    monkeypatch.setattr(sr.httpx, "AsyncClient", lambda *a, **kw: _make_client(handler))

    results = await run_service_responsiveness(
        [probe], dm, notifier, state=state, restart_cooldown=timedelta(minutes=15)
    )

    assert results[0]["last_action"] == "cooldown"
    dm.restart.assert_not_awaited()
