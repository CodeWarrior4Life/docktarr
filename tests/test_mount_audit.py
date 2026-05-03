"""Tests for mount_audit — auto-discover broken bind paths.

Covers the S107 2026-05-03 bug:
- qBit configured ``save_path=/Media/Downloads/completed`` but local-bind
  overlay was empty (no completed/ subdir). 28 torrents stuck at 0%.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from docktarr.docker_manager import DockerManager
from docktarr.notifier import Notifier
from docktarr.qbittorrent import QBitClient
from docktarr.mount_audit import audit_qbit, run_mount_audit


def _make_qbit_with_prefs(save_path: str, temp_path: str) -> QBitClient:
    """qBit mock that returns a preferences object with the given paths."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/auth/login" in path:
            resp = httpx.Response(200, text="Ok.")
            resp.headers["set-cookie"] = "SID=test; path=/"
            return resp
        if "/app/preferences" in path:
            return httpx.Response(
                200,
                json={
                    "save_path": save_path,
                    "temp_path": temp_path,
                    "temp_path_enabled": True,
                },
            )
        if "/torrents/categories" in path:
            return httpx.Response(200, json={})
        return httpx.Response(404)

    client = QBitClient("http://qbit:8082", "user", "pass")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _make_dm(
    existing_paths: set[str], writable: set[str] | None = None
) -> DockerManager:
    """Mock DockerManager whose exec_run answers test/touch/mkdir queries."""
    if writable is None:
        writable = set(existing_paths)
    created: set[str] = set()

    async def fake_exec_run(name, cmd, *, user=None, timeout=30.0):
        # Normalize cmd to a list of tokens for ergonomic matching.
        if isinstance(cmd, str):
            tokens = cmd.split()
        else:
            tokens = list(cmd)

        # test -d <path>
        if tokens[:2] == ["test", "-d"]:
            return (0, "") if tokens[2] in (existing_paths | created) else (1, "")
        # mkdir -p <path>  (auto-fix path)
        if tokens[:2] == ["mkdir", "-p"]:
            created.add(tokens[2])
            writable.add(tokens[2])
            return (0, "")
        # writability probe: sh -c 'touch <probe> && rm -f <probe>'
        if tokens and tokens[0] == "sh":
            joined = " ".join(tokens)
            for w in writable | created:
                if w in joined:
                    return (0, "")
            return (1, "")
        return (1, "unknown cmd")

    dm = DockerManager(_client=object())  # docker_client unused
    dm.exec_run = fake_exec_run  # type: ignore[assignment]
    return dm


@pytest.mark.asyncio
async def test_qbit_audit_detects_missing_save_path():
    """The S107 bug: save_path/temp_path don't exist inside container."""
    qbit = _make_qbit_with_prefs(
        save_path="/Media/Downloads/completed",
        temp_path="/Media/Downloads/incomplete",
    )
    # Container has /Media/Downloads but neither subdir.
    dm = _make_dm(existing_paths={"/Media/Downloads"})

    issues = await audit_qbit(qbit, "qbittorrent", dm, auto_fix=False)

    assert len(issues) == 2
    paths = {i.path for i in issues}
    assert paths == {
        "/Media/Downloads/completed",
        "/Media/Downloads/incomplete",
    }
    assert all(i.kind == "missing" for i in issues)
    assert all(i.fix_attempted is None for i in issues)


@pytest.mark.asyncio
async def test_qbit_audit_auto_fix_creates_missing_dirs():
    qbit = _make_qbit_with_prefs(
        save_path="/Media/Downloads/completed",
        temp_path="/Media/Downloads/incomplete",
    )
    dm = _make_dm(existing_paths={"/Media/Downloads"})

    issues = await audit_qbit(qbit, "qbittorrent", dm, auto_fix=True)

    assert len(issues) == 2
    assert all(i.fix_attempted == "mkdir -p (in container)" for i in issues)
    assert all(i.fix_result == "ok" for i in issues)


@pytest.mark.asyncio
async def test_qbit_audit_clean_when_paths_exist():
    qbit = _make_qbit_with_prefs(
        save_path="/Media/Downloads/completed",
        temp_path="/Media/Downloads/incomplete",
    )
    dm = _make_dm(
        existing_paths={
            "/Media/Downloads",
            "/Media/Downloads/completed",
            "/Media/Downloads/incomplete",
        }
    )

    issues = await audit_qbit(qbit, "qbittorrent", dm, auto_fix=True)

    assert issues == []


@pytest.mark.asyncio
async def test_run_mount_audit_emits_events_for_each_issue():
    qbit = _make_qbit_with_prefs(
        save_path="/Media/Downloads/completed",
        temp_path="/Media/Downloads/incomplete",
    )
    dm = _make_dm(existing_paths={"/Media/Downloads"})
    notifier = AsyncMock(spec=Notifier)

    summary = await run_mount_audit(
        qbit=qbit,
        qbit_container="qbittorrent",
        arr_clients={},
        arr_containers={},
        docker_manager=dm,
        notifier=notifier,
        auto_fix=True,
    )

    assert summary["issue_count"] == 2
    assert summary["errors"] == 2
    assert notifier.emit.await_count == 2
    # Each event payload is a serialized PathIssue dict.
    for call in notifier.emit.await_args_list:
        event_name, payload = call.args
        assert event_name == "mount_audit.issue"
        assert payload["service"] == "qBittorrent"
        assert payload["kind"] == "missing"
