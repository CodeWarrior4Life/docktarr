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


import shlex


@dataclass(frozen=True)
class FixReport:
    fixed: int
    would_fix: int
    failed: int
    exceeded_rate_limit: bool
    errors: list[str] = field(default_factory=list)


async def apply_fixes(
    ssh: SSHClient,
    findings: list[PermissionFinding],
    *,
    dry_run: bool,
    max_files: int = 5000,
) -> FixReport:
    if len(findings) > max_files:
        log.warning(
            "perms: %d findings exceeds max_files=%d; emitting ticket instead of fix",
            len(findings),
            max_files,
        )
        return FixReport(fixed=0, would_fix=0, failed=0, exceeded_rate_limit=True)

    if dry_run:
        return FixReport(
            fixed=0, would_fix=len(findings), failed=0, exceeded_rate_limit=False
        )

    # Group by (uid, gid) target to batch chown calls
    groups: dict[tuple[int, int], list[str]] = {}
    for f in findings:
        key = (f.expected_uid, f.expected_gid)
        groups.setdefault(key, []).append(f.path)

    fixed = 0
    failed = 0
    errors: list[str] = []
    for (uid, gid), paths in groups.items():
        # Safely quote each path, chunk into batches of 100 to keep commands short
        for i in range(0, len(paths), 100):
            batch = paths[i : i + 100]
            quoted = " ".join(shlex.quote(p) for p in batch)
            cmd = f"chown {uid}:{gid} -- {quoted}"
            result = await ssh.run(cmd, sudo=True, timeout=60.0)
            if result.exit_code == 0:
                fixed += len(batch)
            else:
                failed += len(batch)
                errors.append(
                    f"chown batch failed ({result.exit_code}): {result.stderr[:200]}"
                )

    return FixReport(
        fixed=fixed,
        would_fix=0,
        failed=failed,
        exceeded_rate_limit=False,
        errors=errors,
    )


def translate_path(path: str, translation: dict[str, str]) -> str:
    """Translate doctarr-visible path to remote-host path using longest-prefix match."""
    best_prefix = ""
    best_replacement = ""
    for prefix, replacement in translation.items():
        if path.startswith(prefix) and len(prefix) > len(best_prefix):
            best_prefix = prefix
            best_replacement = replacement
    if best_prefix:
        return best_replacement + path[len(best_prefix) :]
    return path


async def run_permissions_health(
    cfg,  # PermissionHealthConfig from yaml_config
    ssh_clients: dict[str, SSHClient],
    plex_client=None,  # Optional PlexClient
    notifier=None,
) -> list[PermissionReport]:
    reports: list[PermissionReport] = []
    fix_ssh = ssh_clients.get(cfg.fix_host) if cfg.fix_host else None

    for path_cfg in cfg.paths:
        if not fix_ssh:
            log.error(
                "perms: no SSH client for fix_host=%s; skipping %s",
                cfg.fix_host,
                path_cfg.name,
            )
            continue

        remote_path = translate_path(path_cfg.path, cfg.fix_path_translation)
        report = await scan_path(fix_ssh, path_cfg, remote_path)
        reports.append(report)

        log.info(
            "perms[%s]: status=%s total=%d drift=%d (%.1f%%)",
            path_cfg.name,
            report.status,
            report.total_files,
            len(report.findings),
            report.drift_pct,
        )

        if report.status == "error" and notifier:
            await notifier.emit(
                "perms.drift",
                {
                    "path": path_cfg.name,
                    "drift_pct": report.drift_pct,
                    "total": report.total_files,
                    "drift_count": len(report.findings),
                },
            )

        if path_cfg.auto_fix and report.findings:
            fix_result = await apply_fixes(
                fix_ssh, report.findings, dry_run=False, max_files=5000
            )
            log.info(
                "perms[%s]: fixed=%d failed=%d",
                path_cfg.name,
                fix_result.fixed,
                fix_result.failed,
            )
            if fix_result.fixed > 0 and plex_client:
                # Trigger Plex library refresh; path→section resolution is caller's problem
                # For Phase 1 we refresh all sections
                try:
                    sections = await plex_client.library_sections()
                    for s in sections:
                        if s.get("key"):
                            await plex_client.refresh_section(int(s["key"]))
                except Exception as e:
                    log.warning("perms: Plex refresh failed: %s", e)
    return reports
