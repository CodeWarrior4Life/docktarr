"""Split-brain Plex detection.

Born from the 2026-06-21 split-brain incident: two Plex containers ran
simultaneously — one on Zion (10.0.0.16) and one on Cypher (10.0.0.111) —
sharing the SAME ``machineIdentifier`` (``984febb...``). Plex clients discover
servers by machineIdentifier, so with two live endpoints advertising one ID,
clients bound to whichever responded first. When a client bound to the weaker
Zion box, playback buffered. Two servers must never share an identity; the
intended topology is exactly one live instance per machineIdentifier.

This module DETECTS that condition. It queries each configured Plex endpoint's
``/identity`` for its ``machineIdentifier`` and groups reachable endpoints by
ID. If two or more *distinct, reachable* endpoints report the SAME
machineIdentifier, it emits ``plex_singleton.split_brain`` naming the colliding
endpoints — the operator must disable all but one.

Reuses :class:`docktarr.plex_api.PlexClient` for the ``/identity`` call. The
endpoint list is configurable (``PLEX_SINGLETON_ENDPOINTS``); the Plex token is
passed the same way ``plex_api`` does (``/identity`` is usually unauthenticated,
so an unreachable/forbidden endpoint simply drops out of the comparison set).
Every network call is timeout-bounded and wrapped — an unreachable endpoint is
expected and degrades to "skip", never a crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from docktarr.notifier import Notifier
from docktarr.plex_api import PlexClient

log = logging.getLogger(__name__)

_DEFAULT_ENDPOINTS = ["http://10.0.0.16:32400", "http://10.0.0.111:32400"]


@dataclass(frozen=True)
class PlexSingletonConfig:
    """Tunables for split-brain Plex detection."""

    enabled: bool = True
    endpoints: list[str] = field(
        default_factory=lambda: list(_DEFAULT_ENDPOINTS)
    )
    token: str = ""


async def _probe_identity(
    base_url: str, token: str, http: httpx.AsyncClient | None
) -> str | None:
    """Return an endpoint's machineIdentifier, or None if unreachable/missing."""
    client = PlexClient(base_url, token, http=http)
    try:
        identity = await client.identity()
    except Exception as exc:
        # Unreachable, 4xx, TLS, timeout — all expected for a dead endpoint.
        log.debug("plex_singleton: %s identity probe failed: %s", base_url, exc)
        return None
    finally:
        # Only close clients we own (caller may share an http session).
        if http is None:
            await client.close()
    mid = identity.get("machineIdentifier")
    if not mid:
        log.debug("plex_singleton: %s returned no machineIdentifier", base_url)
        return None
    return mid


async def run_plex_singleton(
    config: PlexSingletonConfig,
    notifier: Notifier,
    *,
    http: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Single-shot split-brain Plex probe.

    Returns a status dict:
      - reachable (dict[str, str]): endpoint -> machineIdentifier
      - split_brain (bool)
      - collisions (dict[str, list[str]]): machineIdentifier -> endpoints (only
        IDs shared by 2+ endpoints)

    Called by APScheduler on each tick. Never raises.
    """
    reachable: dict[str, str] = {}

    for endpoint in config.endpoints:
        mid = await _probe_identity(endpoint, config.token, http)
        if mid:
            reachable[endpoint] = mid

    # Group endpoints by machineIdentifier.
    by_id: dict[str, list[str]] = {}
    for endpoint, mid in reachable.items():
        by_id.setdefault(mid, []).append(endpoint)

    collisions = {mid: eps for mid, eps in by_id.items() if len(eps) >= 2}
    split_brain = bool(collisions)

    if split_brain:
        for mid, endpoints in collisions.items():
            log.warning(
                "plex_singleton: SPLIT-BRAIN — %d live instances share "
                "machineIdentifier %s: %s",
                len(endpoints),
                mid,
                ", ".join(endpoints),
            )
            await notifier.emit(
                "plex_singleton.split_brain",
                {
                    "count": len(endpoints),
                    "machine_identifier": mid,
                    "endpoints": ", ".join(endpoints),
                },
            )
    else:
        log.debug(
            "plex_singleton: ok — %d reachable endpoint(s), no shared identity",
            len(reachable),
        )

    return {
        "reachable": reachable,
        "split_brain": split_brain,
        "collisions": collisions,
    }
