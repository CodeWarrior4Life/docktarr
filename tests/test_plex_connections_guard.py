"""Tests for plex_connections_guard — self-healing Plex discovery addresses."""

from __future__ import annotations

import httpx
import pytest

from docktarr.notifier import Notifier
from docktarr.plex_connections_guard import (
    PlexConnectionsGuardConfig,
    run_plex_connections_guard,
)


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


def _identity_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<MediaContainer size="0" claimed="1" '
        'machineIdentifier="984febb" version="1.40.0.1234"/>'
    )


def _prefs_xml(custom_connections: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<MediaContainer size="2">'
        f'<Setting id="customConnections" label="Custom server access URLs" '
        f'type="text" value="{custom_connections}" default="" />'
        '<Setting id="PublishServerOnPlexOnlineKey" type="bool" value="1" />'
        '</MediaContainer>'
    )


def _build_transport(
    *,
    reachable_hosts: set[str],
    custom_connections_by_host: dict[str, str],
    puts: list[tuple[str, dict]],
) -> httpx.MockTransport:
    """A transport modelling a small Plex fleet.

    - ``reachable_hosts``: hosts that answer GET /identity (others ConnectError).
    - ``custom_connections_by_host``: host -> the customConnections value that
      host's GET /:/prefs returns.
    - ``puts``: list mutated in place to record (host, params) of every PUT.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if request.method == "PUT" and request.url.path == "/:/prefs":
            puts.append((host, dict(request.url.params)))
            return httpx.Response(200)
        if host not in reachable_hosts:
            raise httpx.ConnectError("unreachable", request=request)
        if request.url.path == "/identity":
            return httpx.Response(200, text=_identity_xml())
        if request.url.path == "/:/prefs":
            return httpx.Response(
                200, text=_prefs_xml(custom_connections_by_host.get(host, ""))
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dead_published_address_autofix_corrects_and_emits():
    """Server reachable at new IP but publishes the OLD dead IP -> heal it.

    Mirrors the real incident: Cypher (10.0.0.111) is live but its
    customConnections still points at dead Zion (10.0.0.16, with a trailing
    space). auto_fix should PUT the new address + toggle re-publish, and emit
    ``corrected``.
    """
    puts: list[tuple[str, dict]] = []
    transport = _build_transport(
        reachable_hosts={"10.0.0.111"},  # Zion (10.0.0.16) is DEAD
        custom_connections_by_host={
            "10.0.0.111": "http://10.0.0.16:32400 "  # note trailing space
        },
        puts=puts,
    )
    http = httpx.AsyncClient(transport=transport)
    cfg = PlexConnectionsGuardConfig(
        endpoints=["http://10.0.0.111:32400"], token="tok", auto_fix=True
    )
    notifier, events = _make_notifier()

    await run_plex_connections_guard(cfg, notifier, http=http)

    # Three PUTs: customConnections, Publish=0, Publish=1 — all to the live host.
    assert len(puts) == 3
    assert all(host == "10.0.0.111" for host, _ in puts)
    assert puts[0][1]["customConnections"] == "http://10.0.0.111:32400"
    assert puts[1][1]["PublishServerOnPlexOnlineKey"] == "0"
    assert puts[2][1]["PublishServerOnPlexOnlineKey"] == "1"

    corrected = [e for e in events if e["event"] == "plex_connections_guard.corrected"]
    assert len(corrected) == 1
    assert corrected[0]["payload"]["endpoint"] == "http://10.0.0.111:32400"
    assert corrected[0]["payload"]["new_customConnections"] == "http://10.0.0.111:32400"
    assert "10.0.0.16" in corrected[0]["payload"]["old_customConnections"]

    await http.aclose()


@pytest.mark.asyncio
async def test_published_address_already_reachable_is_idempotent():
    """If a published URL is already reachable, do nothing — no PUT, no event."""
    puts: list[tuple[str, dict]] = []
    transport = _build_transport(
        reachable_hosts={"10.0.0.111"},
        custom_connections_by_host={
            # already publishes the working address
            "10.0.0.111": "http://10.0.0.111:32400"
        },
        puts=puts,
    )
    http = httpx.AsyncClient(transport=transport)
    cfg = PlexConnectionsGuardConfig(
        endpoints=["http://10.0.0.111:32400"], token="tok", auto_fix=True
    )
    notifier, events = _make_notifier()

    await run_plex_connections_guard(cfg, notifier, http=http)

    assert puts == []
    assert events == []

    await http.aclose()


@pytest.mark.asyncio
async def test_legit_remote_url_among_published_left_alone():
    """A reachable URL anywhere in the published set => no clobber (no churn).

    Even if the endpoint's own address isn't published, as long as SOME
    published URL is reachable we don't fight it.
    """
    puts: list[tuple[str, dict]] = []
    transport = _build_transport(
        # both the endpoint and a "remote" host answer
        reachable_hosts={"10.0.0.111", "10.0.0.50"},
        custom_connections_by_host={
            "10.0.0.111": "http://10.0.0.50:32400"  # reachable, but not E
        },
        puts=puts,
    )
    http = httpx.AsyncClient(transport=transport)
    cfg = PlexConnectionsGuardConfig(
        endpoints=["http://10.0.0.111:32400"], token="tok", auto_fix=True
    )
    notifier, events = _make_notifier()

    await run_plex_connections_guard(cfg, notifier, http=http)

    assert puts == []
    assert events == []

    await http.aclose()


@pytest.mark.asyncio
async def test_autofix_disabled_emits_stale_no_put():
    """auto_fix=False -> emit stale alert, issue no PUT."""
    puts: list[tuple[str, dict]] = []
    transport = _build_transport(
        reachable_hosts={"10.0.0.111"},
        custom_connections_by_host={"10.0.0.111": "http://10.0.0.16:32400"},
        puts=puts,
    )
    http = httpx.AsyncClient(transport=transport)
    cfg = PlexConnectionsGuardConfig(
        endpoints=["http://10.0.0.111:32400"], token="tok", auto_fix=False
    )
    notifier, events = _make_notifier()

    await run_plex_connections_guard(cfg, notifier, http=http)

    assert puts == []
    stale = [e for e in events if e["event"] == "plex_connections_guard.stale"]
    assert len(stale) == 1
    assert stale[0]["payload"]["endpoint"] == "http://10.0.0.111:32400"
    assert stale[0]["payload"]["expected"] == "http://10.0.0.111:32400"
    assert "corrected" not in {e["event"].split(".")[-1] for e in events}

    await http.aclose()


@pytest.mark.asyncio
async def test_server_unreachable_is_skipped_no_crash():
    """An unreachable endpoint is skipped silently — no PUT, no event, no raise."""
    puts: list[tuple[str, dict]] = []
    transport = _build_transport(
        reachable_hosts=set(),  # nothing reachable
        custom_connections_by_host={},
        puts=puts,
    )
    http = httpx.AsyncClient(transport=transport)
    cfg = PlexConnectionsGuardConfig(
        endpoints=["http://10.0.0.16:32400", "http://10.0.0.111:32400"],
        token="tok",
        auto_fix=True,
    )
    notifier, events = _make_notifier()

    result = await run_plex_connections_guard(cfg, notifier, http=http)

    assert result["results"] == []
    assert puts == []
    assert events == []

    await http.aclose()


@pytest.mark.asyncio
async def test_empty_custom_connections_autofixed():
    """Live server with an EMPTY customConnections is drift too — heal it."""
    puts: list[tuple[str, dict]] = []
    transport = _build_transport(
        reachable_hosts={"10.0.0.111"},
        custom_connections_by_host={"10.0.0.111": ""},
        puts=puts,
    )
    http = httpx.AsyncClient(transport=transport)
    cfg = PlexConnectionsGuardConfig(
        endpoints=["http://10.0.0.111:32400"], token="tok", auto_fix=True
    )
    notifier, events = _make_notifier()

    await run_plex_connections_guard(cfg, notifier, http=http)

    assert len(puts) == 3
    corrected = [e for e in events if e["event"] == "plex_connections_guard.corrected"]
    assert len(corrected) == 1
    assert corrected[0]["payload"]["old_customConnections"] == "<empty>"

    await http.aclose()


def test_default_config_matches_singleton_defaults():
    cfg = PlexConnectionsGuardConfig()
    assert cfg.endpoints == ["http://10.0.0.16:32400", "http://10.0.0.111:32400"]
    assert cfg.enabled is True
    assert cfg.auto_fix is True
