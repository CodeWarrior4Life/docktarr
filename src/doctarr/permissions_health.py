from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

from doctarr.ssh_client import SSHClient
from doctarr.yaml_config import PermissionPathConfig

log = logging.getLogger("doctarr.perms")

Reason = Literal["wrong_owner", "wrong_group", "mode_too_restrictive"]


@dataclass(frozen=True)
class FileStat:
    uid: int
    gid: int
    mode: int
    path: str


@dataclass(frozen=True)
class PermissionFinding:
    path: str
    observed_uid: int
    observed_gid: int
    observed_mode: int
    expected_uid: int
    expected_gid: int
    reason: Reason


@dataclass(frozen=True)
class PermissionReport:
    path_config: PermissionPathConfig
    total_files: int
    findings: list[PermissionFinding]
    status: str  # "healthy" | "warn" | "error"

    @property
    def drift_pct(self) -> float:
        return (
            (len(self.findings) / self.total_files * 100.0) if self.total_files else 0.0
        )


def parse_find_output(output: str) -> list[FileStat]:
    entries: list[FileStat] = []
    for line in output.splitlines():
        parts = line.strip().split(" ", 3)
        if len(parts) != 4:
            continue
        try:
            entries.append(
                FileStat(
                    uid=int(parts[0]),
                    gid=int(parts[1]),
                    mode=int(parts[2], 8),
                    path=parts[3],
                )
            )
        except ValueError:
            continue
    return entries


def tally_report(
    cfg: PermissionPathConfig, entries: list[FileStat]
) -> PermissionReport:
    expected_mode = int(cfg.expected_mode_min, 8)
    findings: list[PermissionFinding] = []

    for e in entries:
        reason: Reason | None = None
        if e.uid != cfg.expected_uid:
            reason = "wrong_owner"
        elif e.gid != cfg.expected_gid:
            reason = "wrong_group"
        elif (e.mode & 0o777) < (expected_mode & 0o777):
            # Simplified: treats any bit gap as restrictive. Good enough for Phase 1.
            reason = "mode_too_restrictive"

        if reason:
            findings.append(
                PermissionFinding(
                    path=e.path,
                    observed_uid=e.uid,
                    observed_gid=e.gid,
                    observed_mode=e.mode,
                    expected_uid=cfg.expected_uid,
                    expected_gid=cfg.expected_gid,
                    reason=reason,
                )
            )

    if not findings:
        status = "healthy"
    else:
        drift_pct = len(findings) / len(entries) * 100.0 if entries else 0.0
        status = "error" if drift_pct > cfg.max_drift_pct else "warn"

    return PermissionReport(
        path_config=cfg,
        total_files=len(entries),
        findings=findings,
        status=status,
    )


_EXCLUDES = [
    "@Recycle",
    "@Recently-Snapshot",
    ".@__thumb",
    "lost+found",
]


def _build_find_cmd(path: str) -> str:
    excludes = " ".join(f"-not -path '*{e}*'" for e in _EXCLUDES)
    return f"find {path} -type f {excludes} -printf '%u %g %m %p\\n'"


async def scan_path(
    ssh: SSHClient, cfg: PermissionPathConfig, path_on_remote: str
) -> PermissionReport:
    """Run find on the remote host, return a PermissionReport.

    path_on_remote: path translated to remote-host coordinate system (applies fix_path_translation).
    """
    cmd = _build_find_cmd(path_on_remote)
    result = await ssh.run(cmd, timeout=300.0)  # 5 min cap
    if result.exit_code != 0:
        log.error("perms: scan %s failed: %s", path_on_remote, result.stderr[:300])
        return PermissionReport(
            path_config=cfg, total_files=0, findings=[], status="error"
        )
    entries = parse_find_output(result.stdout)
    return tally_report(cfg, entries)
