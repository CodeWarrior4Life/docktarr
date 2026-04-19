from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from doctarr.arrclient import ArrClient
from doctarr.config import Config
from doctarr.discovery import run_discovery
from doctarr.hw_capability import run_hw_capability, HWCapabilityReport
from doctarr.notifier import Notifier
from doctarr.prowlarr import ProwlarrClient
from doctarr.pruner import run_pruner
from doctarr.qbittorrent import QBitClient
from doctarr.imposter_detector import run_imposter_detector
from doctarr.ssh_client import SSHClient, resolve_ssh_ref
from doctarr.stall_detector import run_stall_detector
from doctarr.state import IndexerState, IndexerStatus, StateStore
from doctarr.tester import run_tester

log = logging.getLogger("doctarr")


async def main() -> None:
    config = Config.from_env_and_yaml()

    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    log.info("Doctarr v0.2.0 starting (prowlarr=%s)", config.prowlarr_url)

    http = httpx.AsyncClient(base_url=config.prowlarr_url, timeout=30.0)
    prowlarr = ProwlarrClient(http, api_key=config.prowlarr_api_key)
    state = StateStore(path=Path("/config/state.json"))
    state.load()
    notifier = Notifier(
        client=httpx.AsyncClient(timeout=10.0),
        webhook_url=config.webhook_url,
        enabled_events=config.webhook_events,
    )

    # Ensure doctarr tag exists
    tag_id = await prowlarr.ensure_tag("doctarr")
    log.info("Using Prowlarr tag 'doctarr' (id=%d)", tag_id)

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
            kwargs={"hosts": hw_clients, "state": state, "notifier": notifier},
        )
        log.info(
            "HW capability detector enabled (schedule=%s, hosts=%d)",
            config.yaml.hw_capability.schedule,
            len(hw_clients),
        )

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
    await http.aclose()
    if qbit:
        await qbit.close()
    for client in arr_clients.values():
        await client.close()
    for client in hw_clients.values():
        await client.close()
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


async def _hw_capability_job(hosts, state, notifier):
    report = await run_hw_capability(hosts)
    # Store report on state for consumption by /health endpoint + media_container_audit (T9, T16)
    if hasattr(state, "set_hw_report"):
        state.set_hw_report(report)
    for host, accelerators in report.by_host.items():
        if not accelerators:
            await notifier.emit("hw.none_detected", {"host": host})
