import httpx
import pytest
from doctarr.prowlarr import ProwlarrClient, is_public_indexer


def make_schema_entry(
    name: str,
    protocol: str = "torrent",
    privacy: str = "public",
    fields: list | None = None,
) -> dict:
    return {
        "id": 0,
        "definitionName": name,
        "implementationName": name,
        "implementation": "Cardigann",
        "protocol": protocol,
        "privacy": privacy,
        "name": "",
        "fields": fields or [],
        "tags": [],
        "enable": True,
    }


class TestIsPublicIndexer:
    def test_public_torrent(self):
        entry = make_schema_entry("1337x", protocol="torrent", privacy="public")
        assert is_public_indexer(entry) is True

    def test_private_torrent(self):
        entry = make_schema_entry("MAM", protocol="torrent", privacy="private")
        assert is_public_indexer(entry) is False

    def test_semi_private(self):
        entry = make_schema_entry(
            "SomeTracker", protocol="torrent", privacy="semiPrivate"
        )
        assert is_public_indexer(entry) is False

    def test_usenet_skipped(self):
        entry = make_schema_entry("NZBgeek", protocol="usenet", privacy="public")
        assert is_public_indexer(entry) is False


class TestProwlarrClient:
    @pytest.fixture
    def mock_client(self):
        schemas = [
            make_schema_entry("1337x"),
            make_schema_entry("TPB"),
            make_schema_entry("MAM", privacy="private"),
        ]
        indexers = [
            {
                "id": 1,
                "definitionName": "1337x",
                "name": "1337x",
                "tags": [5],
                "enable": True,
            },
        ]
        tags = [{"id": 5, "label": "doctarr"}]

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            method = request.method
            if path == "/api/v1/indexer/schema" and method == "GET":
                return httpx.Response(200, json=schemas)
            if path == "/api/v1/indexer" and method == "GET":
                return httpx.Response(200, json=indexers)
            if path == "/api/v1/indexer/test" and method == "POST":
                return httpx.Response(200, json={})
            if path == "/api/v1/indexer/1" and method == "PUT":
                return httpx.Response(200, json={"id": 1, "enable": True})
            if path == "/api/v1/indexer/1" and method == "DELETE":
                return httpx.Response(200)
            if path == "/api/v1/tag" and method == "GET":
                return httpx.Response(200, json=tags)
            if path == "/api/v1/tag" and method == "POST":
                return httpx.Response(201, json={"id": 10, "label": "doctarr"})
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport, base_url="http://prowlarr:9696")
        return ProwlarrClient(http, api_key="test-key")

    async def test_get_public_schemas(self, mock_client):
        schemas = await mock_client.get_indexer_schemas()
        public = [s for s in schemas if is_public_indexer(s)]
        assert len(public) == 2
        names = {s["definitionName"] for s in public}
        assert names == {"1337x", "TPB"}

    async def test_get_indexers(self, mock_client):
        indexers = await mock_client.get_indexers()
        assert len(indexers) == 1
        assert indexers[0]["definitionName"] == "1337x"

    async def test_get_indexers_by_tag(self, mock_client):
        indexers = await mock_client.get_indexers_by_tag(5)
        assert len(indexers) == 1

    async def test_test_indexer_success(self, mock_client):
        result = await mock_client.test_indexer({"id": 1, "name": "1337x"})
        assert result is True

    async def test_ensure_tag_existing(self, mock_client):
        tag_id = await mock_client.ensure_tag("doctarr")
        assert tag_id == 5

    async def test_delete_indexer(self, mock_client):
        await mock_client.delete_indexer(1)  # should not raise
