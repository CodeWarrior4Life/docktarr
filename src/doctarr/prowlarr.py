from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


def is_public_indexer(schema: dict) -> bool:
    """Return True if a schema entry represents a public torrent indexer."""
    if schema.get("protocol") != "torrent":
        return False
    return schema.get("privacy") == "public"


class ProwlarrClient:
    def __init__(self, client: httpx.AsyncClient, api_key: str):
        self._client = client
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key}

    async def get_indexer_schemas(self) -> list[dict]:
        resp = await self._client.get("/api/v1/indexer/schema", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def get_indexers(self) -> list[dict]:
        resp = await self._client.get("/api/v1/indexer", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def get_indexers_by_tag(self, tag_id: int) -> list[dict]:
        indexers = await self.get_indexers()
        return [i for i in indexers if tag_id in i.get("tags", [])]

    async def add_indexer(
        self, schema: dict, tag_ids: list[int], enable: bool = False
    ) -> dict:
        payload = {**schema, "tags": tag_ids, "enable": enable}
        payload.pop("id", None)
        if not payload.get("name"):
            payload["name"] = payload.get(
                "definitionName", payload.get("implementationName", "Unknown")
            )
        resp = await self._client.post(
            "/api/v1/indexer", json=payload, headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()

    async def update_indexer(self, indexer_id: int, data: dict) -> dict:
        resp = await self._client.put(
            f"/api/v1/indexer/{indexer_id}", json=data, headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()

    async def delete_indexer(self, indexer_id: int) -> None:
        resp = await self._client.delete(
            f"/api/v1/indexer/{indexer_id}", headers=self._headers()
        )
        resp.raise_for_status()

    async def test_indexer(self, indexer_data: dict) -> bool:
        """Test an indexer. Returns True if test passes, False otherwise."""
        try:
            resp = await self._client.post(
                "/api/v1/indexer/test", json=indexer_data, headers=self._headers()
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def enable_indexer(self, indexer: dict) -> dict:
        indexer["enable"] = True
        return await self.update_indexer(indexer["id"], indexer)

    async def get_tags(self) -> list[dict]:
        resp = await self._client.get("/api/v1/tag", headers=self._headers())
        resp.raise_for_status()
        return resp.json()

    async def create_tag(self, label: str) -> dict:
        resp = await self._client.post(
            "/api/v1/tag", json={"label": label}, headers=self._headers()
        )
        resp.raise_for_status()
        return resp.json()

    async def ensure_tag(self, label: str) -> int:
        """Get or create a tag by label. Returns the tag ID."""
        tags = await self.get_tags()
        for tag in tags:
            if tag["label"].lower() == label.lower():
                return tag["id"]
        new_tag = await self.create_tag(label)
        return new_tag["id"]
