"""Plex-aware qBittorrent download throttle.

Polls Plex ``/status/sessions`` on a fixed interval. When Plex has at least one
active stream, applies a download cap to qBittorrent so torrenting can't
saturate the disk/network and starve playback. When Plex is idle (with a
configurable grace window), restores the unrestricted limit.

Three tiers:

1. **Transcode active** -> tightest cap (default 5 MB/s). Transcoding hits CPU,
   disk reads, and network simultaneously and is the most fragile state.
2. **Direct play active (no transcode)** -> moderate cap (default 30 MB/s).
3. **Idle** (no streams, sustained for ``grace`` seconds) -> unrestricted
   (default 0 = unlimited).

Idempotent: only calls qBit's ``setDownloadLimit`` when the target changes.
Failure modes:

* Plex unreachable -> snapshot records the error, qBit is left at whatever
  limit was last applied. Don't lift caps blindly when we can't see Plex —
  losing visibility is not the same as Plex going idle.
* qBit unreachable -> snapshot records the error and the apply is retried on
  the next tick. Never crashes the scheduler job.

The S108 incident (2026-05-03) was disk I/O contention, not bandwidth — a
host-side file copy + Qsirch reindex starved Plex's spinning-pool reads. This
module addresses the *bandwidth* class of contention; disk-side contention is
not in scope here. See ``docs/`` for the broader Plex-aware-orchestration
roadmap.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from docktarr.notifier import Notifier
from docktarr.plex_api import PlexClient
from docktarr.qbittorrent import QBitClient

if TYPE_CHECKING:
    from docktarr.http_health import HealthState

log = logging.getLogger(__name__)

UNLIMITED = 0  # qBit setDownloadLimit convention: 0 means unlimited

STATE_IDLE = "idle"
STATE_DIRECTPLAY = "directplay"
STATE_TRANSCODE = "transcode"
STATE_UNKNOWN = "unknown"  # Plex unreachable


@dataclass(frozen=True)
class PlexThrottleConfig:
    plex_url: str
    plex_token: str
    interval: timedelta = timedelta(seconds=30)
    idle_limit_kbps: int = UNLIMITED
    directplay_limit_kbps: int = 30_000  # 30 MB/s
    transcode_limit_kbps: int = 5_000  # 5 MB/s
    grace: timedelta = timedelta(seconds=60)


@dataclass
class PlexThrottleState:
    """Mutable per-instance state shared across scheduler ticks."""

    last_active_at: datetime | None = None
    current_limit_kbps: int | None = None  # last value we successfully applied
    last_state_label: str = STATE_IDLE


_TRANSCODE_RE = re.compile(r"<TranscodeSession\b")
_VIDEO_RE = re.compile(r"<Video\b")
_TRACK_RE = re.compile(r"<Track\b")  # music sessions also count as bandwidth


def _classify(xml: str) -> tuple[str, int, int]:
    """Return (state_label, active_count, transcode_count) from sessions XML."""
    transcodes = len(_TRANSCODE_RE.findall(xml))
    actives = len(_VIDEO_RE.findall(xml)) + len(_TRACK_RE.findall(xml))
    if transcodes > 0:
        return STATE_TRANSCODE, actives, transcodes
    if actives > 0:
        return STATE_DIRECTPLAY, actives, 0
    return STATE_IDLE, 0, 0


def _target_kbps(label: str, cfg: PlexThrottleConfig) -> int:
    if label == STATE_TRANSCODE:
        return cfg.transcode_limit_kbps
    if label == STATE_DIRECTPLAY:
        return cfg.directplay_limit_kbps
    return cfg.idle_limit_kbps


async def run_plex_throttle(
    plex: PlexClient,
    qbit: QBitClient,
    notifier: Notifier,
    config: PlexThrottleConfig,
    *,
    state: PlexThrottleState | None = None,
    health_state: "HealthState | None" = None,
    now: datetime | None = None,
) -> None:
    """Single-shot Plex-aware throttle decision.

    ``now`` is injectable for tests. Defaults to ``datetime.now(timezone.utc)``.
    """
    if state is None:
        state = PlexThrottleState()
    if now is None:
        now = datetime.now(timezone.utc)

    snapshot: dict = {
        "last_check": now.isoformat(),
        "plex_state": STATE_UNKNOWN,
        "active_sessions": None,
        "transcode_sessions": None,
        "target_kbps": None,
        "applied_kbps": state.current_limit_kbps,
        "last_active_at": (
            state.last_active_at.isoformat() if state.last_active_at else None
        ),
        "in_grace": False,
        "last_action": None,
        "error": None,
    }

    def _publish() -> None:
        snapshot["applied_kbps"] = state.current_limit_kbps
        snapshot["last_active_at"] = (
            state.last_active_at.isoformat() if state.last_active_at else None
        )
        if health_state is not None:
            health_state.record_plex_throttle(snapshot)

    # --- 1. Probe Plex ---
    try:
        xml = await _fetch_sessions_xml(plex)
    except Exception as exc:
        log.warning("plex_throttle: Plex unreachable (%s) — leaving qBit alone", exc)
        snapshot["error"] = f"plex: {exc}"
        snapshot["last_action"] = "plex_unreachable"
        _publish()
        return

    label, actives, transcodes = _classify(xml)
    snapshot["plex_state"] = label
    snapshot["active_sessions"] = actives
    snapshot["transcode_sessions"] = transcodes

    # --- 2. Update last-active timestamp ---
    if label != STATE_IDLE:
        state.last_active_at = now

    # --- 3. Compute target with grace ---
    if label == STATE_IDLE and state.last_active_at is not None:
        elapsed = now - state.last_active_at
        if elapsed < config.grace:
            # Still in grace window after a stream ended — hold the previous
            # cap to avoid mid-stream restore that would re-saturate disks if
            # the user is just paused or buffering. Use last_state_label so
            # the cap reflects the most-recent active class (transcode caps
            # transcode-grace, etc.).
            snapshot["in_grace"] = True
            target = _target_kbps(state.last_state_label, config)
        else:
            target = _target_kbps(STATE_IDLE, config)
    else:
        target = _target_kbps(label, config)

    snapshot["target_kbps"] = target

    # --- 4. Idempotent apply ---
    if state.current_limit_kbps == target:
        snapshot["last_action"] = "noop_unchanged"
        if label != STATE_IDLE:
            state.last_state_label = label
        _publish()
        return

    bytes_per_sec = max(0, target * 1024)  # kbps -> bytes/sec for qBit API

    try:
        await qbit.set_download_limit(bytes_per_sec)
    except Exception as exc:
        log.error(
            "plex_throttle: failed to apply qBit download limit %d kB/s: %s",
            target,
            exc,
        )
        snapshot["error"] = f"qbit: {exc}"
        snapshot["last_action"] = "qbit_apply_failed"
        _publish()
        return

    prev = state.current_limit_kbps
    state.current_limit_kbps = target
    if label != STATE_IDLE:
        state.last_state_label = label

    log.info(
        "plex_throttle: state=%s active=%d transcode=%d -> %d kB/s (was %s)",
        label,
        actives,
        transcodes,
        target,
        prev if prev is not None else "?",
    )
    await notifier.emit(
        "plex_throttle.applied",
        {
            "plex_state": label,
            "active_sessions": actives,
            "transcode_sessions": transcodes,
            "target_kbps": target,
            "previous_kbps": prev,
            "in_grace": snapshot["in_grace"],
        },
    )
    snapshot["last_action"] = "applied"
    _publish()


async def _fetch_sessions_xml(plex: PlexClient) -> str:
    """Fetch /status/sessions raw XML. Reuses PlexClient's httpx AsyncClient."""
    r = await plex._http.get(
        f"{plex._base}/status/sessions",
        params={"X-Plex-Token": plex._token},
    )
    r.raise_for_status()
    return r.text
