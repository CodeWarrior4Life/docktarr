"""Tests for artwork_health — the Kodi (XBMC) / Emby consumer-drift guard +
artwork presence spot-check (born from the 2026-07-15 blank-artwork incident).
"""

from __future__ import annotations

import httpx
import pytest

from docktarr.arrclient import ArrClient
from docktarr.artwork_health import (
    ArtworkHealthConfig,
    ArtworkHealthState,
    _find_xbmc_consumer,
    _image_fields_ok,
    run_artwork_health,
)
from docktarr.config import ArrAppConfig
from docktarr.http_health import HealthState
from docktarr.notifier import Notifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _consumer(
    *,
    enable: bool,
    implementation: str = "XbmcMetadata",
    fields: list[dict] | None = None,
    cid: int = 1,
) -> dict:
    return {
        "id": cid,
        "name": "Kodi (XBMC) / Emby",
        "implementation": implementation,
        "enable": enable,
        "fields": fields if fields is not None else [],
    }


def _make_client(
    *,
    consumers: list[dict],
    items: list[dict] | None = None,
    put_recorder: list[dict] | None = None,
    command_recorder: list[dict] | None = None,
    metadata_fail: bool = False,
    put_fail: bool = False,
    name: str = "Sonarr",
) -> ArrClient:
    """ArrClient wired to a MockTransport serving /api/v3/metadata (+PUT),
    /api/v3/series | /api/v3/movie, and /api/v3/command."""
    items = items or []
    endpoint = "series" if name == "Sonarr" else "movie"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/api/v3/metadata") and method == "GET":
            if metadata_fail:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=consumers)
        if "/api/v3/metadata/" in path and method == "PUT":
            if put_fail:
                return httpx.Response(500, json={"error": "boom"})
            import json as _json

            if put_recorder is not None:
                put_recorder.append(_json.loads(request.content))
            return httpx.Response(202, json=_json.loads(request.content))
        if path.endswith(f"/api/v3/{endpoint}") and method == "GET":
            return httpx.Response(200, json=items)
        if path.endswith("/api/v3/command") and method == "POST":
            import json as _json

            if command_recorder is not None:
                command_recorder.append(_json.loads(request.content))
            return httpx.Response(201, json={"id": 99})
        return httpx.Response(404)

    cfg = ArrAppConfig(url="http://arr:8989", api_key="key", name=name)
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _make_notifier() -> tuple[Notifier, list[dict]]:
    events: list[dict] = []

    class CapturingNotifier(Notifier):
        async def emit(self, event: str, payload: dict) -> None:
            events.append({"event": event, "payload": payload})

    n = CapturingNotifier(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))),
        webhook_url=None,
        enabled_events=[],
    )
    return n, events


class _FakeDockerManager:
    """docker exec double: ``present_paths`` are folders whose expected
    artwork files exist. ``test -f {folder}/poster.jpg`` returns 0 iff the
    folder is present."""

    def __init__(self, present_paths: set[str]):
        self.present_paths = present_paths
        self.calls: list[str] = []

    async def exec_run(self, name, cmd, *, user=None, timeout=30.0):
        tokens = cmd.split() if isinstance(cmd, str) else list(cmd)
        # test -f <folder>/<file>
        if tokens[:2] == ["test", "-f"]:
            target = tokens[2]
            self.calls.append(target)
            folder = target.rsplit("/", 1)[0]
            return (0, "") if folder in self.present_paths else (1, "")
        return (1, "")


def _cfg(**kw) -> ArtworkHealthConfig:
    base = dict(enabled=True, presence_check=False, auto_heal=True)
    base.update(kw)
    return ArtworkHealthConfig(**base)


# ---------------------------------------------------------------------------
# Pure discriminators
# ---------------------------------------------------------------------------


def test_find_xbmc_consumer_picks_by_implementation():
    consumers = [
        _consumer(enable=True, implementation="MediaBrowserMetadata", cid=1),
        _consumer(enable=False, implementation="XbmcMetadata", cid=2),
    ]
    c = _find_xbmc_consumer(consumers)
    assert c is not None and c["id"] == 2


def test_find_xbmc_consumer_absent_returns_none():
    assert (
        _find_xbmc_consumer([_consumer(enable=True, implementation="Roksbox")]) is None
    )


def test_image_fields_ok_all_true():
    fields = [
        {"name": "seriesImages", "value": True},
        {"name": "seasonImages", "value": True},
        {"name": "episodeImages", "value": True},
    ]
    assert _image_fields_ok(_consumer(enable=True, fields=fields), "Sonarr") is True


