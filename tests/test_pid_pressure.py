"""Tests for pid_pressure — zombie / PID-pressure watchdog."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from docktarr.notifier import Notifier
from docktarr.pid_pressure import (
    PidPressureConfig,
    PidPressureState,
    _parse_top,
    run_pid_pressure,
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


def _top(rows: list[tuple], titles=("PID", "PPID", "STAT", "COMMAND")) -> dict:
    """Build a docker-top result dict from (pid, ppid, stat, comm) rows."""
    return {"Titles": list(titles), "Processes": [list(r) for r in rows]}


def _make_docker(tops: dict[str, dict]):
    """Fake DockerManager: list_running_containers + top + restart."""
    dm = AsyncMock()
    dm.list_running_containers.return_value = list(tops.keys())

    async def _top_fn(name, ps_args="-eo pid,ppid,stat,comm"):
        return tops[name]

    dm.top.side_effect = _top_fn
    return dm


# ---------------------------------------------------------------------------
# _parse_top
# ---------------------------------------------------------------------------


def test_parse_top_counts_pids_and_zombies():
    top = _top(
        [
            ("1", "0", "Ss", "python"),
            ("42", "1", "Z", "chrome"),
            ("43", "1", "Z", "chrome"),
            ("44", "1", "R", "chrome"),
        ]
    )
    pid_count, zombie_count = _parse_top(top)
    assert pid_count == 4
    assert zombie_count == 2


def test_parse_top_no_stat_column_degrades_to_zero_zombies():
    top = {"Titles": ["PID", "COMMAND"], "Processes": [["1", "python"], ["2", "sh"]]}
    pid_count, zombie_count = _parse_top(top)
    assert pid_count == 2
    assert zombie_count == 0


def test_parse_top_empty():
    assert _parse_top({"Titles": ["PID", "STAT"], "Processes": []}) == (0, 0)


# ---------------------------------------------------------------------------
# run_pid_pressure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_under_threshold_stays_silent():
    dm = _make_docker(
        {"healthy": _top([("1", "0", "Ss", "tini"), ("2", "1", "S", "app")])}
    )
    notifier, events = _make_notifier()
    cfg = PidPressureConfig(container_pid_warn=200, zombie_warn_per_container=30)

    results = await run_pid_pressure(dm, notifier, cfg, state=PidPressureState())

    assert results[0]["status"] == "ok"
    assert events == []


@pytest.mark.asyncio
async def test_zombie_breach_fires_after_debounce():
    rows = [("1", "0", "Ss", "python")] + [
        (str(i), "1", "Z", "chrome") for i in range(40)
    ]
    dm = _make_docker({"leaker": _top(rows)})
    notifier, events = _make_notifier()
    cfg = PidPressureConfig(zombie_warn_per_container=30, debounce=2)
    state = PidPressureState()

    # Tick 1: breach but debounced — no alert yet.
    results = await run_pid_pressure(dm, notifier, cfg, state=state)
    assert results[0].get("debounced") is True
    assert events == []

    # Tick 2: breach persists — alert fires.
    results = await run_pid_pressure(dm, notifier, cfg, state=state)
    assert results[0]["status"] == "warning"
    assert len(events) == 1
    assert events[0]["event"] == "pid_pressure.breach"
    assert events[0]["payload"]["container"] == "leaker"
    assert events[0]["payload"]["zombie_count"] == 40
    assert "init: true" in events[0]["payload"]["cause"]


@pytest.mark.asyncio
async def test_pid_breach_fires():
    rows = [(str(i), "1", "S", "worker") for i in range(250)]
    dm = _make_docker({"busy": _top(rows)})
    notifier, events = _make_notifier()
    cfg = PidPressureConfig(container_pid_warn=200, debounce=1)

    results = await run_pid_pressure(dm, notifier, cfg, state=PidPressureState())

    assert results[0]["status"] == "warning"
    assert any(e["event"] == "pid_pressure.breach" for e in events)


@pytest.mark.asyncio
async def test_debounce_resets_when_back_under_threshold():
    """A one-tick spike that clears does not alert."""
    breach_rows = [("1", "0", "Ss", "python")] + [
        (str(i), "1", "Z", "chrome") for i in range(40)
    ]
    ok_rows = [("1", "0", "Ss", "python")]
    notifier, events = _make_notifier()
    cfg = PidPressureConfig(zombie_warn_per_container=30, debounce=2)
    state = PidPressureState()

    # Tick 1: breach (debounced)
    dm = _make_docker({"flapper": _top(breach_rows)})
    await run_pid_pressure(dm, notifier, cfg, state=state)
    assert state.consecutive_breaches.get("flapper") == 1

    # Tick 2: back to normal — streak resets, no alert
    dm = _make_docker({"flapper": _top(ok_rows)})
    await run_pid_pressure(dm, notifier, cfg, state=state)
    assert "flapper" not in state.consecutive_breaches
    assert events == []


@pytest.mark.asyncio
async def test_host_zombie_total_fires_independently():
    """Several containers each under per-container cap but host total breaches."""
    a = _top([("1", "0", "Ss", "p")] + [(str(i), "1", "Z", "c") for i in range(20)])
    b = _top([("1", "0", "Ss", "p")] + [(str(i), "1", "Z", "c") for i in range(20)])
    dm = _make_docker({"a": a, "b": b})
    notifier, events = _make_notifier()
    cfg = PidPressureConfig(zombie_warn_per_container=30, zombie_warn_total=30)

    await run_pid_pressure(dm, notifier, cfg, state=PidPressureState())

    total_events = [e for e in events if e["event"] == "pid_pressure.zombies_total"]
    assert len(total_events) == 1
    assert total_events[0]["payload"]["zombie_total"] == 40


@pytest.mark.asyncio
async def test_auto_restart_gated_by_flag_off():
    """auto_restart=False → never restarts even over the hard cap."""
    rows = [(str(i), "1", "S", "worker") for i in range(500)]
    dm = _make_docker({"runaway": _top(rows)})
    notifier, events = _make_notifier()
    cfg = PidPressureConfig(
        container_pid_warn=200, auto_restart=False, debounce=1
    )

    results = await run_pid_pressure(dm, notifier, cfg, state=PidPressureState())

    dm.restart.assert_not_called()
    assert results[0]["status"] == "warning"
    assert any(e["event"] == "pid_pressure.breach" for e in events)


@pytest.mark.asyncio
async def test_auto_restart_fires_over_hard_cap_when_enabled():
    """auto_restart=True + over hard cap (2x warn) → restart + restarted event."""
    rows = [(str(i), "1", "S", "worker") for i in range(500)]
    dm = _make_docker({"runaway": _top(rows)})
    notifier, events = _make_notifier()
    cfg = PidPressureConfig(
        container_pid_warn=200, auto_restart=True, debounce=1, hard_cap_multiplier=2.0
    )

    results = await run_pid_pressure(dm, notifier, cfg, state=PidPressureState())

    dm.restart.assert_awaited_once_with("runaway")
    assert results[0]["status"] == "restarted"
    assert any(e["event"] == "pid_pressure.restarted" for e in events)


@pytest.mark.asyncio
async def test_auto_restart_not_fired_below_hard_cap():
    """auto_restart=True but only over warn (not hard cap) → alert, no restart."""
    rows = [(str(i), "1", "S", "worker") for i in range(250)]  # > 200 warn, < 400 cap
    dm = _make_docker({"busy": _top(rows)})
    notifier, events = _make_notifier()
    cfg = PidPressureConfig(
        container_pid_warn=200, auto_restart=True, debounce=1, hard_cap_multiplier=2.0
    )

    results = await run_pid_pressure(dm, notifier, cfg, state=PidPressureState())

    dm.restart.assert_not_called()
    assert results[0]["status"] == "warning"


@pytest.mark.asyncio
async def test_list_containers_failure_degrades_gracefully():
    dm = AsyncMock()
    dm.list_running_containers.side_effect = RuntimeError("docker down")
    notifier, events = _make_notifier()

    results = await run_pid_pressure(dm, notifier, PidPressureConfig())

    assert results == []
    assert events == []


@pytest.mark.asyncio
async def test_top_failure_per_container_is_isolated():
    """A container that exits between list and top yields an error row, not a crash."""
    dm = AsyncMock()
    dm.list_running_containers.return_value = ["gone", "fine"]

    async def _top_fn(name, ps_args="-eo pid,ppid,stat,comm"):
        if name == "gone":
            raise RuntimeError("no such container")
        return _top([("1", "0", "Ss", "app")])

    dm.top.side_effect = _top_fn
    notifier, events = _make_notifier()

    results = await run_pid_pressure(dm, notifier, PidPressureConfig())

    statuses = {r["container"]: r["status"] for r in results}
    assert statuses["gone"] == "error"
    assert statuses["fine"] == "ok"
