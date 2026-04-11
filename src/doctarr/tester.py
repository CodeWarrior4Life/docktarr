from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from doctarr.notifier import Notifier
from doctarr.prowlarr import ProwlarrClient
from doctarr.state import IndexerState, IndexerStatus, StateStore

log = logging.getLogger(__name__)


async def run_tester(
    prowlarr: ProwlarrClient,
    state: StateStore,
    notifier: Notifier,
    tag_id: int,
    test_delay: float = 2.0,
) -> None:
    """Test all doctarr-managed indexers and update their state."""
    log.info("Tester: checking indexer health")

    indexers = await prowlarr.get_indexers_by_tag(tag_id)
    indexer_map = {idx["definitionName"]: idx for idx in indexers}

    all_states = list(state.all())
    tested = 0

    for entry in all_states:
        indexer = indexer_map.get(entry.definition_name)
        if indexer is None:
            state.remove(entry.definition_name)
            continue

        entry.prowlarr_id = indexer["id"]
        passed = await prowlarr.test_indexer(indexer)
        now = datetime.now(timezone.utc)
        entry.last_tested = now
        tested += 1

        if passed:
            await _handle_pass(entry, indexer, prowlarr, notifier, now)
        else:
            await _handle_fail(entry, notifier, now)

        state.set(entry)

        if test_delay > 0:
            await asyncio.sleep(test_delay)

    state.save()
    log.info("Tester: complete. Tested %d indexers.", tested)


async def _handle_pass(
    entry: IndexerState,
    indexer: dict,
    prowlarr: ProwlarrClient,
    notifier: Notifier,
    now: datetime,
) -> None:
    if entry.status == IndexerStatus.CANDIDATE:
        entry.status = IndexerStatus.ACTIVE
        try:
            await prowlarr.enable_indexer(indexer)
        except Exception as exc:
            log.warning("Tester: failed to enable %s: %s", entry.definition_name, exc)
        await notifier.emit(
            "added",
            {
                "name": entry.definition_name,
                "tested_at": now.isoformat(),
            },
        )
        log.info("Tester: %s passed, promoting to active", entry.definition_name)
    elif entry.status == IndexerStatus.DEGRADED:
        entry.status = IndexerStatus.ACTIVE
        entry.failure_count = 0
        entry.first_failure = None
        entry.last_failure = None
        log.info("Tester: %s recovered, back to active", entry.definition_name)


async def _handle_fail(
    entry: IndexerState,
    notifier: Notifier,
    now: datetime,
) -> None:
    if entry.status == IndexerStatus.CANDIDATE:
        entry.failure_count += 1
        log.info(
            "Tester: candidate %s failed (count=%d)",
            entry.definition_name,
            entry.failure_count,
        )
    elif entry.status == IndexerStatus.ACTIVE:
        entry.status = IndexerStatus.DEGRADED
        entry.failure_count = 1
        entry.first_failure = now
        entry.last_failure = now
        log.info("Tester: %s failed, marking degraded", entry.definition_name)
        await notifier.emit(
            "degraded",
            {
                "name": entry.definition_name,
                "failure_count": entry.failure_count,
                "first_failure": now.isoformat(),
            },
        )
    elif entry.status == IndexerStatus.DEGRADED:
        entry.failure_count += 1
        entry.last_failure = now
        log.info(
            "Tester: %s still degraded (count=%d)",
            entry.definition_name,
            entry.failure_count,
        )
