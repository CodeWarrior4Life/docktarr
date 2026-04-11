import json

import httpx
import pytest
from doctarr.arrclient import ArrClient
from doctarr.config import ArrAppConfig


class TestArrClient:
    @pytest.fixture
    def mock_sonarr(self):
        queue_items = [
            {
                "id": 101,
                "downloadId": "ABC123",
                "title": "Test Episode",
                "sizeleft": 500,
            },
            {
                "id": 102,
                "downloadId": "DEF456",
                "title": "Another Episode",
                "sizeleft": 0,
            },
        ]
        removed = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            method = request.method
            if "/queue" in path and method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "records": queue_items,
                        "totalRecords": len(queue_items),
                    },
                )
            if "/queue/" in path and method == "DELETE":
                queue_id = int(path.split("/")[-1])
                removed.append(queue_id)
                return httpx.Response(200)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        config = ArrAppConfig(
            url="http://sonarr:8989", api_key="test-key", name="Sonarr"
        )
        client = ArrClient(config)
        client._client = httpx.AsyncClient(transport=transport)
        return client, removed

    async def test_get_queue(self, mock_sonarr):
        client, _ = mock_sonarr
        queue = await client.get_queue()
        assert len(queue) == 2
        assert queue[0]["downloadId"] == "ABC123"

    async def test_remove_and_blacklist(self, mock_sonarr):
        client, removed = mock_sonarr
        result = await client.remove_and_blacklist(101)
        assert result is True
        assert 101 in removed

    def test_api_version(self, mock_sonarr):
        client, _ = mock_sonarr
        assert client._api_version() == "v3"

    async def test_readarr_uses_v1(self):
        config = ArrAppConfig(url="http://readarr:8787", api_key="key", name="Readarr")
        client = ArrClient(config)
        assert client._api_version() == "v1"
