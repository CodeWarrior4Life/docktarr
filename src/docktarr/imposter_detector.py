"""Imposter episode detection.

Finds mislabeled video files by combining multiple heuristics:
  (1) actual duration vs expected TVDB runtime ("runtime" heuristic)
  (2) quality source vs series network ("source/network" heuristic) -- a
      streaming-only network (Netflix, Apple TV+, ...) tagged with a broadcast
      source (television/televisionRaw) is impossible and flags as imposter.
      This catches the case where runtimes are superficially plausible but the
      file is not actually the episode it claims to be.

Two scan modes:
  * recent: `run_imposter_detector` -- scans downloadFolderImported history
    within `lookback` (cheap, runs hourly by default).
  * backfill: `run_imposter_backfill` -- walks every monitored series and
    evaluates every episode file (expensive, runs weekly by default). This
    retro-catches old imposters imported before a heuristic existed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from doctarr.arrclient import ArrClient
from doctarr.notifier import Notifier

log = logging.getLogger(__name__)

DEFAULT_TOLERANCE = 0.40
MIN_RUNTIME_MINUTES = 10

STREAMING_ONLY_NETWORKS: frozenset[str] = frozenset(
    {
        "netflix",
        "amazon",
        "amazon prime video",
        "prime video",
        "apple tv+",
        "apple tv plus",
        "disney+",
        "disney plus",
        "hulu",
        "paramount+",
        "paramount plus",
        "hbo max",
        "max",
        "peacock",
        "crunchyroll",
    }
)
# Quality sources that correspond to over-the-air broadcast -- impossible for a
# streaming-only original (no broadcast exists to rip from).
BROADCAST_SOURCES: frozenset[str] = frozenset({"television", "televisionraw"})


def _parse_runtime_str(runtime_str: str) -> float | None:
    """Parse Sonarr's mediaInfo runTime string like '1:06:57' or '42:30' to minutes."""
    if not runtime_str:
        return None
    parts = runtime_str.strip().split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 60 + int(parts[1]) + int(parts[2]) / 60
        if len(parts) == 2:
            return int(parts[0]) + int(parts[1]) / 60
        return float(parts[0])
    except (ValueError, IndexError):
        return None


def _evaluate_episode(episode: dict, tolerance: float) -> tuple[bool, str | None]:
    """Return (is_imposter, reason). Reasons: 'runtime_short', 'runtime_long',
    'streaming_with_broadcast_source'. None when file should be skipped."""
    ep_file = episode.get("episodeFile")
    if not ep_file:
        return (False, None)

    # Skip multi-episode files (E01-E02, E01E02 patterns)
    file_path_raw = ep_file.get("relativePath", "")
    if re.search(r"E\d+[-E]+E?\d+", file_path_raw, re.IGNORECASE):
        return (False, None)

    # --- Heuristic 2: streaming-only network vs broadcast source ---
    # Runs first because it is unambiguous and independent of runtime metadata.
    series = episode.get("series") or {}
    network = (series.get("network") or "").strip().lower()
    quality_source = (
        (((ep_file.get("quality") or {}).get("quality") or {}).get("source") or "")
        .strip()
        .lower()
    )
    if (
        network
        and network in STREAMING_ONLY_NETWORKS
        and quality_source in BROADCAST_SOURCES
    ):
        return (True, "streaming_with_broadcast_source")

    # --- Heuristic 1: actual runtime vs expected ---
    expected_runtime = episode.get("runtime", 0)
    if expected_runtime < MIN_RUNTIME_MINUTES:
        return (False, None)

    actual_runtime = _parse_runtime_str(
        (ep_file.get("mediaInfo") or {}).get("runTime", "")
    )
    if actual_runtime is None or actual_runtime < 1:
        return (False, None)

    ratio = actual_runtime / expected_runtime
    if ratio < (1 - tolerance):
        return (True, "runtime_short")
    if ratio > 2.3:
        return (True, "runtime_long")
    return (False, None)


