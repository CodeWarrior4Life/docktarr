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
        telegram_bot_token=config.telegram_bot_token,
        telegram_chat_id=config.telegram_chat_id,
    )
    if notifier.telegram_enabled:
        log.info("Telegram notifications enabled (chat_id=%s)", config.telegram_chat_id)

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
            try:
                await qbit.login()
                log.info("qBittorrent connected at %s", config.qbit_url)
            except Exception as exc:
                # Don't let an unreachable qBit crash startup — qbit_health is
                # the very module that knows how to recover qBit (Pattern 1
                # exit-137, stale gluetun namespace, etc.). If we abort here we
                # never schedule qbit_health and the recovery loop is dead.
                log.warning(
                    "qBittorrent unreachable at startup (%s) — continuing; "
                    "qbit_health will probe and recover when qBit is back",
                    exc,
                )

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

    # --- arr_services + container recovery (consolidation T15; recovery 0.5.2) ---
    if arr_clients:
        from datetime import timedelta as _timedelta

        from docktarr.arr_services import ArrServicesState, run_arr_services

        if docker_mgr is None:
            docker_mgr = DockerManager()
        arr_services_state = ArrServicesState()
        arr_unreachable_threshold = int(
            os.environ.get("ARR_UNREACHABLE_THRESHOLD", "3")
        )
        arr_restart_cooldown = parse_duration(
            os.environ.get("ARR_RESTART_COOLDOWN", "15m")
        )

        async def _arr_services_job():
            await run_arr_services(
                arr_clients,
                notifier,
                docker_manager=docker_mgr,
                state=arr_services_state,
                health_state=health_state,
                running_unreachable_threshold=arr_unreachable_threshold,
                restart_cooldown=arr_restart_cooldown,
            )

        _arr_services_interval = os.environ.get("ARR_SERVICES_INTERVAL", "5m")
        scheduler.add_job(
            _arr_services_job,
            "interval",
            seconds=parse_duration(_arr_services_interval).total_seconds(),
            id="arr_services",
        )
        log.info(
            "arr_services enabled (apps=%s, containers=%s, threshold=%d, cooldown=%s, interval=%s)",
            list(arr_clients.keys()),
            {n: c.container_name for n, c in arr_clients.items()},
            arr_unreachable_threshold,
            arr_restart_cooldown,
            _arr_services_interval,
        )

    # --- mount_audit (S107 2026-05-03) ---
    # Catches the class of bug where an app's configured save/library path
    # (qBit save_path, Sonarr/Radarr root folder, etc.) doesn't actually
    # exist or isn't writable inside the container — e.g. an empty bind
    # overlay hiding the real subdirs. Auto-fixes missing dirs via
    # in-container ``mkdir -p``; emits a ``mount_audit.issue`` event for
    # everything else (Telegram + webhook).
    mount_audit_enabled = (
        os.environ.get("MOUNT_AUDIT_ENABLED", "true").strip().lower() == "true"
    )
    if mount_audit_enabled and (qbit or arr_clients):
        from docktarr.mount_audit import run_mount_audit

        if docker_mgr is None:
            docker_mgr = DockerManager()

        mount_audit_auto_fix = (
            os.environ.get("MOUNT_AUDIT_AUTO_FIX", "true").strip().lower() == "true"
        )
        mount_audit_qbit_container = os.environ.get(
            "QBITTORRENT_CONTAINER", "qbittorrent"
        ).strip()
        mount_audit_interval = os.environ.get("MOUNT_AUDIT_INTERVAL", "6h")

        def _arr_containers_map() -> dict[str, str]:
            return {n: c.container_name for n, c in arr_clients.items()}

        async def _mount_audit_job():
            await run_mount_audit(
                qbit=qbit,
                qbit_container=mount_audit_qbit_container if qbit else None,
                arr_clients=arr_clients,
                arr_containers=_arr_containers_map(),
                docker_manager=docker_mgr,
                notifier=notifier,
                health_state=health_state,
                auto_fix=mount_audit_auto_fix,
            )

        scheduler.add_job(
            _mount_audit_job,
            "interval",
            seconds=parse_duration(mount_audit_interval).total_seconds(),
            id="mount_audit",
            next_run_time=datetime.now(timezone.utc),  # run once on startup
        )
        log.info(
            "mount_audit enabled (auto_fix=%s, interval=%s, qbit_container=%s, arr_apps=%s)",
            mount_audit_auto_fix,
            mount_audit_interval,
            mount_audit_qbit_container,
            list(arr_clients.keys()),
        )

    # --- service_responsiveness (S107 2026-05-03) ---
    # Catches the class of bug where a container is "running" + "healthy" but
    # its HTTP listener has become unresponsive. Probes arbitrary URLs, measures
    # latency, restarts on sustained slowness. Configured via SERVICE_PROBES env
    # (DSL: 'name,url,container,slow_ms,consecutive;...'). Disabled if empty.
    raw_probes = os.environ.get("SERVICE_PROBES", "").strip()
    if raw_probes:
        from docktarr.service_responsiveness import (
            ServiceProbeState,
            parse_probes_env,
            run_service_responsiveness,
        )

        probes = parse_probes_env(raw_probes)
        if probes:
            if docker_mgr is None:
                docker_mgr = DockerManager()
            sr_state: dict[str, ServiceProbeState] = {}
            sr_interval = os.environ.get("SERVICE_PROBES_INTERVAL", "5m")
            sr_cooldown = parse_duration(
                os.environ.get("SERVICE_PROBES_RESTART_COOLDOWN", "15m")
            )

            async def _service_responsiveness_job():
                await run_service_responsiveness(
                    probes,
                    docker_mgr,
                    notifier,
                    state=sr_state,
                    health_state=health_state,
                    restart_cooldown=sr_cooldown,
                )

            scheduler.add_job(
                _service_responsiveness_job,
                "interval",
                seconds=parse_duration(sr_interval).total_seconds(),
                id="service_responsiveness",
                next_run_time=datetime.now(timezone.utc),
            )
            log.info(
                "service_responsiveness enabled (probes=%d, interval=%s, cooldown=%s)",
                len(probes),
                sr_interval,
                sr_cooldown,
            )

    # --- arr_command_queue (0.7.1) ---
    # Watches Sonarr/Radarr /api/v3/command and auto-drains runaway
    # external-API search batches (trigger=unspecified EpisodeSearch /
    # SeasonSearch / MovieSearch) before they wedge the scheduler. Driven
    # by the 2026-05-13 incident where 891 unspecified-trigger EpisodeSearch
    # commands blocked RssSync + ImportListSync for 15 hours.
    _acq_yaml = config.yaml.arr_command_queue
    if _acq_yaml and _acq_yaml.enabled and arr_clients:
        from docktarr.arr_command_queue import (
            ArrCommandQueueConfig,
            run_arr_command_queue,
        )
        from docktarr.arr_scheduler_health import (
            ArrSchedulerHealthConfig,
            run_arr_scheduler_health,
        )

        _acq_cfg = ArrCommandQueueConfig(
            enabled=_acq_yaml.enabled,
            poll_interval_seconds=_acq_yaml.poll_interval_seconds,
            drain_threshold_count=_acq_yaml.drain_threshold_count,
            drain_age_seconds=_acq_yaml.drain_age_seconds,
            drain_command_names=list(_acq_yaml.drain_command_names),
            elevated_warn_count=_acq_yaml.elevated_warn_count,
            burst_threshold=_acq_yaml.burst_threshold,
            burst_rate_threshold=_acq_yaml.burst_rate_threshold,
        )
        _sched_cfg = ArrSchedulerHealthConfig(
            enabled=_acq_yaml.scheduler_health_enabled,
            wedge_threshold=_acq_yaml.scheduler_wedge_threshold,
            critical_tasks=list(_acq_yaml.scheduler_critical_tasks),
        )

        # Only Sonarr/Radarr expose /api/v3/command in the relevant shape;
        # Readarr/Bookshelf use v1 with a different command set. Scope to v3.
        _acq_clients = [
            c for c in arr_clients.values() if c.name in ("Sonarr", "Radarr")
        ]

        if _acq_clients:

            async def _arr_command_queue_job():
                # Liveness FIRST — wedge signal is the primary primitive
                # and decides whether the queue probe should force-drain
                # regardless of count/age gates. Bug-of-the-day rationale:
                # by the time queue depth crosses 50, the scheduler has
                # already missed several RssSync cycles; the wedge ratio
                # trips at minute ~5 vs queue-depth at minute ~10.
                force_services: set[str] = set()
                if _sched_cfg.enabled:
                    _, force_services = await run_arr_scheduler_health(
                        arr_clients=_acq_clients,
                        config=_sched_cfg,
                        notifier=notifier,
                        health_state=health_state,
                    )
                await run_arr_command_queue(
                    arr_clients=_acq_clients,
                    config=_acq_cfg,
                    notifier=notifier,
                    health_state=health_state,
                    state_store=state,
                    force_drain_services=force_services,
                )
                # Persist burst-state to /config/state.json so a restart
                # mid-burst doesn't lose the baseline.
                state.save()

            scheduler.add_job(
                _arr_command_queue_job,
                "interval",
                seconds=_acq_cfg.poll_interval_seconds,
                id="arr_command_queue",
                next_run_time=datetime.now(timezone.utc),
            )
            log.info(
                "arr_command_queue enabled (services=%s, poll=%ds, "
                "drain_threshold=%d/%ds, burst=%d/%.2fps, scheduler=%s "
                "wedge=%.1fx, critical=%s)",
                [c.name for c in _acq_clients],
                _acq_cfg.poll_interval_seconds,
                _acq_cfg.drain_threshold_count,
                _acq_cfg.drain_age_seconds,
                _acq_cfg.burst_threshold,
                _acq_cfg.burst_rate_threshold,
                "on" if _sched_cfg.enabled else "off",
                _sched_cfg.wedge_threshold,
                _sched_cfg.critical_tasks,
            )

    # --- plex_throttle (v0.7.0 2026-05-03) ---
    # Plex-aware qBit download cap. When Plex has a stream, throttle qBit so it
    # doesn't compete for bandwidth/disk. Idempotent + grace-windowed. Disabled
    # by default (PLEX_THROTTLE_ENABLED=false). Requires PLEX_URL + PLEX_TOKEN
    # + qBit configured.
    _plex_throttle_enabled = os.environ.get(
        "PLEX_THROTTLE_ENABLED", "false"
    ).strip().lower() not in ("0", "false", "no", "")
    _plex_throttle_url = os.environ.get("PLEX_URL", "").strip()
    _plex_throttle_token = os.environ.get("PLEX_TOKEN", "").strip()
    if (
        _plex_throttle_enabled
        and _plex_throttle_url
        and _plex_throttle_token
        and qbit is not None
    ):
        from docktarr.plex_throttle import (
            PlexThrottleConfig,
            PlexThrottleState,
            run_plex_throttle,
        )

        if plex_client is None:
            from docktarr.plex_api import PlexClient

            plex_client = PlexClient(_plex_throttle_url, _plex_throttle_token)

        plex_throttle_cfg = PlexThrottleConfig(
            plex_url=_plex_throttle_url,
            plex_token=_plex_throttle_token,
            interval=parse_duration(os.environ.get("PLEX_THROTTLE_INTERVAL", "30s")),
            idle_limit_kbps=int(os.environ.get("PLEX_THROTTLE_IDLE_LIMIT_KBPS", "0")),
            directplay_limit_kbps=int(
                os.environ.get("PLEX_THROTTLE_DIRECTPLAY_LIMIT_KBPS", "30000")
            ),
            transcode_limit_kbps=int(
                os.environ.get("PLEX_THROTTLE_TRANSCODE_LIMIT_KBPS", "5000")
            ),
            grace=parse_duration(os.environ.get("PLEX_THROTTLE_GRACE", "60s")),
        )
        plex_throttle_state = PlexThrottleState()

        async def _plex_throttle_job():
            await run_plex_throttle(
                plex_client,
                qbit,
                notifier,
                plex_throttle_cfg,
                state=plex_throttle_state,
                health_state=health_state,
            )

        scheduler.add_job(
            _plex_throttle_job,
            "interval",
            seconds=plex_throttle_cfg.interval.total_seconds(),
            id="plex_throttle",
            next_run_time=datetime.now(timezone.utc),
        )
        log.info(
            "plex_throttle enabled (interval=%s, idle=%dkB/s, directplay=%dkB/s, "
            "transcode=%dkB/s, grace=%s)",
            plex_throttle_cfg.interval,
            plex_throttle_cfg.idle_limit_kbps,
            plex_throttle_cfg.directplay_limit_kbps,
            plex_throttle_cfg.transcode_limit_kbps,
            plex_throttle_cfg.grace,
        )

    # --- pid_pressure (v0.8.0 2026-06-21) ---
    # Zombie / PID-pressure watchdog. Born from the surplusrecovery-harvester-1
    # incident: a bare-interpreter PID 1 (no init) leaked 439 chrome zombies
    # over six weeks. Reads `docker top` per running container for pid + zombie
    # (STAT=Z) counts; alert-only by default (PID_PRESSURE_AUTO_RESTART=false).
    _pid_pressure_enabled = os.environ.get(
        "PID_PRESSURE_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no", "off")
    if _pid_pressure_enabled:
        from docktarr.pid_pressure import (
            PidPressureConfig,
            PidPressureState,
            run_pid_pressure,
        )

        if docker_mgr is None:
            try:
                docker_mgr = DockerManager()
            except Exception as exc:
                # No docker socket (e.g. test/CI host) — degrade to disabled
                # rather than crash startup. pid_pressure needs docker access.
                log.warning("pid_pressure: docker unavailable, disabling (%s)", exc)
                docker_mgr = None

    if _pid_pressure_enabled and docker_mgr is not None:
        _pid_pressure_cfg = PidPressureConfig(
            enabled=True,
            container_pid_warn=int(
                os.environ.get("PID_PRESSURE_CONTAINER_PID_WARN", "200")
            ),
            zombie_warn_total=int(
                os.environ.get("PID_PRESSURE_ZOMBIE_WARN_TOTAL", "50")
            ),
            zombie_warn_per_container=int(
                os.environ.get("PID_PRESSURE_ZOMBIE_WARN_PER_CONTAINER", "30")
            ),
            auto_restart=os.environ.get("PID_PRESSURE_AUTO_RESTART", "false")
            .strip()
            .lower()
            not in ("0", "false", "no", "off", ""),
            debounce=int(os.environ.get("PID_PRESSURE_DEBOUNCE", "2")),
        )
        _pid_pressure_state = PidPressureState()
        _pid_pressure_interval = os.environ.get("PID_PRESSURE_INTERVAL", "10m")

        async def _pid_pressure_job():
            await run_pid_pressure(
                docker_mgr,
                notifier,
                _pid_pressure_cfg,
                state=_pid_pressure_state,
            )

        scheduler.add_job(
            _pid_pressure_job,
            "interval",
            seconds=parse_duration(_pid_pressure_interval).total_seconds(),
            id="pid_pressure",
        )
        log.info(
            "pid_pressure enabled (pid_warn=%d, zombie_warn=%d/container %d/host, "
            "auto_restart=%s, debounce=%d, interval=%s)",
            _pid_pressure_cfg.container_pid_warn,
            _pid_pressure_cfg.zombie_warn_per_container,
            _pid_pressure_cfg.zombie_warn_total,
            _pid_pressure_cfg.auto_restart,
            _pid_pressure_cfg.debounce,
            _pid_pressure_interval,
        )

    # --- plex_singleton (v0.8.0 2026-06-21) ---
    # Split-brain Plex detection. Born from the incident where a Zion Plex and a
    # Cypher Plex ran simultaneously sharing one machineIdentifier — clients
    # bound non-deterministically and landed on the weaker host. Queries each
    # endpoint's /identity and alerts if 2+ reachable endpoints share an ID.
    _plex_singleton_enabled = os.environ.get(
        "PLEX_SINGLETON_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no", "off")
    if _plex_singleton_enabled:
        from docktarr.plex_singleton import (
            PlexSingletonConfig,
            run_plex_singleton,
        )

        _ps_endpoints_raw = os.environ.get(
            "PLEX_SINGLETON_ENDPOINTS",
            "http://10.0.0.16:32400,http://10.0.0.111:32400",
        ).strip()
        _ps_endpoints = [e.strip() for e in _ps_endpoints_raw.split(",") if e.strip()]
        _plex_singleton_cfg = PlexSingletonConfig(
            enabled=True,
            endpoints=_ps_endpoints,
            token=os.environ.get("PLEX_TOKEN", "").strip(),
        )
        _plex_singleton_interval = os.environ.get("PLEX_SINGLETON_INTERVAL", "5m")

        async def _plex_singleton_job():
            await run_plex_singleton(_plex_singleton_cfg, notifier)

        scheduler.add_job(
            _plex_singleton_job,
            "interval",
            seconds=parse_duration(_plex_singleton_interval).total_seconds(),
            id="plex_singleton",
        )
        log.info(
            "plex_singleton enabled (endpoints=%s, interval=%s)",
            _ps_endpoints,
            _plex_singleton_interval,
        )

    # --- plex_connections_guard (v0.8.0 2026-06-21) ---
    # Self-healing Plex server-discovery guard. Born from the Zion->Cypher
    # migration where Cypher's Plex still published the dead Zion IP
    # (customConnections="http://10.0.0.16:32400") to plex.tv — clients tried
    # the dead address and hung ("spinning"). For each reachable endpoint, if
    # NONE of its published customConnections URLs is reachable, it replaces
    # customConnections with the live endpoint and toggles
    # PublishServerOnPlexOnlineKey 0->1 to force a re-publish. Idempotent: does
    # nothing if a published URL is already reachable. auto_fix default-on.
    _plex_cg_enabled = os.environ.get(
        "PLEX_CONNECTIONS_GUARD_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no", "off")
    if _plex_cg_enabled:
        from docktarr.plex_connections_guard import (
            PlexConnectionsGuardConfig,
            run_plex_connections_guard,
        )

        # Endpoints: PLEX_CONNECTIONS_ENDPOINTS, falling back to
        # PLEX_SINGLETON_ENDPOINTS (same default set), then the hardcoded default.
        _plex_cg_endpoints_raw = (
            os.environ.get("PLEX_CONNECTIONS_ENDPOINTS", "").strip()
            or os.environ.get("PLEX_SINGLETON_ENDPOINTS", "").strip()
            or "http://10.0.0.16:32400,http://10.0.0.111:32400"
        )
        _plex_cg_endpoints = [
            e.strip() for e in _plex_cg_endpoints_raw.split(",") if e.strip()
        ]
        _plex_cg_auto_fix = os.environ.get(
            "PLEX_CONNECTIONS_GUARD_AUTO_FIX", "true"
        ).strip().lower() not in ("0", "false", "no", "off", "")
        _plex_cg_cfg = PlexConnectionsGuardConfig(
            enabled=True,
            endpoints=_plex_cg_endpoints,
            auto_fix=_plex_cg_auto_fix,
            token=os.environ.get("PLEX_TOKEN", "").strip(),
        )
        _plex_cg_interval = os.environ.get("PLEX_CONNECTIONS_GUARD_INTERVAL", "5m")

        async def _plex_connections_guard_job():
            await run_plex_connections_guard(_plex_cg_cfg, notifier)

        scheduler.add_job(
            _plex_connections_guard_job,
            "interval",
            seconds=parse_duration(_plex_cg_interval).total_seconds(),
            id="plex_connections_guard",
        )
        log.info(
            "plex_connections_guard enabled (endpoints=%s, auto_fix=%s, interval=%s)",
            _plex_cg_endpoints,
            _plex_cg_auto_fix,
            _plex_cg_interval,
        )

    # --- artwork_health (v0.9.0 2026-07-15) ---
    # Consumer-drift guard + artwork presence spot-check. Born from the
    # 2026-07-15 incident: Sonarr/Radarr's "Kodi (XBMC) / Emby" (XbmcMetadata)
    # metadata consumer was disabled, so no poster.jpg / fanart.jpg / .nfo were
    # written to disk and Plex/Jellyfin showed blank artwork. Each tick GETs
    # /api/v3/metadata per arr, re-enables the consumer if it drifted off
    # (auto_heal default on — safe + reversible), and (if a DockerManager is
    # available) docker-exec spot-checks the N most-recently-added items for
    # poster/fanart on disk. OFF by default — enable via ARTWORK_HEALTH_ENABLED.
    _artwork_enabled = os.environ.get(
        "ARTWORK_HEALTH_ENABLED", "false"
    ).strip().lower() not in ("0", "false", "no", "off", "")
    _artwork_clients = [
        c for c in arr_clients.values() if c.name in ("Sonarr", "Radarr")
    ]
    if _artwork_enabled and _artwork_clients:
        from docktarr.artwork_health import (
            ArtworkHealthConfig,
            ArtworkHealthState,
            run_artwork_health,
        )

        _artwork_presence = os.environ.get(
            "ARTWORK_HEALTH_PRESENCE_CHECK", "true"
        ).strip().lower() not in ("0", "false", "no", "off")
        # Presence spot-check needs docker exec into the arr container; build a
        # DockerManager if one isn't already up, degrading to disabled (not a
        # crash) on a host with no docker socket.
        if _artwork_presence and docker_mgr is None:
            try:
                docker_mgr = DockerManager()
            except Exception as exc:
                log.warning(
                    "artwork_health: docker unavailable, presence spot-check "
                    "disabled (%s)",
                    exc,
                )
                docker_mgr = None
        _artwork_cfg = ArtworkHealthConfig(
            enabled=True,
            auto_heal=os.environ.get("ARTWORK_HEALTH_AUTO_HEAL", "true").strip().lower()
            not in ("0", "false", "no", "off"),
            check_image_fields=os.environ.get(
                "ARTWORK_HEALTH_CHECK_IMAGE_FIELDS", "false"
            )
            .strip()
            .lower()
            not in ("0", "false", "no", "off", ""),
            presence_check=_artwork_presence and docker_mgr is not None,
            presence_sample_size=int(
                os.environ.get("ARTWORK_HEALTH_PRESENCE_SAMPLE_SIZE", "10")
            ),
            presence_auto_refresh=os.environ.get(
                "ARTWORK_HEALTH_PRESENCE_AUTO_REFRESH", "false"
            )
            .strip()
            .lower()
            not in ("0", "false", "no", "off", ""),
            debounce=int(os.environ.get("ARTWORK_HEALTH_DEBOUNCE", "1")),
        )
        _artwork_state = ArtworkHealthState()
        _artwork_docker = docker_mgr if _artwork_cfg.presence_check else None
        _artwork_interval = os.environ.get("ARTWORK_HEALTH_INTERVAL", "6h")

        async def _artwork_health_job():
            await run_artwork_health(
                arr_clients=_artwork_clients,
                config=_artwork_cfg,
                notifier=notifier,
                state=_artwork_state,
                docker_manager=_artwork_docker,
                health_state=health_state,
            )

        scheduler.add_job(
            _artwork_health_job,
            "interval",
            seconds=parse_duration(_artwork_interval).total_seconds(),
            id="artwork_health",
            next_run_time=datetime.now(timezone.utc),  # run once on startup
        )
        log.info(
            "artwork_health enabled (services=%s, auto_heal=%s, "
            "check_image_fields=%s, presence_check=%s, sample=%d, "
            "auto_refresh=%s, debounce=%d, interval=%s)",
            [c.name for c in _artwork_clients],
            _artwork_cfg.auto_heal,
            _artwork_cfg.check_image_fields,
            _artwork_cfg.presence_check,
            _artwork_cfg.presence_sample_size,
            _artwork_cfg.presence_auto_refresh,
            _artwork_cfg.debounce,
            _artwork_interval,
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
    log.info("Docktarr stopped")


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
