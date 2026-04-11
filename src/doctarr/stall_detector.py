"""Stall detection: finds stuck downloads and triggers re-search via *arr APIs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from doctarr.arrclient import ArrClient, CATEGORY_MAP
from doctarr.config import ArrAppConfig
from doctarr.notifier import Notifier
from doctarr.qbittorrent import QBitClient

log = logging.getLogger(__name__)


async def run_stall_detector(
    qbit: QBitClient,
    arr_clients: dict[str, ArrClient],
    notifier: Notifier,
    stall_threshold: timedelta,
    protected_categories: list[str],
) -> None:
    """Detect stalled torrents and clear them via the owning *arr app."""
    log.info("Stall detector: scanning for stuck downloads")

    try:
        torrents = await qbit.get_torrents()
    except Exception as exc:
        log.warning("Stall detector: failed to get torrents from qBit: %s", exc)
        return

    now = datetime.now(timezone.utc)
    threshold_secs = stall_threshold.total_seconds()
    cleared = 0

    # Build a map of download hashes -> arr queue items for fast lookup
    arr_queues: dict[str, list[tuple[ArrClient, dict]]] = {}
    for client in arr_clients.values():
        try:
            queue = await client.get_queue()
            for item in queue:
                dl_id = item.get("downloadId", "").lower()
                if dl_id:
                    arr_queues.setdefault(dl_id, []).append((client, item))
        except Exception as exc:
            log.warning(
                "Stall detector: failed to get queue from %s: %s", client.name, exc
            )

    for torrent in torrents:
        progress = torrent.get("progress", 0)
        if progress >= 1.0:
            continue

        category = torrent.get("category", "")
        if category in protected_categories:
            continue

        # Check if torrent is stalled
        dlspeed = torrent.get("dlspeed", 0)
        added_on = torrent.get("added_on", 0)
        age_secs = (now.timestamp() - added_on) if added_on else 0

        # Skip torrents younger than the threshold
        if age_secs < threshold_secs:
            continue

        # A torrent is stalled if it has no download speed and has been
        # in the queue longer than the threshold. We use last_activity
        # (timestamp of last data transfer) as the primary signal.
        last_activity = torrent.get("last_activity", 0)
        if last_activity > 0:
            idle_secs = now.timestamp() - last_activity
        else:
            idle_secs = age_secs

        if idle_secs < threshold_secs and dlspeed > 0:
            continue

        # This torrent is stalled -- find it in an *arr queue and clear it
        hash_lower = torrent.get("hash", "").lower()
        name = torrent.get("name", "unknown")[:70]

        arr_entries = arr_queues.get(hash_lower, [])
        if not arr_entries:
            # Not managed by any *arr app -- might be manual. Skip.
            log.debug("Stall detector: %s not in any *arr queue, skipping", name)
            continue

        # Remove from the first matching *arr app (usually there's only one)
        client, queue_item = arr_entries[0]
        queue_id = queue_item.get("id")
        idle_hours = idle_secs / 3600

        log.info(
            "Stall detector: clearing %s (idle %.1fh, progress %.0f%%) via %s",
            name,
            idle_hours,
            progress * 100,
            client.name,
        )

        success = await client.remove_and_blacklist(queue_id)
        if success:
            cleared += 1
            await notifier.emit(
                "stall.cleared",
                {
                    "name": name,
                    "idle_hours": idle_hours,
                    "progress": f"{progress * 100:.0f}%",
                    "app": client.name,
                },
            )

    log.info("Stall detector: complete. Cleared %d stalled downloads.", cleared)
