from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from docktarr.arrclient import ArrClient
from docktarr.config import Config, parse_duration
from docktarr.discovery import run_discovery
from docktarr.docker_manager import DockerManager
from docktarr.hw_capability import run_hw_capability, HWCapabilityReport
from docktarr.http_health import HealthServer, HealthState
from docktarr.media_container_audit import run_media_container_audit
from docktarr.notifier import Notifier
from docktarr.permissions_health import run_permissions_health
from docktarr.prowlarr import ProwlarrClient
from docktarr.pruner import run_pruner
from docktarr.qbittorrent import QBitClient
from docktarr.imposter_detector import run_imposter_backfill, run_imposter_detector
from docktarr.ssh_client import SSHClient, resolve_ssh_ref
from docktarr.stall_detector import run_stall_detector
from docktarr.state import IndexerState, IndexerStatus, StateStore
from docktarr.tester import run_tester

log = logging.getLogger("docktarr")


async def _build_scheduler_for_test(
    yaml_path: "Path | str | None" = None,
) -> tuple:
    """Build scheduler + health_state WITHOUT starting the scheduler or health server.

    Used by integration tests to verify wire-up without hitting real network resources.
    When DOCKTARR_SKIP_NETWORK_INIT=1 is set, skips all calls that require live network
    (prowlarr.ensure_tag, _reconcile, qbit.login).

    Returns (scheduler, health_state, http, qbit, arr_clients, hw_clients, ph_ssh,
             plex_client, vpn_http) — callers are responsible for cleanup.
    """
    skip_network = os.environ.get("DOCKTARR_SKIP_NETWORK_INIT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    if yaml_path is not None:
        config = Config.from_env_and_yaml(yaml_path)
    else:
        config = Config.from_env_and_yaml()

    health_state = HealthState()

    # Declared so shutdown block can unconditionally reference them
    ph_ssh: dict[str, SSHClient] = {}
    plex_client = None
    vpn_http: httpx.AsyncClient | None = None

    http = httpx.AsyncClient(base_url=config.prowlarr_url, timeout=30.0)
    prowlarr = ProwlarrClient(http, api_key=config.prowlarr_api_key)
    state = StateStore(path=Path("/config/state.json"))
    state.load()
    notifier = Notifier(
        client=httpx.AsyncClient(timeout=10.0),
        webhook_url=config.webhook_url,
        enabled_events=config.webhook_events,
    )

    if skip_network:
        tag_id = 0
        log.info(
            "DOCKTARR_SKIP_NETWORK_INIT: skipping prowlarr.ensure_tag + _reconcile"
        )
    else:
        # Ensure docktarr tag exists
        tag_id = await prowlarr.ensure_tag("docktarr")
        log.info("Using Prowlarr tag 'docktarr' (id=%d)", tag_id)
        # Reconcile state with Prowlarr on startup
        await _reconcile(prowlarr, state, tag_id)

    delay_secs = config.test_delay.total_seconds()

    scheduler = AsyncIOScheduler(timezone=config.tz)

    # --- Indexer health jobs (v0.1) ---
    scheduler.add_job(
        run_discovery,
        "interval",
        seconds=config.discovery_interval.total_seconds(),
        id="discovery",
        kwargs={
            "prowlarr": prowlarr,
            "state": state,
            "notifier": notifier,
            "tag_id": tag_id,
        },
        next_run_time=datetime.now(timezone.utc),
    )
    scheduler.add_job(
        run_tester,
        "interval",
        seconds=config.test_interval.total_seconds(),
        id="tester",
        kwargs={
            "prowlarr": prowlarr,
            "state": state,
            "notifier": notifier,
            "tag_id": tag_id,
            "test_delay": delay_secs,
        },
    )
    scheduler.add_job(
        run_pruner,
        "interval",
        seconds=config.prune_interval.total_seconds(),
        id="pruner",
        kwargs={
            "prowlarr": prowlarr,
            "state": state,
            "notifier": notifier,
            "prune_threshold": config.prune_threshold,
        },
    )

    # --- Stall detection jobs (v0.2) ---
    qbit = None
    arr_clients: dict[str, ArrClient] = {}

    if config.qbit_url and config.qbit_username and config.qbit_password:
        qbit = QBitClient(config.qbit_url, config.qbit_username, config.qbit_password)
        if skip_network:
            log.info("DOCKTARR_SKIP_NETWORK_INIT: skipping qbit.login()")
        else:
            await qbit.login()
            log.info("qBittorrent connected at %s", config.qbit_url)

        for app_config in config.arr_apps:
            arr_clients[app_config.name] = ArrClient(app_config)
            log.info("Registered *arr app: %s at %s", app_config.name, app_config.url)

        if arr_clients:
            scheduler.add_job(
                run_stall_detector,
                "interval",
                seconds=config.stall_interval.total_seconds(),
                id="stall_detector",
                kwargs={
                    "qbit": qbit,
                    "arr_clients": arr_clients,
                    "notifier": notifier,
                    "stall_threshold": config.stall_threshold,
                    "protected_categories": config.protected_categories,
                },
            )
            log.info(
                "Stall detector enabled (threshold=%s, interval=%s, protected=%s)",
                config.stall_threshold,
                config.stall_interval,
                config.protected_categories,
            )
        else:
            log.warning("No *arr apps configured -- stall detector disabled")
    else:
        log.info("qBittorrent not configured -- stall detection disabled")

    # --- Imposter detection (v0.3) ---
    if arr_clients.get("Sonarr"):
        scheduler.add_job(
            run_imposter_detector,
            "interval",
            seconds=config.imposter_interval.total_seconds(),
            id="imposter_detector",
            kwargs={
                "arr_clients": arr_clients,
                "notifier": notifier,
                "lookback": config.imposter_lookback,
                "tolerance": config.imposter_tolerance,
            },
        )
        log.info(
            "Imposter detector enabled (tolerance=%.0f%%, lookback=%s, interval=%s)",
            config.imposter_tolerance * 100,
            config.imposter_lookback,
            config.imposter_interval,
        )

        if config.imposter_backfill_enabled:
            scheduler.add_job(
                run_imposter_backfill,
                "interval",
                seconds=config.imposter_backfill_interval.total_seconds(),
                id="imposter_backfill",
                kwargs={
                    "arr_clients": arr_clients,
                    "notifier": notifier,
                    "tolerance": config.imposter_tolerance,
                },
            )
            log.info(
                "Imposter backfill enabled (interval=%s)",
                config.imposter_backfill_interval,
            )

    # --- qbit_health (ported from arr-orchestrator, T13; stale-namespace fix v0.5.1) ---
    docker_mgr: DockerManager | None = None
    if config.qbit_url and config.qbit_username and config.qbit_password and qbit:
        from docktarr.qbit_health import (
            QbitHealthConfig,
            QbitHealthState,
            run_qbit_health,
        )

        qbit_container = os.environ.get("QBITTORRENT_CONTAINER", "qbittorrent").strip()
        qbit_vpn_container = (
            os.environ.get("QBIT_VPN_CONTAINER", "gluetun").strip() or None
        )
        qbit_unreachable_threshold = int(
            os.environ.get("QBIT_HEALTH_UNREACHABLE_THRESHOLD", "2")
        )
        qbit_health_cfg = QbitHealthConfig(
            container_name=qbit_container,
            protected_categories=config.protected_categories,
            vpn_container_name=qbit_vpn_container,
            running_unreachable_threshold=qbit_unreachable_threshold,
        )
        qbit_health_state = QbitHealthState()
        docker_mgr = DockerManager()

        async def _qbit_health_job():
            await run_qbit_health(
                qbit,
                docker_mgr,
                notifier,
                qbit_health_cfg,
                state=qbit_health_state,
                health_state=health_state,
            )

        _qbit_health_interval = os.environ.get("QBIT_HEALTH_INTERVAL", "5m")
        scheduler.add_job(
            _qbit_health_job,
            "interval",
            seconds=parse_duration(_qbit_health_interval).total_seconds(),
            id="qbit_health",
        )
        log.info(
            "qbit_health enabled (container=%s, vpn=%s, threshold=%d, interval=%s)",
            qbit_container,
            qbit_vpn_container,
            qbit_unreachable_threshold,
            _qbit_health_interval,
        )

    # --- vpn_health (ported from arr-orchestrator, T14) ---
    _vpn_healthcheck_url = os.environ.get("VPN_HEALTHCHECK_URL", "").strip()
    if _vpn_healthcheck_url:
        from docktarr.vpn_health import run_vpn_health, VpnHealthConfig

        _vpn_container = os.environ.get("VPN_CONTAINER", "gluetun").strip()
        _vpn_regions_raw = os.environ.get(
            "VPN_ALLOWED_REGIONS", "CA Toronto,CA Montreal,CA Vancouver"
        ).strip()
        _vpn_allowed_regions = [
            r.strip() for r in _vpn_regions_raw.split(",") if r.strip()
        ]
        _vpn_require_pf = os.environ.get(
            "VPN_REQUIRE_PORT_FORWARDING", "true"
        ).strip().lower() not in ("0", "false", "no")
        vpn_health_cfg = VpnHealthConfig(
            container_name=_vpn_container,
            healthcheck_url=_vpn_healthcheck_url,
            allowed_regions=_vpn_allowed_regions,
            require_port_forwarding=_vpn_require_pf,
        )
        if docker_mgr is None:
            docker_mgr = DockerManager()
        vpn_http = httpx.AsyncClient(timeout=10.0)

        async def _vpn_health_job():
            await run_vpn_health(vpn_http, docker_mgr, notifier, vpn_health_cfg)

        _vpn_health_interval = os.environ.get("VPN_HEALTH_INTERVAL", "2m")
        scheduler.add_job(
            _vpn_health_job,
            "interval",
            seconds=parse_duration(_vpn_health_interval).total_seconds(),
            id="vpn_health",
        )
        log.info(
            "vpn_health enabled (container=%s, url=%s, interval=%s)",
            _vpn_container,
            _vpn_healthcheck_url,
            _vpn_health_interval,
        )

    # --- disk_health (ported from arr-orchestrator, T15) ---
    _disk_paths_raw = os.environ.get("DISK_HEALTH_PATHS", "").strip()
    if _disk_paths_raw:
        from docktarr.disk_health import run_disk_health, DiskPath

        _disk_warning_pct = float(os.environ.get("DISK_WARNING_PCT", "85.0"))
        _disk_critical_pct = float(os.environ.get("DISK_CRITICAL_PCT", "95.0"))
        _disk_paths = [
            DiskPath(
                path=p.strip(),
                warning_pct=_disk_warning_pct,
                critical_pct=_disk_critical_pct,
            )
            for p in _disk_paths_raw.split(",")
            if p.strip()
        ]

        async def _disk_health_job():
            await run_disk_health(_disk_paths, notifier)

        _disk_health_interval = os.environ.get("DISK_HEALTH_INTERVAL", "10m")
        scheduler.add_job(
            _disk_health_job,
            "interval",
            seconds=parse_duration(_disk_health_interval).total_seconds(),
            id="disk_health",
        )
        log.info(
            "disk_health enabled (paths=%s, interval=%s)",
            _disk_paths_raw,
            _disk_health_interval,
        )

    # --- arr_services (ported from arr-orchestrator, T15) ---
    if arr_clients:
        from docktarr.arr_services import run_arr_services

        async def _arr_services_job():
            await run_arr_services(arr_clients, notifier)

        _arr_services_interval = os.environ.get("ARR_SERVICES_INTERVAL", "5m")
        scheduler.add_job(
            _arr_services_job,
            "interval",
            seconds=parse_duration(_arr_services_interval).total_seconds(),
            id="arr_services",
        )
        log.info(
            "arr_services enabled (apps=%s, interval=%s)",
            list(arr_clients.keys()),
            _arr_services_interval,
        )

    # Daily digest
    hour, minute = (int(x) for x in config.digest_time.split(":"))
    scheduler.add_job(
        _send_digest,
        "cron",
        hour=hour,
        minute=minute,
        id="digest",
        kwargs={"state": state, "notifier": notifier},
    )

    # --- HW capability (v0.4) ---
    hw_clients: dict[str, SSHClient] = {}
    if config.yaml.hw_capability and config.yaml.hw_capability.enabled:
        for host_ref in config.yaml.hw_capability.hosts:
            if host_ref.ssh_ref:
                ref = resolve_ssh_ref(host_ref.ssh_ref, host=host_ref.name)
                hw_clients[host_ref.name] = SSHClient(ref)

        scheduler.add_job(
            _hw_capability_job,
            "cron",
            **_parse_cron(config.yaml.hw_capability.schedule),
            id="hw_capability",
            kwargs={
                "hosts": hw_clients,
                "state": state,
                "notifier": notifier,
                "health_state": health_state,
            },
        )
        log.info(
            "HW capability detector enabled (schedule=%s, hosts=%d)",
            config.yaml.hw_capability.schedule,
            len(hw_clients),
        )

    # --- media_container_audit (v0.4) ---
    if config.yaml.media_container_audit and config.yaml.media_container_audit.enabled:
        audit_docker: dict[str, DockerManager] = {}
        audit_ssh: dict[str, SSHClient] = {}
        local_host = os.environ.get("DOCKTARR_HOST_NAME", "zion")
        for spec in config.yaml.media_container_audit.containers:
            # Reuse SSH client from hw_capability if same host
            if spec.host not in audit_ssh and spec.host in hw_clients:
                audit_ssh[spec.host] = hw_clients[spec.host]
            elif spec.host not in audit_ssh:
                # Look up ssh_ref from yaml hosts
                host_ref = next(
                    (
                        h
                        for h in (
                            config.yaml.hw_capability.hosts
                            if config.yaml.hw_capability
                            else []
                        )
                        if h.name == spec.host
                    ),
                    None,
                )
                if host_ref and host_ref.ssh_ref:
                    ref = resolve_ssh_ref(host_ref.ssh_ref, host=host_ref.name)
                    audit_ssh[spec.host] = SSHClient(ref)
            # DockerManager for local host only (Phase 1 limitation)
            if spec.host == local_host and spec.host not in audit_docker:
                audit_docker[spec.host] = DockerManager()

        async def _audit_job():
            # Pull hw_report from state if available, else empty
            hw_report = getattr(state, "hw_report", None)
            if hw_report is None:
                hw_report = HWCapabilityReport()
            findings = await run_media_container_audit(
                containers=config.yaml.media_container_audit.containers,
                docker_managers=audit_docker,
                ssh_clients=audit_ssh,
                hw_report=hw_report,
                notifier=notifier,
            )
            health_state.record_audit_findings(
                [
                    {
                        "container": f.container,
                        "host": f.host,
                        "status": f.status.value,
                        "reason": f.reason,
                        "hint": f.remediation_hint,
                    }
                    for f in findings
                ]
            )

        scheduler.add_job(
            _audit_job,
            "cron",
            **_parse_cron(config.yaml.media_container_audit.schedule),
            id="media_container_audit",
        )
        log.info(
            "media_container_audit enabled (schedule=%s, containers=%d)",
            config.yaml.media_container_audit.schedule,
            len(config.yaml.media_container_audit.containers),
        )

    # --- permissions_health (v0.4) ---
    if config.yaml.permission_health and config.yaml.permission_health.enabled:
        ph = config.yaml.permission_health
        if ph.fix_host and ph.fix_credential_ref:
            ref = resolve_ssh_ref(ph.fix_credential_ref, host=ph.fix_host)
            ph_ssh[ph.fix_host] = SSHClient(ref)

        # Optional Plex client for refresh trigger
        plex_url = os.environ.get("PLEX_URL", "").strip()
        plex_token = os.environ.get("PLEX_TOKEN", "").strip()
        if plex_url and plex_token:
            from docktarr.plex_api import PlexClient

            plex_client = PlexClient(plex_url, plex_token)

        async def _perms_job():
            reports = await run_permissions_health(ph, ph_ssh, plex_client, notifier)
            health_state.record_permission_findings(
                [
                    {
                        "path": r.path_config.name,
                        "total": r.total_files,
                        "drift": len(r.findings),
                        "status": r.status,
                    }
                    for r in reports
                ]
            )

        scheduler.add_job(
            _perms_job,
            "cron",
            **_parse_cron(ph.schedule),
            id="permissions_health",
        )
        log.info(
            "permissions_health enabled (schedule=%s, paths=%d, fix_host=%s)",
            ph.schedule,
            len(ph.paths),
            ph.fix_host,
        )

    return (
        scheduler,
        health_state,
        http,
        qbit,
        arr_clients,
        hw_clients,
        ph_ssh,
        plex_client,
        vpn_http,
    )


async def main() -> None:
    # Parse config first so log level is applied before the helper runs
    config = Config.from_env_and_yaml()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    try:
        _ver = _pkg_version("docktarr")
    except PackageNotFoundError:
        _ver = "unknown"
    log.info("Docktarr v%s starting (prowlarr=%s)", _ver, config.prowlarr_url)

    # Build scheduler and all components (skips network if DOCKTARR_SKIP_NETWORK_INIT=1)
    (
        scheduler,
        health_state,
        http,
        qbit,
        arr_clients,
        hw_clients,
        ph_ssh,
        plex_client,
        vpn_http,
    ) = await _build_scheduler_for_test()

    health_server = HealthServer(state=health_state)
    await health_server.start()

    scheduler.start()
    log.info(
        "Scheduler started. Discovery=%s, Tester=%s, Pruner=%s, Stall=%s, Digest=%s",
        config.discovery_interval,
        config.test_interval,
        config.prune_interval,
        config.stall_interval if qbit else "disabled",
        config.digest_time,
    )

    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: _signal_handler())

    await stop_event.wait()

    scheduler.shutdown(wait=False)
    await health_server.stop()
    await http.aclose()
    if qbit:
        await qbit.close()
    for client in arr_clients.values():
        await client.close()
    for client in hw_clients.values():
        await client.close()
    for client in ph_ssh.values():
        await client.close()
    if plex_client is not None:
        await plex_client.close()
    if vpn_http is not None:
        await vpn_http.aclose()
    log.info("Doctarr stopped")


