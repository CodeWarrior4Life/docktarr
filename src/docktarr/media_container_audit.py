from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Iterable

from docktarr.docker_manager import ContainerInfo
from docktarr.hw_capability import HWAccelerator, HWCapabilityReport
from docktarr.yaml_config import MediaContainer

log = logging.getLogger("docktarr.audit")


class AuditStatus(str, enum.Enum):
    ALIGNED = "aligned"
    DEGRADED = "degraded"
    INCAPABLE = "incapable"


@dataclass(frozen=True)
class AuditFinding:
    container: str
    host: str
    status: AuditStatus
    reason: str
    remediation_hint: str | None = None


def audit_plex_container(
    spec: MediaContainer,
    info: ContainerInfo,
    prefs: dict[str, str],
    available_hw: Iterable[HWAccelerator],
) -> AuditFinding:
    hw_list = list(available_hw)
    if not hw_list:
        return AuditFinding(
            container=spec.name,
            host=spec.host,
            status=AuditStatus.INCAPABLE,
            reason="no hardware accelerator detected on host",
        )

    missing_devices = [d for d in spec.expected_devices if d not in info.device_paths]
    if missing_devices:
        return AuditFinding(
            container=spec.name,
            host=spec.host,
            status=AuditStatus.DEGRADED,
            reason=f"container missing device passthrough: {missing_devices}",
            remediation_hint=(
                f"Recreate container with --device {missing_devices[0]}:{missing_devices[0]}. "
                f"See runbook: Plex - Rebuild Runbook with QuickSync HW Transcoding."
            ),
        )

    missing_prefs = {k: v for k, v in spec.required_prefs.items() if prefs.get(k) != v}
    if missing_prefs:
        return AuditFinding(
            container=spec.name,
            host=spec.host,
            status=AuditStatus.DEGRADED,
            reason=f"HardwareAcceleratedCodecs preference missing/mismatched: {missing_prefs}",
            remediation_hint=("PUT /:/prefs?HardwareAcceleratedCodecs=1 on Plex API."),
        )

    return AuditFinding(
        container=spec.name,
        host=spec.host,
        status=AuditStatus.ALIGNED,
        reason="device passthrough and preferences aligned",
    )


async def run_media_container_audit(
    containers: list[MediaContainer],
    docker_managers: dict,  # keyed by host → DockerManager
    ssh_clients: dict,  # keyed by host → SSHClient
    hw_report: HWCapabilityReport,
    notifier,
) -> list[AuditFinding]:
    findings: list[AuditFinding] = []
    for spec in containers:
        if spec.kind != "plex":
            log.warning(
                "audit: kind %r not yet supported; skipping %s", spec.kind, spec.name
            )
            continue
        dm = docker_managers.get(spec.host)
        if not dm:
            log.error("audit: no DockerManager for host %s", spec.host)
            continue
        try:
            info = await dm.get_container(spec.name)
        except LookupError:
            findings.append(
                AuditFinding(
                    container=spec.name,
                    host=spec.host,
                    status=AuditStatus.DEGRADED,
                    reason="container not found on host",
                )
            )
            continue

        # Read Plex prefs via SSH (since docktarr doesn't share host FS with Plex container)
        ssh = ssh_clients.get(spec.host)
        prefs = await _read_plex_prefs(ssh, spec) if ssh else {}
        available_hw = hw_report.by_host.get(spec.host, [])
        finding = audit_plex_container(spec, info, prefs, available_hw)
        findings.append(finding)

        if finding.status == AuditStatus.DEGRADED:
            await notifier.emit(
                "hw.degraded",
                {
                    "container": finding.container,
                    "host": finding.host,
                    "reason": finding.reason,
                    "hint": finding.remediation_hint,
                },
            )
    return findings


async def _read_plex_prefs(ssh, spec: MediaContainer) -> dict[str, str]:
    if not spec.pref_file:
        return {}
    # Use docker exec through SSH to read the prefs file from inside the container
    cmd = (
        f"/share/CACHEDEV2_DATA/.qpkg/container-station/bin/docker "
        f"exec {spec.name} cat '{spec.pref_file}'"
    )
    result = await ssh.run(cmd, timeout=10)
    if result.exit_code != 0:
        log.warning(
            "audit: failed to read prefs on %s: %s", spec.host, result.stderr[:200]
        )
        return {}
    from docktarr.plex_api import parse_preferences_xml

    try:
        parsed = parse_preferences_xml(result.stdout)
        return parsed.attrs
    except Exception as e:
        log.warning("audit: failed to parse prefs: %s", e)
        return {}
