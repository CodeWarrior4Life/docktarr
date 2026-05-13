import json

import httpx
import pytest
from docktarr.arrclient import ArrClient
from docktarr.config import ArrAppConfig


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


# ---------------------------------------------------------------------------
# Download-client API tests (standalone, not in the class fixture)
# ---------------------------------------------------------------------------


def _client_with_handler(handler):
    app = ArrAppConfig(url="http://sonarr:8989", api_key="abc", name="Sonarr")
    c = ArrClient(app)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


@pytest.mark.asyncio
async def test_get_download_clients_returns_list():
    payload = [
        {
            "id": 1,
            "name": "qBittorrent",
            "enable": True,
            "fields": [
                {"name": "host", "value": "gluetun"},
                {"name": "port", "value": 8082},
            ],
        },
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v3/downloadclient"
        assert req.headers["X-Api-Key"] == "abc"
        return httpx.Response(200, json=payload)

    c = _client_with_handler(handler)
    result = await c.get_download_clients()
    assert result == payload


@pytest.mark.asyncio
async def test_test_download_client_pass():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v3/downloadclient/test"
        assert req.method == "POST"
        return httpx.Response(200, json={})

    c = _client_with_handler(handler)
    ok, status, body = await c.test_download_client(
        {"name": "qBittorrent", "fields": []}
    )
    assert ok is True
    assert status == 200
    assert body == "{}"


@pytest.mark.asyncio
async def test_test_download_client_fail_returns_body():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json=[{"propertyName": "host", "errorMessage": "Unable to connect"}],
        )

    c = _client_with_handler(handler)
    ok, status, body = await c.test_download_client({"name": "x"})
    assert ok is False
    assert status == 400
    assert "Unable to connect" in body


@pytest.mark.asyncio
async def test_put_download_client_host_rewrites_host_field():
    current = {
        "id": 1,
        "name": "qBittorrent",
        "enable": True,
        "fields": [
            {"name": "host", "value": "172.29.0.2"},
            {"name": "port", "value": 8082},
        ],
    }
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=current)
        captured["body"] = req.read()
        return httpx.Response(202)

    c = _client_with_handler(handler)
    ok = await c.put_download_client_host(client_id=1, new_host="gluetun")
    assert ok is True
    body = json.loads(captured["body"])
    fields = {f["name"]: f["value"] for f in body["fields"]}
    assert fields["host"] == "gluetun"
    assert fields["port"] == 8082