async def _reconcile(prowlarr: ProwlarrClient, state: StateStore, tag_id: int) -> None:
    """Reconcile local state with Prowlarr on startup."""
    indexers = await prowlarr.get_indexers_by_tag(tag_id)
    prowlarr_names = {idx["definitionName"] for idx in indexers}

    for entry in list(state.all()):
        if entry.definition_name not in prowlarr_names:
            state.remove(entry.definition_name)
            log.info("Reconcile: removed orphan state for %s", entry.definition_name)

    for idx in indexers:
        name = idx["definitionName"]
        if state.get(name) is None:
            status = (
                IndexerStatus.ACTIVE if idx.get("enable") else IndexerStatus.CANDIDATE
            )
            state.set(
                IndexerState(
                    definition_name=name,
                    prowlarr_id=idx["id"],
                    status=status,
                )
            )
            log.info("Reconcile: adopted %s as %s", name, status.value)

    state.save()


async def _send_digest(state: StateStore, notifier: Notifier) -> None:
    active = len(state.get_by_status(IndexerStatus.ACTIVE))
    degraded = len(state.get_by_status(IndexerStatus.DEGRADED))
    candidates = len(state.get_by_status(IndexerStatus.CANDIDATE))

    await notifier.emit(
        "digest",
        {
            "total_active": active,
            "total_degraded": degraded,
            "added_24h": candidates,
            "pruned_24h": 0,
        },
    )


