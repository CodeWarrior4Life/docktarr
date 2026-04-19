"""Tests for disk_health — disk space monitoring with warning/critical thresholds."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from doctarr.disk_health import DiskPath, run_disk_health
from doctarr.notifier import Notifier


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


def _fake_usage(*, total_gb: float, free_gb: float):
    """Return a shutil.disk_usage-compatible named tuple."""
    total = int(total_gb * 1024**3)
    free = int(free_gb * 1024**3)
    used = total - free
    result = MagicMock()
    result.total = total
    result.free = free
    result.used = used
    return result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_below_warning_no_events():
    """Usage below warning_pct → no events, status 'ok'."""
    dp = DiskPath(path="/data", warning_pct=85.0, critical_pct=95.0)
    notifier, events = _make_notifier()

    with patch(
        "doctarr.disk_health.shutil.disk_usage",
        return_value=_fake_usage(total_gb=10, free_gb=5),
    ):
        results = await run_disk_health([dp], notifier)

    assert len(results) == 1
    assert results[0]["status"] == "ok"
    assert results[0]["path"] == "/data"
    assert events == []


@pytest.mark.asyncio
async def test_warning_threshold_emits_disk_warning():
    """Usage >= warning_pct but < critical_pct → emit disk.warning, status 'warning'."""
    dp = DiskPath(path="/data", warning_pct=85.0, critical_pct=95.0)
    notifier, events = _make_notifier()

    # 90% used: total=10GB, free=1GB → used=9GB → 90%
    with patch(
        "doctarr.disk_health.shutil.disk_usage",
        return_value=_fake_usage(total_gb=10, free_gb=1),
    ):
        results = await run_disk_health([dp], notifier)

    assert results[0]["status"] == "warning"
    assert len(events) == 1
    assert events[0]["event"] == "disk.warning"
    assert events[0]["payload"]["path"] == "/data"
    assert events[0]["payload"]["percent_used"] == pytest.approx(90.0, abs=0.5)


@pytest.mark.asyncio
async def test_critical_threshold_emits_disk_critical():
    """Usage >= critical_pct → emit disk.critical, status 'critical'."""
    dp = DiskPath(path="/data", warning_pct=85.0, critical_pct=95.0)
    notifier, events = _make_notifier()

    # 96% used: total=100GB, free=4GB → used=96GB → 96%
    with patch(
        "doctarr.disk_health.shutil.disk_usage",
        return_value=_fake_usage(total_gb=100, free_gb=4),
    ):
        results = await run_disk_health([dp], notifier)

    assert results[0]["status"] == "critical"
    assert len(events) == 1
    assert events[0]["event"] == "disk.critical"
    assert events[0]["payload"]["path"] == "/data"
    assert events[0]["payload"]["percent_used"] >= 95.0


@pytest.mark.asyncio
async def test_oserror_returns_error_status_no_crash():
    """shutil.disk_usage raises OSError → return error status, no events, no crash."""
    dp = DiskPath(path="/nonexistent", warning_pct=85.0, critical_pct=95.0)
    notifier, events = _make_notifier()

    with patch(
        "doctarr.disk_health.shutil.disk_usage", side_effect=OSError("No such file")
    ):
        results = await run_disk_health([dp], notifier)

    assert results[0]["status"] == "error"
    assert results[0]["path"] == "/nonexistent"
    assert "No such file" in results[0]["error"]
    assert events == []


@pytest.mark.asyncio
async def test_multiple_paths_independent():
    """Multiple paths are each checked independently."""
    paths = [
        DiskPath(path="/data", warning_pct=85.0, critical_pct=95.0),
        DiskPath(path="/backup", warning_pct=85.0, critical_pct=95.0),
    ]
    notifier, events = _make_notifier()

    def _fake(path):
        # /data is healthy (50%), /backup is critical (96%)
        if path == "/data":
            return _fake_usage(total_gb=10, free_gb=5)
        return _fake_usage(total_gb=100, free_gb=4)

    with patch("doctarr.disk_health.shutil.disk_usage", side_effect=_fake):
        results = await run_disk_health(paths, notifier)

    assert results[0]["path"] == "/data"
    assert results[0]["status"] == "ok"
    assert results[1]["path"] == "/backup"
    assert results[1]["status"] == "critical"
    assert len(events) == 1
    assert events[0]["event"] == "disk.critical"
    assert events[0]["payload"]["path"] == "/backup"


@pytest.mark.asyncio
async def test_exactly_at_warning_threshold_emits_warning():
    """Usage exactly at warning_pct triggers disk.warning (boundary check)."""
    dp = DiskPath(path="/media", warning_pct=85.0, critical_pct=95.0)
    notifier, events = _make_notifier()

    # 85% exactly: total=100, free=15
    with patch(
        "doctarr.disk_health.shutil.disk_usage",
        return_value=_fake_usage(total_gb=100, free_gb=15),
    ):
        results = await run_disk_health([dp], notifier)

    assert results[0]["status"] == "warning"
    assert len(events) == 1
    assert events[0]["event"] == "disk.warning"


@pytest.mark.asyncio
async def test_custom_thresholds_respected():
    """Custom warning_pct/critical_pct are used instead of defaults."""
    dp = DiskPath(path="/data", warning_pct=50.0, critical_pct=75.0)
    notifier, events = _make_notifier()

    # 60% used: below custom critical (75%) but above custom warning (50%)
    with patch(
        "doctarr.disk_health.shutil.disk_usage",
        return_value=_fake_usage(total_gb=10, free_gb=4),
    ):
        results = await run_disk_health([dp], notifier)

    assert results[0]["status"] == "warning"
    assert len(events) == 1
    assert events[0]["event"] == "disk.warning"


@pytest.mark.asyncio
async def test_empty_paths_list_returns_empty():
    """Empty path list returns empty results with no events."""
    notifier, events = _make_notifier()
    results = await run_disk_health([], notifier)
    assert results == []
    assert events == []
