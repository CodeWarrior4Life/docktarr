"""Imposter episode detection: finds mislabeled video files by comparing actual
duration against expected runtime from TVDB metadata."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from doctarr.arrclient import ArrClient
from doctarr.notifier import Notifier

log = logging.getLogger(__name__)

# How far off the runtime can be before flagging (percentage)
DEFAULT_TOLERANCE = 0.40  # 40% -- a 42-min episode at 25 or 59 min is suspicious

# Minimum expected runtime to check (skip very short content like specials)
MIN_RUNTIME_MINUTES = 10


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


async def run_imposter_detector(
    arr_clients: dict[str, ArrClient],
    notifier: Notifier,
    lookback: timedelta = timedelta(hours=24),
    tolerance: float = DEFAULT_TOLERANCE,
) -> None:
    """Scan recently imported episodes for duration mismatches."""
    log.info("Imposter detector: scanning recent imports")

    # Only check Sonarr (TV episodes are where this happens)
    sonarr = arr_clients.get("Sonarr")
    if not sonarr:
        log.debug("Imposter detector: no Sonarr configured, skipping")
        return

    flagged = 0

    try:
        # Get recent import history
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

        # Collect unique episode IDs from recent imports
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

        # Check each episode
        for ep_id in episode_ids:
            try:
                resp = await sonarr._client.get(
                    f"{sonarr._url}/api/v3/episode/{ep_id}",
                    headers=sonarr._headers(),
                )
                resp.raise_for_status()
                episode = resp.json()
            except Exception:
                continue

            expected_runtime = episode.get("runtime", 0)
            if expected_runtime < MIN_RUNTIME_MINUTES:
                continue

            ep_file = episode.get("episodeFile")
            if not ep_file:
                continue

            media_info = ep_file.get("mediaInfo", {})
            actual_runtime_str = media_info.get("runTime", "")
            actual_runtime = _parse_runtime_str(actual_runtime_str)

            if actual_runtime is None or actual_runtime < 1:
                continue

            # Compare
            diff_ratio = abs(actual_runtime - expected_runtime) / expected_runtime

            if diff_ratio > tolerance:
                series_title = episode.get("series", {}).get("title", "Unknown")
                season = episode.get("seasonNumber", 0)
                ep_num = episode.get("episodeNumber", 0)
                ep_title = episode.get("title", "")
                file_path = ep_file.get("relativePath", "")

                log.warning(
                    "IMPOSTER DETECTED: %s S%02dE%02d '%s' -- expected %dm, got %.0fm (%.0f%% off). File: %s",
                    series_title,
                    season,
                    ep_num,
                    ep_title,
                    expected_runtime,
                    actual_runtime,
                    diff_ratio * 100,
                    file_path,
                )

                flagged += 1

                # Blacklist this file and trigger re-search
                ep_file_id = ep_file.get("id")
                if ep_file_id:
                    try:
                        # Delete the episode file
                        await sonarr._client.delete(
                            f"{sonarr._url}/api/v3/episodefile/{ep_file_id}",
                            headers=sonarr._headers(),
                        )
                        # Trigger episode search
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

                await notifier.emit(
                    "imposter.detected",
                    {
                        "name": f"{series_title} S{season:02d}E{ep_num:02d} - {ep_title}",
                        "expected_minutes": expected_runtime,
                        "actual_minutes": f"{actual_runtime:.0f}",
                        "diff_percent": f"{diff_ratio * 100:.0f}",
                        "file": file_path,
                    },
                )

    except Exception as exc:
        log.warning("Imposter detector: error during scan: %s", exc)

    log.info("Imposter detector: complete. Flagged %d imposters.", flagged)
