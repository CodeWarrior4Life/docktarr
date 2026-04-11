from __future__ import annotations

import logging

from doctarr.notifier import Notifier
from doctarr.prowlarr import ProwlarrClient, is_public_indexer
from doctarr.state import IndexerState, StateStore

log = logging.getLogger(__name__)


async def run_discovery(
    prowlarr: ProwlarrClient,
    state: StateStore,
    notifier: Notifier,
    tag_id: int,
) -> None:
    """Discover new public indexers from Prowlarr's schema and add as candidates."""
    log.info("Discovery: scanning Prowlarr schema for public indexers")

    app_profile_id = await prowlarr.get_app_profile_id()
    schemas = await prowlarr.get_indexer_schemas()
    public_schemas = [s for s in schemas if is_public_indexer(s)]
    log.info(
        "Discovery: found %d public torrent indexer definitions", len(public_schemas)
    )

    existing = await prowlarr.get_indexers_by_tag(tag_id)
    existing_names = {idx["definitionName"] for idx in existing}

    added_count = 0
    for schema in public_schemas:
        name = schema["definitionName"]
        if name in existing_names:
            continue

        try:
            result = await prowlarr.add_indexer(
                schema, tag_ids=[tag_id], enable=False, app_profile_id=app_profile_id
            )
            prowlarr_id = result["id"]
            state.set(IndexerState.new_candidate(name, prowlarr_id=prowlarr_id))
            added_count += 1
            log.info("Discovery: added candidate %s (id=%d)", name, prowlarr_id)
        except Exception as exc:
            log.warning("Discovery: failed to add %s: %s", name, exc)

    state.save()
    log.info("Discovery: complete. Added %d new candidates.", added_count)
