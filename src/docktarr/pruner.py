from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from docktarr.notifier import Notifier
from docktarr.prowlarr import ProwlarrClient
from docktarr.state import IndexerStatus, StateStore

log = logging.getLogger(__name__)


async def run_pruner(
    prowlarr: ProwlarrClient,
    state: StateStore,
    notifier: Notifier,
    prune_threshold: timedelta,
) -> None:
    """Remove indexers that have been degraded beyond the threshold."""
    log.info("Pruner: scanning for indexers past %s threshold", prune_threshold)

    now = datetime.now(timezone.utc)
    degraded = state.get_by_status(IndexerStatus.DEGRADED)
    pruned_count = 0

    for entry in degraded:
        if entry.first_failure is None:
            continue
        downtime = now - entry.first_failure
        if downtime < prune_threshold:
            continue

        try:
            await prowlarr.delete_indexer(entry.prowlarr_id)
        except Exception as exc:
            log.warning(
                "Pruner: failed to delete %s (id=%d): %s",
                entry.definition_name,
                entry.prowlarr_id,
                exc,
            )
            continue

        downtime_hours = downtime.total_seconds() / 3600
        state.remove(entry.definition_name)
        pruned_count += 1
        log.info(
            "Pruner: removed %s (down %.1fh)", entry.definition_name, downtime_hours
        )

        await notifier.emit(
            "pruned",
            {
                "name": entry.definition_name,
                "downtime_hours": downtime_hours,
            },
        )

    state.save()
    log.info("Pruner: complete. Pruned %d indexers.", pruned_count)