async def _fetch_episode(sonarr: ArrClient, ep_id: int) -> dict | None:
    try:
        resp = await sonarr._client.get(
            f"{sonarr._url}/api/v3/episode/{ep_id}",
            params={"includeSeries": "true"},
            headers=sonarr._headers(),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


async def _remediate(
    sonarr: ArrClient, episode: dict, reason: str, notifier: Notifier
) -> None:
    """Delete the offending file, trigger re-search, and emit notification."""
    ep_file = episode.get("episodeFile") or {}
    ep_file_id = ep_file.get("id")
    ep_id = episode.get("id")
    series_title = (episode.get("series") or {}).get("title", "Unknown")
    season = episode.get("seasonNumber", 0)
    ep_num = episode.get("episodeNumber", 0)
    ep_title = episode.get("title", "")
    file_path = ep_file.get("relativePath", "")

    expected_runtime = episode.get("runtime", 0)
    actual_runtime = (
        _parse_runtime_str((ep_file.get("mediaInfo") or {}).get("runTime", "")) or 0
    )
    quality_source = ((ep_file.get("quality") or {}).get("quality") or {}).get(
        "source"
    ) or ""
    network = (episode.get("series") or {}).get("network") or ""

    log.warning(
        "IMPOSTER DETECTED (%s): %s S%02dE%02d '%s' -- expected %dm, got %.0fm, source=%s, network=%s. File: %s",
        reason,
        series_title,
        season,
        ep_num,
        ep_title,
        expected_runtime,
        actual_runtime,
        quality_source or "?",
        network or "?",
        file_path,
    )

    if ep_file_id:
        try:
            await sonarr._client.delete(
                f"{sonarr._url}/api/v3/episodefile/{ep_file_id}",
                headers=sonarr._headers(),
            )
            if ep_id:
                await sonarr._client.post(
                    f"{sonarr._url}/api/v3/command",
                    json={"name": "EpisodeSearch", "episodeIds": [ep_id]},
                    headers=sonarr._headers(),
                )
            log.info(
                "Imposter detector: deleted file and triggered re-search for %s S%02dE%02d",
                series_title,
                season,
                ep_num,
            )
        except Exception as exc:
            log.warning("Imposter detector: failed to remediate: %s", exc)

    payload: dict = {
        "name": f"{series_title} S{season:02d}E{ep_num:02d} - {ep_title}",
        "reason": reason,
        "file": file_path,
    }
    if actual_runtime:
        payload["actual_minutes"] = f"{actual_runtime:.0f}"
    if expected_runtime:
        payload["expected_minutes"] = expected_runtime
    if quality_source:
        payload["quality_source"] = quality_source
    if network:
        payload["network"] = network
    await notifier.emit("imposter.detected", payload)


async def run_imposter_detector(
    arr_clients: dict[str, ArrClient],
    notifier: Notifier,
    lookback: timedelta = timedelta(hours=24),
    tolerance: float = DEFAULT_TOLERANCE,
) -> None:
    """Scan recently imported episodes for imposter signals."""
    log.info("Imposter detector: scanning recent imports")

    sonarr = arr_clients.get("Sonarr")
    if not sonarr:
        log.debug("Imposter detector: no Sonarr configured, skipping")
        return

    flagged = 0
    try:
        now = datetime.now(timezone.utc)
        cutoff = now - lookback

        resp = await sonarr._client.get(
            f"{sonarr._url}/api/v3/history",
            params={
                "pageSize": 100,
                "sortKey": "date",
                "sortDirection": "descending",
                "eventType": "downloadFolderImported",
            },
            headers=sonarr._headers(),
        )
        resp.raise_for_status()
        history = resp.json().get("records", [])

        episode_ids = set()
        for record in history:
            record_date = record.get("date", "")
            if record_date and record_date[:19] < cutoff.strftime("%Y-%m-%dT%H:%M:%S"):
                continue
            ep_id = record.get("episodeId")
            if ep_id:
                episode_ids.add(ep_id)

        if not episode_ids:
            log.info("Imposter detector: no recent imports to check")
            return

        log.info(
            "Imposter detector: checking %d recently imported episodes",
            len(episode_ids),
        )

        for ep_id in episode_ids:
            episode = await _fetch_episode(sonarr, ep_id)
            if not episode:
                continue
            is_imposter, reason = _evaluate_episode(episode, tolerance)
            if is_imposter and reason:
                await _remediate(sonarr, episode, reason, notifier)
                flagged += 1

    except Exception as exc:
        log.warning("Imposter detector: error during scan: %s", exc)

    log.info("Imposter detector: complete. Flagged %d imposters.", flagged)


async def run_imposter_backfill(
    arr_clients: dict[str, ArrClient],
    notifier: Notifier,
    tolerance: float = DEFAULT_TOLERANCE,
) -> None:
    """Full-library scan: walk every monitored series and evaluate every
    episode file. Meant to run on a slow cadence (weekly). Catches imposters
    imported before a heuristic existed."""
    log.info("Imposter backfill: starting full library scan")

    sonarr = arr_clients.get("Sonarr")
    if not sonarr:
        log.debug("Imposter backfill: no Sonarr configured, skipping")
        return

    flagged = 0
    scanned = 0
    try:
        resp = await sonarr._client.get(
            f"{sonarr._url}/api/v3/series",
            headers=sonarr._headers(),
        )
        resp.raise_for_status()
        series_list = resp.json()

        for series in series_list:
            if not series.get("monitored"):
                continue
            series_id = series.get("id")
            if not series_id:
                continue

            ep_resp = await sonarr._client.get(
                f"{sonarr._url}/api/v3/episode",
                params={"seriesId": series_id, "includeEpisodeFile": "true"},
                headers=sonarr._headers(),
            )
            if ep_resp.status_code != 200:
                continue
            episodes = ep_resp.json()

            for episode in episodes:
                if not episode.get("hasFile"):
                    continue
                # Inject series for _evaluate_episode / _remediate
                episode.setdefault("series", series)
                scanned += 1
                is_imposter, reason = _evaluate_episode(episode, tolerance)
                if is_imposter and reason:
                    await _remediate(sonarr, episode, reason, notifier)
                    flagged += 1

    except Exception as exc:
        log.warning("Imposter backfill: error during scan: %s", exc)

    log.info(
        "Imposter backfill: complete. Scanned %d episodes, flagged %d imposters.",
        scanned,
        flagged,
    )
