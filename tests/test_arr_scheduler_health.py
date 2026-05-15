"""Tests for arr_scheduler_health — liveness probe + composition with the
command-queue drainer (the *primary* wedge signal, per 0.7.2).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from docktarr.arr_command_queue import (
    ArrCommandQueueConfig,
    run_arr_command_queue,
)
from docktarr.arr_scheduler_health import (
    ArrSchedulerHealthConfig,
    ArrSchedulerHealthReport,
    _build_task_report,
    run_arr_scheduler_health,
)
from docktarr.arrclient import ArrClient
from docktarr.config import ArrAppConfig
from docktarr.http_health import HealthState
from docktarr.notifier import Notifier


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task(
    name: str,
    *,
    interval_minutes: int,
    last_exec_minutes_ago: float,
) -> dict:
    last_exec = _now() - timedelta(minutes=last_exec_minutes_ago)
    return {
        "name": name,
        "interval": interval_minutes,
        "lastExecution": last_exec.isoformat().replace("+00:00", "Z"),
    }


def _make_scheduler_client(
    tasks: list[dict],
    *,
    commands: list[dict] | None = None,
    deleted: list[int] | None = None,
    fail: bool = False,
    name: str = "Sonarr",
) -> ArrClient:
    """Wire ArrClient to a MockTransport that serves both
    /api/v3/system/task and /api/v3/command (the latter for composition
    tests that exercise both probes in sequence)."""
    if commands is None:
        commands = []
    if deleted is None:
        deleted = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/api/v3/system/task") and method == "GET":
            if fail:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json=tasks)
        if path.endswith("/api/v3/command") and method == "GET":
            return httpx.Response(200, json=commands)
        if "/api/v3/command/" in path and method == "DELETE":
            deleted.append(int(path.split("/")[-1]))
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

    n = CapturingNotifier(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))),
        webhook_url=None,
        enabled_events=[],
    )
    return n, events


# ---------------------------------------------------------------------------
# _build_task_report — discriminator
# ---------------------------------------------------------------------------


def test_build_task_report_computes_overdue_ratio():
    t = _task("Rss Sync", interval_minutes=1, last_exec_minutes_ago=56)
    r = _build_task_report(t, now=_now(), wedge_threshold=3.0)
    assert r is not None
    assert r.name == "Rss Sync"
    assert r.interval_minutes == 1
    assert r.overdue_ratio >= 50
    assert r.is_wedged is True


def test_build_task_report_below_threshold_not_wedged():
    t = _task("Rss Sync", interval_minutes=1, last_exec_minutes_ago=2)
    r = _build_task_report(t, now=_now(), wedge_threshold=3.0)
    assert r is not None
    assert r.is_wedged is False


def test_build_task_report_skips_invalid_entries():
    # Missing lastExecution -> None
    assert (
        _build_task_report(
            {"name": "x", "interval": 1}, now=_now(), wedge_threshold=3.0
        )
        is None
    )
    # interval <= 0 -> None
    assert (
        _build_task_report(
            {"name": "x", "interval": 0, "lastExecution": "2026-01-01T00:00:00Z"},
            now=_now(),
            wedge_threshold=3.0,
        )
        is None
    )


# ---------------------------------------------------------------------------
# run_arr_scheduler_health — behavioural
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_wedged_tasks_reports_ok():
    tasks = [
        _task("Rss Sync", interval_minutes=1, last_exec_minutes_ago=0.5),
        _task("Import List Sync", interval_minutes=60, last_exec_minutes_ago=30),
    ]
    client = _make_scheduler_client(tasks)
    notifier, events = _make_notifier()
    reports, force = await run_arr_scheduler_health(
        arr_clients=[client],
        config=ArrSchedulerHealthConfig(),
        notifier=notifier,
    )
    assert len(reports) == 1
    r = reports[0]
    assert r.wedged_count == 0
    assert r.last_action == "ok"
    assert force == set()
    assert events == []


@pytest.mark.asyncio
async def test_critical_task_overdue_flags_wedge_and_emits_event():
    # RssSync 1m interval, last executed 50m ago -> 50x overdue
    tasks = [
        _task("Rss Sync", interval_minutes=1, last_exec_minutes_ago=50),
    ]
    client = _make_scheduler_client(tasks)
    notifier, events = _make_notifier()
    reports, force = await run_arr_scheduler_health(
        arr_clients=[client],
        config=ArrSchedulerHealthConfig(),
        notifier=notifier,
    )
    r = reports[0]
    assert r.wedged_count == 1
    assert r.last_action == "alerted"
    assert force == {"Sonarr"}
    wedged_events = [e for e in events if e["event"] == "arr_scheduler.wedged"]
    assert len(wedged_events) == 1
    assert wedged_events[0]["payload"]["service"] == "Sonarr"
    assert "Rss Sync" in wedged_events[0]["payload"]["offenders"]


@pytest.mark.asyncio
async def test_non_critical_overdue_task_is_filtered():
    # "Backup" overdue 50x — NOT in critical_tasks, must NOT count as wedged.
    tasks = [
        _task("Backup", interval_minutes=1, last_exec_minutes_ago=50),
        _task("Rss Sync", interval_minutes=1, last_exec_minutes_ago=0.5),
    ]
    client = _make_scheduler_client(tasks)
    notifier, events = _make_notifier()
    reports, force = await run_arr_scheduler_health(
        arr_clients=[client],
        config=ArrSchedulerHealthConfig(),
        notifier=notifier,
    )
    r = reports[0]
    # Backup IS reported as a task and is_wedged=True; just not COUNTED
    # in wedged_count because it's outside the critical allowlist.
    backup_report = next(t for t in r.tasks if t.name == "Backup")
    assert backup_report.is_wedged is True
    assert r.wedged_count == 0
    assert r.last_action == "ok"
    assert force == set()
    assert [e for e in events if e["event"] == "arr_scheduler.wedged"] == []


@pytest.mark.asyncio
async def test_http_error_records_error_action():
    client = _make_scheduler_client([], fail=True)
    notifier, events = _make_notifier()
    reports, force = await run_arr_scheduler_health(
        arr_clients=[client],
        config=ArrSchedulerHealthConfig(),
        notifier=notifier,
    )
    r = reports[0]
    assert r.last_action == "error"
    assert r.error is not None
    assert force == set()
    error_events = [e for e in events if e["event"] == "arr_scheduler.error"]
    assert len(error_events) == 1


@pytest.mark.asyncio
async def test_disabled_config_short_circuits():
    client = _make_scheduler_client([])
    notifier, events = _make_notifier()
    reports, force = await run_arr_scheduler_health(
        arr_clients=[client],
        config=ArrSchedulerHealthConfig(enabled=False),
        notifier=notifier,
    )
    assert reports == []
    assert force == set()
    assert events == []


@pytest.mark.asyncio
async def test_health_state_records_scheduler_reports():
    tasks = [_task("Rss Sync", interval_minutes=1, last_exec_minutes_ago=50)]
    client = _make_scheduler_client(tasks)
    notifier, _ = _make_notifier()
    hs = HealthState()
    await run_arr_scheduler_health(
        arr_clients=[client],
        config=ArrSchedulerHealthConfig(),
        notifier=notifier,
        health_state=hs,
    )
    assert len(hs.arr_scheduler) == 1
    snap = hs.snapshot()
    assert "arr_scheduler" in snap
    assert snap["arr_scheduler"][0]["wedged_count"] == 1


# ---------------------------------------------------------------------------
# Composition with arr_command_queue — wedge must force-drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wedge_forces_drain_on_empty_age_queue():
    # 10 stale EpisodeSearch — below count threshold (30), below age threshold
    # by themselves. Should NOT drain on count/age alone. But the scheduler
    # is wedged -> force drain.
    from datetime import timedelta as _td

    def _cmd(cid: int) -> dict:
        queued_at = _now() - _td(minutes=1)
        return {
            "id": cid,
            "name": "EpisodeSearch",
            "status": "queued",
            "trigger": "unspecified",
            "queued": queued_at.isoformat().replace("+00:00", "Z"),
        }

    cmds = [_cmd(i) for i in range(10)]
    tasks = [_task("Rss Sync", interval_minutes=1, last_exec_minutes_ago=50)]
    deleted: list[int] = []
    client = _make_scheduler_client(tasks, commands=cmds, deleted=deleted)
    notifier, events = _make_notifier()

    # 1. liveness probe -> force set
    _, force = await run_arr_scheduler_health(
        arr_clients=[client],
        config=ArrSchedulerHealthConfig(),
        notifier=notifier,
    )
    assert force == {"Sonarr"}

    # 2. queue probe with force_drain_services
    reports = await run_arr_command_queue(
        arr_clients=[client],
        config=ArrCommandQueueConfig(),
        notifier=notifier,
        force_drain_services=force,
    )
    assert len(deleted) == 10
    r = reports[0]
    assert r.drained_count == 10
    assert r.triggered_by == "forced"
    drained = [e for e in events if e["event"] == "arr_command_queue.drained"]
    assert drained[0]["payload"]["triggered_by"] == "forced"


@pytest.mark.asyncio
async def test_wedge_with_no_candidates_emits_event_no_drain():
    # Wedged scheduler but no drain candidates -> we still fire the wedged
    # event (so operators see the alert) but there's nothing to drain.
    tasks = [_task("Rss Sync", interval_minutes=1, last_exec_minutes_ago=50)]
    deleted: list[int] = []
    client = _make_scheduler_client(tasks, commands=[], deleted=deleted)
    notifier, events = _make_notifier()

    _, force = await run_arr_scheduler_health(
        arr_clients=[client],
        config=ArrSchedulerHealthConfig(),
        notifier=notifier,
    )
    reports = await run_arr_command_queue(
        arr_clients=[client],
        config=ArrCommandQueueConfig(),
        notifier=notifier,
        force_drain_services=force,
    )
    assert deleted == []
    assert reports[0].drained_count == 0
    assert reports[0].last_action == "ok"
    # Wedged event still fired, drain event did not.
    wedged = [e for e in events if e["event"] == "arr_scheduler.wedged"]
    drained = [e for e in events if e["event"] == "arr_command_queue.drained"]
    assert len(wedged) == 1
    assert drained == []


# ---------------------------------------------------------------------------
# ArrClient.list_scheduled_tasks — thin transport test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_arrclient_list_scheduled_tasks_returns_list_payload():
    payload = [
        _task("Rss Sync", interval_minutes=1, last_exec_minutes_ago=0.5),
        _task("Backup", interval_minutes=720, last_exec_minutes_ago=400),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/system/task"
        assert request.headers.get("x-api-key") == "k"
        return httpx.Response(200, json=payload)

    cfg = ArrAppConfig(url="http://arr:8989", api_key="k", name="Sonarr")
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await client.list_scheduled_tasks()
    assert len(result) == 2
    assert result[0]["name"] == "Rss Sync"


def test_scheduler_report_to_dict_roundtrip():
    r = ArrSchedulerHealthReport(
        service="Sonarr",
        tasks=[],
        wedged_count=2,
        last_action="alerted",
        error=None,
    )
    d = r.to_dict()
    assert d["service"] == "Sonarr"
    assert d["wedged_count"] == 2
    assert d["last_action"] == "alerted"
    assert d["tasks"] == []
