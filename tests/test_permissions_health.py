import pytest
from doctarr.permissions_health import (
    PermissionFinding,
    PermissionReport,
    parse_find_output,
    tally_report,
)
from doctarr.yaml_config import PermissionPathConfig


PATH_CFG = PermissionPathConfig(
    name="Plex Movies",
    path="/data/Video/Movies",
    expected_uid=1026,
    expected_gid=100,
    expected_mode_min="0644",
    auto_fix=False,
    max_drift_pct=5.0,
)


def test_parse_find_output_extracts_uid_gid_mode_path():
    raw = """1026 100 644 /data/Video/Movies/x.mkv
1000 0 644 /data/Video/Movies/bad.mkv
0 0 400 /data/Video/Movies/rooty.mkv
"""
    entries = parse_find_output(raw)
    assert len(entries) == 3
    assert entries[1].uid == 1000
    assert entries[2].mode == 0o400


def test_tally_report_flags_wrong_owner():
    entries = parse_find_output(
        "1026 100 644 /a.mkv\n1000 0 644 /b.mkv\n0 0 644 /c.mkv\n"
    )
    report = tally_report(PATH_CFG, entries)
    # 2 of 3 files wrong owner → drift_pct 66.7 — over max 5% → error
    assert report.status == "error"
    assert report.total_files == 3
    assert len(report.findings) == 2


def test_tally_report_healthy_when_all_aligned():
    entries = parse_find_output("1026 100 644 /a.mkv\n1026 100 644 /b.mkv\n")
    report = tally_report(PATH_CFG, entries)
    assert report.status == "healthy"
    assert report.findings == []


def test_tally_report_flags_mode_too_restrictive():
    entries = parse_find_output("1026 100 400 /a.mkv\n")
    report = tally_report(PATH_CFG, entries)
    assert report.findings[0].reason == "mode_too_restrictive"


from doctarr.ssh_client import SSHClient, SSHRef, _FakeSSHConnection
from doctarr.permissions_health import apply_fixes, FixReport


@pytest.mark.asyncio
async def test_apply_fixes_batches_chown_calls():
    captured = []
    client = SSHClient(
        ref=SSHRef(host="mega", username="su", password="pw"),
        _connection_factory=_FakeSSHConnection.factory({"*": ""}, capture=captured),
    )
    findings = [
        PermissionFinding(
            path="/share/Media/Video/Movies/a.mkv",
            observed_uid=1000,
            observed_gid=0,
            observed_mode=0o644,
            expected_uid=1026,
            expected_gid=100,
            reason="wrong_owner",
        ),
        PermissionFinding(
            path="/share/Media/Video/Movies/b.mkv",
            observed_uid=0,
            observed_gid=0,
            observed_mode=0o644,
            expected_uid=1026,
            expected_gid=100,
            reason="wrong_owner",
        ),
    ]
    report = await apply_fixes(client, findings, dry_run=False, max_files=100)
    assert report.fixed == 2
    # Batched by target owner — single chown call per group
    sudo_cmds = [c for c in captured if "sudo -S" in c and "chown 1026:100" in c]
    assert len(sudo_cmds) == 1  # 2 files → 1 batched call


@pytest.mark.asyncio
async def test_apply_fixes_dry_run_does_not_execute():
    captured = []
    client = SSHClient(
        ref=SSHRef(host="mega", username="su", password="pw"),
        _connection_factory=_FakeSSHConnection.factory({"*": ""}, capture=captured),
    )
    findings = [
        PermissionFinding(
            path="/x.mkv",
            observed_uid=1000,
            observed_gid=0,
            observed_mode=0o644,
            expected_uid=1026,
            expected_gid=100,
            reason="wrong_owner",
        ),
    ]
    report = await apply_fixes(client, findings, dry_run=True, max_files=100)
    assert report.fixed == 0
    assert report.would_fix == 1
    assert all("chown" not in c for c in captured)


@pytest.mark.asyncio
async def test_apply_fixes_rate_limit_ticketed():
    client = SSHClient(
        ref=SSHRef(host="mega", username="su", password="pw"),
        _connection_factory=_FakeSSHConnection.factory({"*": ""}),
    )
    findings = [
        PermissionFinding(
            path=f"/x{i}.mkv",
            observed_uid=1000,
            observed_gid=0,
            observed_mode=0o644,
            expected_uid=1026,
            expected_gid=100,
            reason="wrong_owner",
        )
        for i in range(50)
    ]
    report = await apply_fixes(client, findings, dry_run=False, max_files=10)
    assert report.fixed == 0
    assert report.exceeded_rate_limit is True
