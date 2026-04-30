"""ARR service reachability checks for Doctarr.

Ported from arr-orchestrator/services.py ``check_arr_services`` (reachability
path only).

Behavior faithfully ported
--------------------------
1. For each ArrClient in ``arr_clients``, probe the ``/api/{version}/health``
   endpoint with the app's API key.
2. If the request raises any exception (connection refused, timeout, etc.)
   → emit ``service.down`` notifier event.
3. If the response status is not 200 → emit ``service.down``.
4. If reachable, parse the health JSON array and log any items with a
   "download"-related source and type == "error".
5. Return a list of per-service status dicts.

Deviations from orchestrator
-----------------------------
- Orchestrator's ``check_prowlarr_indexers`` (disabled indexer retest via
  TestAllIndexers + MAM renewal message) is NOT ported here.  Doctarr's
  existing ``tester.py`` already handles Prowlarr indexer health; duplicating
  that logic would violate the no-duplication constraint.
  DONE_WITH_CONCERNS: if operators relied on the auto-retest trigger from the
  orchestrator, they should verify that ``tester.py`` covers the gap.
- Orchestrator probed ``http://{host}:{port}/api/{version}/health`` built from
  raw host/port config fields.  Doctarr's ``ArrClient`` already holds a full
  ``_url`` and ``_api_key``; we derive the health URL from those rather than
  re-parsing host/port.  This keeps the two in sync automatically.
- ArrClient already has ``_api_version()`` which returns "v3" for
  Sonarr/Radarr and "v1" for others.  We reuse that.
- Orchestrator used a fresh ``httpx.AsyncClient`` per call.  Doctarr reuses
  each ArrClient's internal ``_client`` for consistency with the rest of the
  codebase.  The ``/health`` endpoint is a GET so this is safe.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from docktarr.arrclient import ArrClient
from docktarr.notifier import Notifier

log = logging.getLogger(__name__)


async def run_arr_services(
    arr_clients: dict[str, ArrClient],
    notifier: Notifier,
) -> list[dict[str, Any]]:
    """Single-shot ARR service reachability probe.

    Parameters
    ----------
    arr_clients:
        Mapping of service name → ArrClient instance (e.g. ``{"Sonarr": ...}``).
    notifier:
        Doctarr notifier for ``service.down`` events.

    Returns
    -------
    list of per-service status dicts with keys:
      - name (str)
      - url (str)  — the health endpoint that was probed
      - status (str): "ok" | "down"
      - http_status (int | None)  — None if connection failed
      - error (str | None)  — set on connection failure
    """
    results: list[dict[str, Any]] = []

    for name, client in arr_clients.items():
        result = await _check_service(name, client, notifier)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _check_service(
    name: str, client: ArrClient, notifier: Notifier
) -> dict[str, Any]:
    """Probe the /api/{version}/health endpoint for a single ARR app."""
    version = client._api_version()
    url = f"{client._url}/api/{version}/health"
    params = {"apikey": client._api_key}

    try:
        resp = await client._client.get(url, params=params, timeout=10.0)
    except Exception as exc:
        log.warning("arr_services: %s unreachable at %s — %s", name, url, exc)
        await notifier.emit(
            "service.down",
            {"name": name, "url": url, "reason": str(exc)},
        )
        return {
            "name": name,
            "url": url,
            "status": "down",
            "http_status": None,
            "error": str(exc),
        }

    if resp.status_code != 200:
        log.warning(
            "arr_services: %s health endpoint returned HTTP %d — %s",
            name,
            resp.status_code,
            url,
        )
        await notifier.emit(
            "service.down",
            {"name": name, "url": url, "reason": f"HTTP {resp.status_code}"},
        )
        return {
            "name": name,
            "url": url,
            "status": "down",
            "http_status": resp.status_code,
            "error": None,
        }

    # Parse health array and warn on download-related errors (mirrors orchestrator).
    try:
        health_items = resp.json()
    except Exception:
        health_items = []

    for item in health_items:
        source = item.get("source", "") or ""
        item_type = item.get("type", "") or ""
        if "download" in source.lower() and item_type.lower() == "error":
            log.warning(
                "arr_services: %s health warning — source=%s type=%s message=%s",
                name,
                source,
                item_type,
                item.get("message", ""),
            )

    log.debug("arr_services: %s reachable (HTTP 200)", name)
    return {
        "name": name,
        "url": url,
        "status": "ok",
        "http_status": 200,
        "error": None,
    }