def test_image_fields_ok_one_false():
    fields = [
        {"name": "seriesImages", "value": True},
        {"name": "seasonImages", "value": False},
    ]
    assert _image_fields_ok(_consumer(enable=True, fields=fields), "Sonarr") is False


# ---------------------------------------------------------------------------
# (A) consumer-drift guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_consumer_alerts_and_reenables():
    put_recorder: list[dict] = []
    client = _make_client(
        consumers=[_consumer(enable=False, cid=7)], put_recorder=put_recorder
    )
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client], config=_cfg(auto_heal=True), notifier=notifier
    )
    r = reports[0]
    assert r.consumer_enabled is True
    assert r.consumer_reenabled is True
    assert r.last_action == "healed"
    # PUT preserved the full object, with enable flipped to True.
    assert len(put_recorder) == 1
    assert put_recorder[0]["id"] == 7
    assert put_recorder[0]["enable"] is True
    assert put_recorder[0]["implementation"] == "XbmcMetadata"
    kinds = [e["event"] for e in events]
    assert "artwork_health.consumer_disabled" in kinds
    assert "artwork_health.consumer_reenabled" in kinds


@pytest.mark.asyncio
async def test_disabled_consumer_alert_only_when_auto_heal_off():
    put_recorder: list[dict] = []
    client = _make_client(
        consumers=[_consumer(enable=False)], put_recorder=put_recorder
    )
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client], config=_cfg(auto_heal=False), notifier=notifier
    )
    r = reports[0]
    assert r.consumer_reenabled is False
    assert put_recorder == []  # no PUT when auto-heal off
    kinds = [e["event"] for e in events]
    assert "artwork_health.consumer_disabled" in kinds
    assert "artwork_health.consumer_reenabled" not in kinds


@pytest.mark.asyncio
async def test_enabled_consumer_no_action():
    put_recorder: list[dict] = []
    client = _make_client(consumers=[_consumer(enable=True)], put_recorder=put_recorder)
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client], config=_cfg(), notifier=notifier
    )
    r = reports[0]
    assert r.consumer_enabled is True
    assert r.consumer_reenabled is False
    assert r.last_action == "ok"
    assert put_recorder == []
    assert events == []


@pytest.mark.asyncio
async def test_reenable_failure_emits_error():
    client = _make_client(consumers=[_consumer(enable=False)], put_fail=True)
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client], config=_cfg(auto_heal=True), notifier=notifier
    )
    r = reports[0]
    assert r.consumer_reenabled is False
    assert r.error is not None
    kinds = [e["event"] for e in events]
    assert "artwork_health.error" in kinds


@pytest.mark.asyncio
async def test_metadata_probe_error_emits_error_and_no_crash():
    client = _make_client(consumers=[], metadata_fail=True)
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client], config=_cfg(), notifier=notifier
    )
    assert reports[0].last_action == "error"
    assert [e for e in events if e["event"] == "artwork_health.error"]


@pytest.mark.asyncio
async def test_missing_consumer_alerts():
    client = _make_client(consumers=[_consumer(enable=True, implementation="Roksbox")])
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client], config=_cfg(), notifier=notifier
    )
    assert reports[0].consumer_found is False
    assert [e for e in events if e["event"] == "artwork_health.consumer_disabled"]


@pytest.mark.asyncio
async def test_image_fields_disabled_alerts_when_enabled():
    fields = [
        {"name": "seriesImages", "value": True},
        {"name": "episodeImages", "value": False},
    ]
    client = _make_client(consumers=[_consumer(enable=True, fields=fields)])
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client],
        config=_cfg(check_image_fields=True),
        notifier=notifier,
    )
    assert reports[0].image_fields_ok is False
    assert [
        e for e in events if e["event"] == "artwork_health.consumer_images_disabled"
    ]


# ---------------------------------------------------------------------------
# (B) presence spot-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_presence_missing_alerts():
    items = [
        {"id": 1, "title": "Show A", "path": "/tv/Show A", "added": "2026-07-10"},
        {"id": 2, "title": "Show B", "path": "/tv/Show B", "added": "2026-07-14"},
    ]
    client = _make_client(consumers=[_consumer(enable=True)], items=items)
    dm = _FakeDockerManager(present_paths={"/tv/Show B"})  # Show A missing art
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client],
        config=_cfg(presence_check=True, presence_sample_size=10),
        notifier=notifier,
        docker_manager=dm,
    )
    r = reports[0]
    assert r.presence_checked == 2
    assert r.presence_missing == 1
    assert "Show A" in r.missing_samples
    missing_events = [
        e for e in events if e["event"] == "artwork_health.artwork_missing"
    ]
    assert len(missing_events) == 1
    assert missing_events[0]["payload"]["missing"] == 1