def _parse_cron(expr: str) -> dict:
    """Parse '0 3 * * *' -> kwargs for AsyncIOScheduler.add_job('cron', ...)."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Invalid cron: {expr!r}")
    return dict(
        minute=parts[0],
        hour=parts[1],
        day=parts[2],
        month=parts[3],
        day_of_week=parts[4],
    )


async def _hw_capability_job(hosts, state, notifier, health_state: HealthState):
    report = await run_hw_capability(hosts)
    # Store report on state for consumption by /health endpoint + media_container_audit (T9, T16)
    if hasattr(state, "set_hw_report"):
        state.set_hw_report(report)
    # Publish a serializable view into /health
    health_state.record_hw(
        {
            host: [
                {
                    "kind": a.kind,
                    "vendor": a.vendor,
                    "model": a.model,
                    "device_paths": list(a.device_paths),
                    "codecs_decode": list(a.codecs_decode),
                    "codecs_encode": list(a.codecs_encode),
                    "hdr_tone_mapping": a.hdr_tone_mapping,
                    "driver_version": a.driver_version,
                }
                for a in accs
            ]
            for host, accs in report.by_host.items()
        }
    )
    for host, accelerators in report.by_host.items():
        if not accelerators:
            await notifier.emit("hw.none_detected", {"host": host})
