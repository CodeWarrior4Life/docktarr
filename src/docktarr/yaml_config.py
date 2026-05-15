from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class HostRef:
    name: str
    ssh_ref: str | None = None


@dataclass(frozen=True)
class HWCapabilityConfig:
    enabled: bool = False
    schedule: str = "0 3 * * *"
    hosts: list[HostRef] = field(default_factory=list)


@dataclass(frozen=True)
class MediaContainer:
    name: str
    host: str
    kind: str  # "plex" | "tdarr" | "jellyfin" | "emby"
    expected_devices: list[str] = field(default_factory=list)
    auto_remediate: bool = False
    pref_file: str | None = None
    required_prefs: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MediaContainerAuditConfig:
    enabled: bool = False
    schedule: str = "0 */4 * * *"
    containers: list[MediaContainer] = field(default_factory=list)


@dataclass(frozen=True)
class PermissionPathConfig:
    name: str
    path: str
    expected_uid: int
    expected_gid: int
    expected_mode_min: str = "0644"
    auto_fix: bool = False
    max_drift_pct: float = 5.0


@dataclass(frozen=True)
class PermissionHealthConfig:
    enabled: bool = False
    schedule: str = "0 3 * * *"
    paths: list[PermissionPathConfig] = field(default_factory=list)
    fix_host: str | None = None
    fix_path_translation: dict[str, str] = field(default_factory=dict)
    fix_credential_ref: str | None = None


@dataclass(frozen=True)
class ArrCommandQueueYamlConfig:
    """YAML-side config for :mod:`docktarr.arr_command_queue`.

    Kept separate from ``ArrCommandQueueConfig`` (the dataclass the module
    actually consumes) so the yaml parser stays a pure data-conversion layer
    with no runtime imports. ``Config.from_env_and_yaml`` resolves env-var
    overrides (``DOCKTARR_ARR_COMMAND_QUEUE_*``) on top of this.
    """

    enabled: bool = True
    poll_interval_seconds: int = 60
    drain_threshold_count: int = 50
    drain_age_seconds: int = 600
    drain_command_names: list[str] = field(
        default_factory=lambda: ["EpisodeSearch", "SeasonSearch", "MovieSearch"]
    )
    elevated_warn_count: int = 200
    burst_threshold: int = 20
    burst_rate_threshold: float = 0.5
    # Scheduler-liveness probe (sibling: arr_scheduler_health). Lives in
    # the same YAML block to keep the operational surface unified — they
    # are conceptually one feature ("keep the ARR scheduler healthy") with
    # two probes.
    scheduler_health_enabled: bool = True
    scheduler_wedge_threshold: float = 3.0
    scheduler_critical_tasks: list[str] = field(
        default_factory=lambda: [
            "Rss Sync",
            "Import List Sync",
            "Refresh Monitored Downloads",
            "Messaging Cleanup",
        ]
    )


@dataclass(frozen=True)
class YamlConfig:
    hw_capability: HWCapabilityConfig | None = None
    media_container_audit: MediaContainerAuditConfig | None = None
    permission_health: PermissionHealthConfig | None = None
    arr_command_queue: ArrCommandQueueYamlConfig | None = None


def load_yaml_config(path: Path | str) -> YamlConfig:
    p = Path(path)
    if not p.exists():
        return YamlConfig()

    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {p}: {e}") from e

    hw = None
    if h := raw.get("hw_capability"):
        hw = HWCapabilityConfig(
            enabled=bool(h.get("enabled", False)),
            schedule=str(h.get("schedule", "0 3 * * *")),
            hosts=[
                HostRef(name=x["name"], ssh_ref=x.get("ssh_ref"))
                for x in h.get("hosts", [])
            ],
        )

    mca = None
    if m := raw.get("media_container_audit"):
        mca = MediaContainerAuditConfig(
            enabled=bool(m.get("enabled", False)),
            schedule=str(m.get("schedule", "0 */4 * * *")),
            containers=[
                MediaContainer(
                    name=c["name"],
                    host=c["host"],
                    kind=c["kind"],
                    expected_devices=list(c.get("expected_devices", [])),
                    auto_remediate=bool(c.get("auto_remediate", False)),
                    pref_file=c.get("pref_file"),
                    required_prefs=dict(c.get("required_prefs", {})),
                )
                for c in m.get("containers", [])
            ],
        )

    ph = None
    if p_ := raw.get("permission_health"):
        ph = PermissionHealthConfig(
            enabled=bool(p_.get("enabled", False)),
            schedule=str(p_.get("schedule", "0 3 * * *")),
            paths=[
                PermissionPathConfig(
                    name=x["name"],
                    path=x["path"],
                    expected_uid=int(x["expected_uid"]),
                    expected_gid=int(x["expected_gid"]),
                    expected_mode_min=str(x.get("expected_mode_min", "0644")),
                    auto_fix=bool(x.get("auto_fix", False)),
                    max_drift_pct=float(x.get("max_drift_pct", 5.0)),
                )
                for x in p_.get("paths", [])
            ],
            fix_host=p_.get("fix_host"),
            fix_path_translation=dict(p_.get("fix_path_translation", {})),
            fix_credential_ref=p_.get("fix_credential_ref"),
        )

    acq = None
    if a := raw.get("arr_command_queue"):
        acq = ArrCommandQueueYamlConfig(
            enabled=bool(a.get("enabled", True)),
            poll_interval_seconds=int(a.get("poll_interval_seconds", 60)),
            drain_threshold_count=int(a.get("drain_threshold_count", 50)),
            drain_age_seconds=int(a.get("drain_age_seconds", 600)),
            drain_command_names=list(
                a.get(
                    "drain_command_names",
                    ["EpisodeSearch", "SeasonSearch", "MovieSearch"],
                )
            ),
            elevated_warn_count=int(a.get("elevated_warn_count", 200)),
            burst_threshold=int(a.get("burst_threshold", 20)),
            burst_rate_threshold=float(a.get("burst_rate_threshold", 0.5)),
            scheduler_health_enabled=bool(
                a.get("scheduler_health_enabled", True)
            ),
            scheduler_wedge_threshold=float(
                a.get("scheduler_wedge_threshold", 3.0)
            ),
            scheduler_critical_tasks=list(
                a.get(
                    "scheduler_critical_tasks",
                    [
                        "Rss Sync",
                        "Import List Sync",
                        "Refresh Monitored Downloads",
                        "Messaging Cleanup",
                    ],
                )
            ),
        )

    return YamlConfig(
        hw_capability=hw,
        media_container_audit=mca,
        permission_health=ph,
        arr_command_queue=acq,
    )