@pytest.mark.asyncio
async def test_presence_all_present_clean():
    items = [
        {"id": 1, "title": "Show A", "path": "/tv/Show A", "added": "2026-07-10"},
    ]
    client = _make_client(consumers=[_consumer(enable=True)], items=items)
    dm = _FakeDockerManager(present_paths={"/tv/Show A"})
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client],
        config=_cfg(presence_check=True),
        notifier=notifier,
        docker_manager=dm,
    )
    r = reports[0]
    assert r.presence_checked == 1
    assert r.presence_missing == 0
    assert events == []


@pytest.mark.asyncio
async def test_presence_auto_refresh_triggers_command():
    items = [
        {"id": 5, "title": "Show A", "path": "/tv/Show A", "added": "2026-07-10"},
    ]
    command_recorder: list[dict] = []
    client = _make_client(
        consumers=[_consumer(enable=True)],
        items=items,
        command_recorder=command_recorder,
    )
    dm = _FakeDockerManager(present_paths=set())  # everything missing
    notifier, _ = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client],
        config=_cfg(presence_check=True, presence_auto_refresh=True),
        notifier=notifier,
        docker_manager=dm,
    )
    assert reports[0].refresh_triggered == 1
    assert len(command_recorder) == 1
    assert command_recorder[0]["name"] == "RefreshSeries"
    assert command_recorder[0]["seriesIds"] == [5]


@pytest.mark.asyncio
async def test_radarr_presence_uses_movie_endpoint_and_refresh():
    items = [
        {"id": 3, "title": "Movie X", "path": "/movies/Movie X", "added": "2026-07-12"},
    ]
    command_recorder: list[dict] = []
    client = _make_client(
        consumers=[_consumer(enable=True)],
        items=items,
        command_recorder=command_recorder,
        name="Radarr",
    )
    dm = _FakeDockerManager(present_paths=set())
    notifier, _ = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client],
        config=_cfg(presence_check=True, presence_auto_refresh=True),
        notifier=notifier,
        docker_manager=dm,
    )
    assert reports[0].presence_missing == 1
    assert command_recorder[0]["name"] == "RefreshMovie"
    assert command_recorder[0]["movieIds"] == [3]


# ---------------------------------------------------------------------------
# dedup, gating, health-state wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_config_short_circuits():
    client = _make_client(consumers=[_consumer(enable=False)])
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[client],
        config=ArtworkHealthConfig(enabled=False),
        notifier=notifier,
    )
    assert reports == []
    assert events == []


@pytest.mark.asyncio
async def test_alert_deduped_across_ticks_when_no_autoheal():
    # auto_heal off + debounce=1: first tick alerts, second tick (still
    # disabled) is deduped, then it re-arms after the condition clears.
    client = _make_client(consumers=[_consumer(enable=False)])
    notifier, events = _make_notifier()
    state = ArtworkHealthState()
    cfg = _cfg(auto_heal=False, debounce=1)
    await run_artwork_health(
        arr_clients=[client], config=cfg, notifier=notifier, state=state
    )
    await run_artwork_health(
        arr_clients=[client], config=cfg, notifier=notifier, state=state
    )
    disabled = [e for e in events if e["event"] == "artwork_health.consumer_disabled"]
    assert len(disabled) == 1  # deduped on the 2nd tick


@pytest.mark.asyncio
async def test_health_state_records_reports():
    client = _make_client(consumers=[_consumer(enable=True)])
    notifier, _ = _make_notifier()
    hs = HealthState()
    await run_artwork_health(
        arr_clients=[client], config=_cfg(), notifier=notifier, health_state=hs
    )
    assert len(hs.artwork_health) == 1
    snap = hs.snapshot()
    assert "artwork_health" in snap
    assert snap["artwork_health"][0]["service"] == "Sonarr"


@pytest.mark.asyncio
async def test_non_arr_clients_skipped():
    readarr = _make_client(consumers=[_consumer(enable=False)], name="Readarr")
    notifier, events = _make_notifier()
    reports = await run_artwork_health(
        arr_clients=[readarr], config=_cfg(), notifier=notifier
    )
    assert reports == []
    assert events == []
