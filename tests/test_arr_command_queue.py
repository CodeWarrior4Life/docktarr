"""Tests for arr_command_queue — Sonarr/Radarr /api/v3/command drainer.

Covers the discriminator (status='queued' AND trigger='unspecified' AND
name IN drain_command_names) plus dual thresholds (count + age), the
elevated-warn path, and the per-service error path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from docktarr.arr_command_queue import (
    ArrCommandQueueConfig,
    ArrCommandQueueReport,
    _classify,
    run_arr_command_queue,
)
from docktarr.arrclient import ArrClient
from docktarr.config import ArrAppConfig
from docktarr.http_health import HealthState
from docktarr.notifier import Notifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    """Real wall-clock 'now' — the module uses datetime.now(UTC) internally
    so test fixtures need to anchor on the same clock or age comparisons
    drift unpredictably."""
    return datetime.now(timezone.utc)


def _cmd(
    cid: int,
    *,
    name: str = "EpisodeSearch",
    status: str = "queued",
    trigger: str = "unspecified",
    queued_minutes_ago: float = 30,
) -> dict:
    queued_at = _now() - timedelta(minutes=queued_minutes_ago)
    return {
        "id": cid,
        "name": name,
        "status": status,
        "trigger": trigger,
        "queued": queued_at.isoformat().replace("+00:00", "Z"),
    }


def _make_client(
    commands: list[dict],
    *,
    name: str = "Sonarr",
    deleted: list[int] | None = None,
    fail_list: bool = False,
    fail_delete_ids: set[int] | None = None,
) -> ArrClient:
    """Return an ArrClient wired to a MockTransport over the given commands."""
    fail_delete_ids = fail_delete_ids or set()
    if deleted is None:
        deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/api/v3/command") and method == "GET":
            if fail_list:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=commands)
        if "/api/v3/command/" in path and method == "DELETE":
            cid = int(path.split("/")[-1])
            if cid in fail_delete_ids:
                return httpx.Response(500)
            deleted.append(cid)
            return httpx.Response(200)
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

    transport = httpx.MockTransport(lambda r: httpx.Response(204))
    n = CapturingNotifier(
        httpx.AsyncClient(transport=transport),
        webhook_url=None,
        enabled_events=[],
    )
    return n, events


# ---------------------------------------------------------------------------
# _classify — unit-level discriminator
# ---------------------------------------------------------------------------


def test_classify_partitions_started_queued_and_drain_candidates():
    cmds = [
        _cmd(1, status="started"),
        _cmd(2, status="queued", trigger="unspecified", queued_minutes_ago=20),
        _cmd(3, status="queued", trigger="manual", queued_minutes_ago=20),
        _cmd(4, status="queued", trigger="scheduled", queued_minutes_ago=20),
    ]
    started, queued, drain, oldest = _classify(
        cmds, drain_names={"EpisodeSearch"}, now=_now()
    )
    assert len(started) == 1
    assert len(queued) == 3
    assert len(drain) == 1
    assert drain[0]["id"] == 2
    assert oldest is not None and oldest >= 1200


def test_classify_excludes_non_drain_names():
    # RssSync + ImportListSync with trigger=unspecified must NOT be drain
    # candidates even though they share the trigger.
    cmds = [
        _cmd(1, name="RssSync", trigger="unspecified", queued_minutes_ago=60),
        _cmd(2, name="ImportListSync", trigger="unspecified", queued_minutes_ago=60),
        _cmd(3, name="EpisodeSearch", trigger="unspecified", queued_minutes_ago=60),
    ]
    _, _, drain, _ = _classify(
        cmds,
        drain_names={"EpisodeSearch", "SeasonSearch", "MovieSearch"},
        now=_now(),
    )
    assert [c["id"] for c in drain] == [3]


# ---------------------------------------------------------------------------
# run_arr_command_queue — behavioural tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_queue_reports_zero_and_no_action():
    client = _make_client([])
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    assert len(reports) == 1
    r = reports[0]
    assert r.queued_count == 0
    assert r.started_count == 0
    assert r.drained_count == 0
    assert r.last_action == "ok"
    assert events == []


@pytest.mark.asyncio
async def test_below_count_threshold_no_drain():
    # 10 stale EpisodeSearch — below default 50 threshold.
    deleted: list[int] = []
    cmds = [
        _cmd(i, trigger="unspecified", queued_minutes_ago=30) for i in range(10)
    ]
    client = _make_client(cmds, deleted=deleted)
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    assert deleted == []
    assert reports[0].drained_count == 0
    assert reports[0].last_action == "ok"
    assert events == []


@pytest.mark.asyncio
async def test_count_met_but_age_too_young_no_drain():
    # 60 commands meeting the count threshold, but each queued only 30s ago
    # (default age threshold is 600s).
    deleted: list[int] = []
    cmds = [
        _cmd(i, trigger="unspecified", queued_minutes_ago=0.5) for i in range(60)
    ]
    client = _make_client(cmds, deleted=deleted)
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    assert deleted == []
    assert reports[0].drained_count == 0
    assert reports[0].last_action == "ok"
    # 60 queued does NOT exceed default elevated_warn_count of 200 — no warn.
    assert events == []


@pytest.mark.asyncio
async def test_both_thresholds_met_drain_executes_and_emits_event():
    deleted: list[int] = []
    cmds = [
        _cmd(i, trigger="unspecified", queued_minutes_ago=30) for i in range(60)
    ]
    client = _make_client(cmds, deleted=deleted)
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    assert len(deleted) == 60
    r = reports[0]
    assert r.drained_count == 60
    assert r.last_action == "drained"
    assert len(r.sample_drained_ids) == 3
    assert len(events) == 1
    assert events[0]["event"] == "arr_command_queue.drained"
    assert events[0]["payload"]["count"] == 60
    assert len(events[0]["payload"]["sample_ids"]) == 3
    assert events[0]["payload"]["service"] == "Sonarr"


@pytest.mark.asyncio
async def test_manual_and_scheduled_commands_preserved():
    # 60 manual + 60 scheduled, all aged out. None should be drained.
    deleted: list[int] = []
    cmds: list[dict] = []
    cmds.extend(
        _cmd(i, trigger="manual", queued_minutes_ago=30) for i in range(60)
    )
    cmds.extend(
        _cmd(100 + i, trigger="scheduled", queued_minutes_ago=30)
        for i in range(60)
    )
    client = _make_client(cmds, deleted=deleted)
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    assert deleted == []
    assert reports[0].drained_count == 0
    assert reports[0].last_action == "ok"
    drained_events = [e for e in events if e["event"] == "arr_command_queue.drained"]
    assert drained_events == []


@pytest.mark.asyncio
async def test_episode_search_drained_rsssync_preserved():
    # Mixed payload: 60 EpisodeSearch (drain), plus RssSync + ImportListSync.
    deleted: list[int] = []
    cmds: list[dict] = []
    cmds.extend(
        _cmd(i, name="EpisodeSearch", trigger="unspecified", queued_minutes_ago=30)
        for i in range(60)
    )
    cmds.append(_cmd(900, name="RssSync", trigger="unspecified", queued_minutes_ago=30))
    cmds.append(
        _cmd(901, name="ImportListSync", trigger="unspecified", queued_minutes_ago=30)
    )
    client = _make_client(cmds, deleted=deleted)
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    # All 60 EpisodeSearch drained, RssSync + ImportListSync untouched.
    assert sorted(deleted) == list(range(60))
    assert 900 not in deleted
    assert 901 not in deleted
    assert reports[0].drained_count == 60


@pytest.mark.asyncio
async def test_elevated_warns_above_200_when_age_threshold_not_met():
    # 250 commands, all only 1 minute old → fails age threshold (600s).
    # Count exceeds elevated_warn_count (200) → emit elevated warning.
    deleted: list[int] = []
    cmds = [
        _cmd(i, trigger="unspecified", queued_minutes_ago=1) for i in range(250)
    ]
    client = _make_client(cmds, deleted=deleted)
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    assert deleted == []  # no drain
    assert reports[0].drained_count == 0
    assert reports[0].last_action == "elevated"
    elevated = [e for e in events if e["event"] == "arr_command_queue.elevated"]
    assert len(elevated) == 1
    assert elevated[0]["payload"]["count"] == 250


@pytest.mark.asyncio
async def test_http_500_on_list_records_error_and_emits_event():
    client = _make_client([], fail_list=True)
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    r = reports[0]
    assert r.last_action == "error"
    assert r.error is not None
    assert r.drained_count == 0
    error_events = [e for e in events if e["event"] == "arr_command_queue.error"]
    assert len(error_events) == 1
    assert error_events[0]["payload"]["service"] == "Sonarr"


# ---------------------------------------------------------------------------
# Auxiliary coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_batches_in_groups_of_16_in_parallel():
    # 100 drain candidates → 7 batches (16+16+16+16+16+16+4). Confirm all
    # deletions land regardless of batching.
    deleted: list[int] = []
    cmds = [
        _cmd(i, trigger="unspecified", queued_minutes_ago=30) for i in range(100)
    ]
    client = _make_client(cmds, deleted=deleted)
    notifier, _ = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    assert len(deleted) == 100
    assert reports[0].drained_count == 100


@pytest.mark.asyncio
async def test_per_delete_failure_does_not_abort_batch():
    deleted: list[int] = []
    cmds = [
        _cmd(i, trigger="unspecified", queued_minutes_ago=30) for i in range(60)
    ]
    # Simulate id=5 and id=42 returning 500 on DELETE.
    client = _make_client(cmds, deleted=deleted, fail_delete_ids={5, 42})
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client], config=ArrCommandQueueConfig(), notifier=notifier
    )
    assert len(deleted) == 58  # 60 attempted, 2 failed
    assert reports[0].drained_count == 58
    drained_events = [
        e for e in events if e["event"] == "arr_command_queue.drained"
    ]
    assert drained_events[0]["payload"]["count"] == 58


@pytest.mark.asyncio
async def test_disabled_config_short_circuits():
    client = _make_client(
        [_cmd(i, trigger="unspecified", queued_minutes_ago=30) for i in range(200)]
    )
    notifier, events = _make_notifier()
    reports = await run_arr_command_queue(
        arr_clients=[client],
        config=ArrCommandQueueConfig(enabled=False),
        notifier=notifier,
    )
    assert reports == []
    assert events == []


@pytest.mark.asyncio
async def test_health_state_records_per_service_reports():
    # 1 Sonarr, 1 Radarr. Sonarr drains, Radarr empty.
    sonarr_deleted: list[int] = []
    radarr_deleted: list[int] = []
    sonarr = _make_client(
        [_cmd(i, trigger="unspecified", queued_minutes_ago=30) for i in range(60)],
        name="Sonarr",
        deleted=sonarr_deleted,
    )
    radarr = _make_client([], name="Radarr", deleted=radarr_deleted)
    notifier, _ = _make_notifier()
    hs = HealthState()
    await run_arr_command_queue(
        arr_clients=[sonarr, radarr],
        config=ArrCommandQueueConfig(),
        notifier=notifier,
        health_state=hs,
    )
    assert len(hs.arr_command_queue) == 2
    services = {r["service"] for r in hs.arr_command_queue}
    assert services == {"Sonarr", "Radarr"}
    snap = hs.snapshot()
    assert "arr_command_queue" in snap
    assert len(snap["arr_command_queue"]) == 2


def test_report_to_dict_roundtrip():
    r = ArrCommandQueueReport(
        service="Sonarr",
        queued_count=10,
        started_count=1,
        oldest_queued_age_seconds=42.5,
        drained_count=5,
        last_action="drained",
        error=None,
        sample_drained_ids=[1, 2, 3],
    )
    d = r.to_dict()
    assert d["service"] == "Sonarr"
    assert d["drained_count"] == 5
    assert d["last_action"] == "drained"
    assert d["sample_drained_ids"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# ArrClient.list_commands / delete_command — thin transport tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arrclient_list_commands_returns_list_payload():
    payload = [_cmd(1), _cmd(2)]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/command"
        assert request.headers.get("x-api-key") == "k"
        return httpx.Response(200, json=payload)

    cfg = ArrAppConfig(url="http://arr:8989", api_key="k", name="Sonarr")
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await client.list_commands()
    assert len(result) == 2
    assert result[0]["id"] == 1


@pytest.mark.asyncio
async def test_arrclient_delete_command_raises_on_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    cfg = ArrAppConfig(url="http://arr:8989", api_key="k", name="Sonarr")
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await client.delete_command(1)
