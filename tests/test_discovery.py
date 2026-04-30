import json as _json

import httpx
import pytest
from docktarr.discovery import run_discovery
from docktarr.notifier import Notifier
from docktarr.prowlarr import ProwlarrClient
from docktarr.state import IndexerStatus, StateStore


def make_schema(name: str, privacy: str = "public", protocol: str = "torrent") -> dict:
    return {
        "id": 0,
        "definitionName": name,
        "implementationName": name,
        "implementation": "Cardigann",
        "protocol": protocol,
        "privacy": privacy,
        "name": "",
        "fields": [],
        "tags": [],
        "enable": True,
    }


class TestDiscovery:
    @pytest.fixture
    def state_store(self, tmp_path):
        return StateStore(tmp_path / "state.json")

    @pytest.fixture
    def notifier(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(204))
        return Notifier(
            httpx.AsyncClient(transport=transport),
            webhook_url=None,
            enabled_events=[],
        )

    def _make_prowlarr(
        self, schemas: list[dict], existing_indexers: list[dict], tags: list[dict]
    ) -> tuple[ProwlarrClient, list]:
        added_indexers = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            method = request.method
            if path == "/api/v1/appprofile" and method == "GET":
                return httpx.Response(200, json=[{"id": 1, "name": "Standard"}])
            if path == "/api/v1/indexer/schema" and method == "GET":
                return httpx.Response(200, json=schemas)
            if path == "/api/v1/indexer" and method == "GET":
                return httpx.Response(200, json=existing_indexers + added_indexers)
            if path == "/api/v1/indexer" and method == "POST":
                body = _json.loads(request.content)
                new = {"id": 100 + len(added_indexers), **body}
                added_indexers.append(new)
                return httpx.Response(201, json=new)
            if path == "/api/v1/tag" and method == "GET":
                return httpx.Response(200, json=tags)
            if path == "/api/v1/tag" and method == "POST":
                return httpx.Response(201, json={"id": 5, "label": "docktarr"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport, base_url="http://prowlarr:9696")
        return ProwlarrClient(http, api_key="test"), added_indexers

    async def test_discovers_new_public_indexers(self, state_store, notifier):
        schemas = [
            make_schema("1337x"),
            make_schema("TPB"),
            make_schema("MAM", privacy="private"),
        ]
        prowlarr, added = self._make_prowlarr(
            schemas, [], [{"id": 5, "label": "docktarr"}]
        )

        await run_discovery(prowlarr, state_store, notifier, tag_id=5)

        assert len(added) == 2
        names = {a["definitionName"] for a in added}
        assert names == {"1337x", "TPB"}
        assert state_store.get("1337x").status == IndexerStatus.CANDIDATE
        assert state_store.get("TPB").status == IndexerStatus.CANDIDATE

    async def test_skips_already_managed_indexers(self, state_store, notifier):
        schemas = [make_schema("1337x"), make_schema("TPB")]
        existing = [{"id": 1, "definitionName": "1337x", "tags": [5], "enable": True}]
        prowlarr, added = self._make_prowlarr(
            schemas, existing, [{"id": 5, "label": "docktarr"}]
        )

        await run_discovery(prowlarr, state_store, notifier, tag_id=5)

        assert len(added) == 1
        assert added[0]["definitionName"] == "TPB"

    async def test_skips_usenet_indexers(self, state_store, notifier):
        schemas = [make_schema("NZBgeek", protocol="usenet")]
        prowlarr, added = self._make_prowlarr(
            schemas, [], [{"id": 5, "label": "docktarr"}]
        )

        await run_discovery(prowlarr, state_store, notifier, tag_id=5)

        assert len(added) == 0
