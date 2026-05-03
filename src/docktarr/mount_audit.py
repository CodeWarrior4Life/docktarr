"""Mount-path audit + auto-fix for ARR-stack containers.

For every service docktarr is wired to (qBittorrent, Sonarr, Radarr,
Readarr, Bookshelf, Plex), this module:

1. Asks the app's API where it expects to read/write data.
2. From INSIDE the container, verifies each of those paths exists and is
   writable by the container's runtime user.
3. Auto-fixes the cheap, safe class of bug — *missing subdirectory inside
   a working bind mount* — by ``mkdir -p`` from inside the container.
4. Emits a structured ``mount_audit.issue`` event (Telegram + webhook) for
   every issue, fixed or not, so failures never go silent.

The class of bug it exists to catch came from S107 (2026-05-03):

    * qBit configured ``save_path=/Media/Downloads/completed`` and
      ``temp_path=/Media/Downloads/incomplete``.
    * Host bind ``/share/CACHEDEV2_DATA/MediaOverflow/Downloads ->
      /Media/Downloads`` was an empty directory (4 KB, root-owned, no
      ``completed`` / ``incomplete`` subdirs).
    * Every download silently failed with "Permission denied" / "file_open
      error" / "fast resume rejected"; qBit had been "doing nothing for
      weeks" and nobody noticed.

The audit catches that exact shape: app says "I write to X", container
can't ``test -d X``, mount_audit ``mkdir -p X`` from inside the container
(so ownership inherits the container's runtime UID) and fires an alert.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from docktarr.docker_manager import DockerManager
from docktarr.notifier import Notifier
from docktarr.qbittorrent import QBitClient

if TYPE_CHECKING:
    from docktarr.arrclient import ArrClient
    from docktarr.http_health import HealthState

log = logging.getLogger("docktarr.mount_audit")


@dataclass
class PathIssue:
    """A single broken-mount finding."""

    service: (
        str  # "qBittorrent" | "Sonarr" | "Radarr" | "Readarr" | "Bookshelf" | "Plex"
    )
    container: str
    path: str  # path inside the container as the app referenced it
    role: str  # "save_path" | "temp_path" | "category:tv-sonarr" | "rootfolder" | ...
    kind: str  # "missing" | "not_writable" | "api_unreachable"
    severity: str = "error"  # "error" | "warn"
    fix_attempted: str | None = None
    fix_result: str | None = None  # "ok" | "<error message>"
    detail: str = ""


@dataclass
class AuditReport:
    issues: list[PathIssue] = field(default_factory=list)
    services_checked: list[str] = field(default_factory=list)

    def by_severity(self, sev: str) -> list[PathIssue]:
        return [i for i in self.issues if i.severity == sev]


# ---------------------------------------------------------------------------
# Per-container path probes
# ---------------------------------------------------------------------------


async def _path_exists(dm: DockerManager, container: str, path: str) -> bool:
    """True if ``test -d`` inside the container reports the path exists."""
    code, _ = await dm.exec_run(container, ["test", "-d", path])
    return code == 0


async def _path_writable(dm: DockerManager, container: str, path: str) -> bool:
    """True if we can create+remove a probe file in ``path`` from inside the container."""
    probe = f"{path.rstrip('/')}/.docktarr-write-probe"
    cmd = f"sh -c {shlex.quote(f'touch {shlex.quote(probe)} && rm -f {shlex.quote(probe)}')}"
    code, _ = await dm.exec_run(container, cmd)
    return code == 0


async def _try_mkdir(dm: DockerManager, container: str, path: str) -> tuple[bool, str]:
    """Attempt ``mkdir -p`` inside the container. Returns (ok, message)."""
    code, out = await dm.exec_run(container, ["mkdir", "-p", path])
    if code == 0:
        return True, "ok"
    return False, (out.strip() or f"mkdir exit={code}").splitlines()[0][:200]


# ---------------------------------------------------------------------------
# Per-service audits
# ---------------------------------------------------------------------------


async def audit_qbit(
    qbit: QBitClient,
    container: str,
    dm: DockerManager,
    *,
    auto_fix: bool = True,
) -> list[PathIssue]:
    issues: list[PathIssue] = []

    try:
        await qbit.login()
    except Exception as exc:
        return [
            PathIssue(
                service="qBittorrent",
                container=container,
                path="(api)",
                role="api_login",
                kind="api_unreachable",
                severity="error",
                detail=f"login failed: {exc}",
            )
        ]

    base = qbit._base_url
    cookies = qbit._cookies()

    # Global save_path + temp_path
    paths: list[tuple[str, str]] = []
    try:
        prefs = await qbit._client.get(
            f"{base}/api/v2/app/preferences", cookies=cookies
        )
        prefs.raise_for_status()
        p = prefs.json()
        if p.get("save_path"):
            paths.append(("save_path", p["save_path"]))
        if p.get("temp_path_enabled") and p.get("temp_path"):
            paths.append(("temp_path", p["temp_path"]))
    except Exception as exc:
        issues.append(
            PathIssue(
                service="qBittorrent",
                container=container,
                path="(api)",
                role="preferences",
                kind="api_unreachable",
                severity="error",
                detail=str(exc),
            )
        )
        return issues

    # Per-category save paths
    try:
        cats = await qbit._client.get(
            f"{base}/api/v2/torrents/categories", cookies=cookies
        )
        if cats.status_code == 200:
            for cname, cinfo in cats.json().items():
                sp = cinfo.get("savePath", "")
                if sp:
                    paths.append((f"category:{cname}", sp))
    except Exception as exc:
        log.debug("audit_qbit: categories fetch failed: %s", exc)

    seen: set[str] = set()
    for role, path in paths:
        if path in seen:
            continue
        seen.add(path)
        if await _path_exists(dm, container, path):
            if not await _path_writable(dm, container, path):
                issues.append(
                    PathIssue(
                        service="qBittorrent",
                        container=container,
                        path=path,
                        role=role,
                        kind="not_writable",
                    )
                )
            continue

        issue = PathIssue(
            service="qBittorrent",
            container=container,
            path=path,
            role=role,
            kind="missing",
        )
        if auto_fix:
            ok, msg = await _try_mkdir(dm, container, path)
            issue.fix_attempted = "mkdir -p (in container)"
            issue.fix_result = "ok" if ok else msg
            if ok and not await _path_writable(dm, container, path):
                issue.kind = "not_writable"
                issue.fix_result = "mkdir ok but not writable"
        issues.append(issue)

    return issues


async def audit_arr(
    name: str,
    client: "ArrClient",
    container: str,
    dm: DockerManager,
    *,
    auto_fix: bool = True,
) -> list[PathIssue]:
    issues: list[PathIssue] = []

    version = client._api_version()
    url = f"{client._url}/api/{version}/rootfolder"
    try:
        resp = await client._client.get(
            url, params={"apikey": client._api_key}, timeout=10.0
        )
        resp.raise_for_status()
        roots = resp.json()
    except Exception as exc:
        return [
            PathIssue(
                service=name,
                container=container,
                path="(api)",
                role="rootfolder",
                kind="api_unreachable",
                severity="error",
                detail=str(exc),
            )
        ]

    for rf in roots:
        path = rf.get("path") or ""
        if not path:
            continue
        accessible = rf.get("accessible", True)

        if await _path_exists(dm, container, path):
            if not await _path_writable(dm, container, path):
                issues.append(
                    PathIssue(
                        service=name,
                        container=container,
                        path=path,
                        role="rootfolder",
                        kind="not_writable",
                        detail=f"app reports accessible={accessible}",
                    )
                )
            continue

        issue = PathIssue(
            service=name,
            container=container,
            path=path,
            role="rootfolder",
            kind="missing",
            detail=f"app reports accessible={accessible}",
        )
        if auto_fix:
            ok, msg = await _try_mkdir(dm, container, path)
            issue.fix_attempted = "mkdir -p (in container)"
            issue.fix_result = "ok" if ok else msg
        issues.append(issue)

    return issues


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def run_mount_audit(
    *,
    qbit: QBitClient | None,
    qbit_container: str | None,
    arr_clients: dict[str, "ArrClient"],
    arr_containers: dict[str, str],
    docker_manager: DockerManager,
    notifier: Notifier,
    health_state: "HealthState | None" = None,
    auto_fix: bool = True,
) -> dict[str, Any]:
    report = AuditReport()

    if qbit is not None and qbit_container:
        report.services_checked.append("qBittorrent")
        try:
            report.issues.extend(
                await audit_qbit(
                    qbit, qbit_container, docker_manager, auto_fix=auto_fix
                )
            )
        except Exception as exc:
            log.exception("audit_qbit failed: %s", exc)

    for name, client in arr_clients.items():
        cn = arr_containers.get(name)
        if not cn:
            continue
        report.services_checked.append(name)
        try:
            report.issues.extend(
                await audit_arr(name, client, cn, docker_manager, auto_fix=auto_fix)
            )
        except Exception as exc:
            log.exception("audit_arr(%s) failed: %s", name, exc)

    # Emit + log
    for issue in report.issues:
        payload = asdict(issue)
        log.warning(
            "mount_audit: %s [%s] %s (%s, role=%s) — fix=%s detail=%s",
            issue.service,
            issue.container,
            issue.path,
            issue.kind,
            issue.role,
            issue.fix_result or "(none)",
            issue.detail,
        )
        await notifier.emit("mount_audit.issue", payload)

    summary = {
        "services_checked": report.services_checked,
        "issue_count": len(report.issues),
        "errors": len(report.by_severity("error")),
        "warnings": len(report.by_severity("warn")),
        "issues": [asdict(i) for i in report.issues],
    }
    if health_state is not None and hasattr(health_state, "record_mount_audit"):
        health_state.record_mount_audit(summary)
    if not report.issues:
        log.info(
            "mount_audit: clean across %d service(s)",
            len(report.services_checked),
        )
    return summary
