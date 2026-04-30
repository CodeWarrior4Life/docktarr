"""Generic *arr API client for Sonarr/Radarr/Readarr queue management."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from docktarr.config import ArrAppConfig

log = logging.getLogger(__name__)


# Map qBit categories to arr app names
CATEGORY_MAP = {
    "tv-sonarr": "Sonarr",
    "sonarr": "Sonarr",
    "radarr": "Radarr",
    "readarr": "Readarr",
    "bookshelf": "Bookshelf",
}


class ArrClient:
    def __init__(self, config: ArrAppConfig):
        self.name = config.name
        self._url = config.url
        self._api_key = config.api_key
        self._client = httpx.AsyncClient(timeout=15.0)

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key}

    def _api_version(self) -> str:
        """Sonarr/Radarr use v3, Readarr/Bookshelf use v1."""
        if self.name in ("Sonarr", "Radarr"):
            return "v3"
        return "v1"

    async def get_queue(self) -> list[dict]:
        """Get all queue items."""
        v = self._api_version()
        items = []
        page = 1
        while True:
            resp = await self._client.get(
                f"{self._url}/api/{v}/queue",
                params={"page": page, "pageSize": 100},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            records = data.get("records", [])
            items.extend(records)
            if len(items) >= data.get("totalRecords", 0):
                break
            page += 1
        return items

    async def remove_and_blacklist(self, queue_id: int) -> bool:
        """Remove a queue item, blacklist the release, and trigger re-search."""
        v = self._api_version()
        try:
            resp = await self._client.delete(
                f"{self._url}/api/{v}/queue/{queue_id}",
                params={
                    "removeFromClient": "true",
                    "blocklist": "true",
                    "skipRedownload": "false",
                },
                headers=self._headers(),
            )
            return resp.status_code < 400
        except httpx.HTTPError as exc:
            log.warning(
                "Failed to remove queue item %d from %s: %s", queue_id, self.name, exc
            )
            return False

    async def close(self) -> None:
        await self._client.aclose()
