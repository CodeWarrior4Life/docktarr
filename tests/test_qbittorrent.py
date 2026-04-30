import json

import httpx
import pytest
from docktarr.qbittorrent import QBitClient


class TestQBitClient:
    @pytest.fixture
    def mock_qbit(self):
        torrents = [
            {
                "hash": "abc123",
                "name": "Test Torrent",
                "progress": 0.5,
                "dlspeed": 0,
                "category": "tv-sonarr",
                "added_on": 1000000,
                "last_activity": 1000000,
                "num_seeds": 0,
                "num_complete": 0,
            },
            {
                "hash": "def456",
                "name": "Complete Torrent",
                "progress": 1.0,
                "dlspeed": 0,
                "category": "radarr",
                "added_on": 1000000,
                "last_activity": 1000000,
                "num_seeds": 5,
                "num_complete": 10,
            },
        ]
        deleted = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if "/auth/login" in path:
                resp = httpx.Response(200, text="Ok.")
                resp.headers["set-cookie"] = "SID=test123; path=/"
                return resp
            if "/torrents/info" in path:
                return httpx.Response(200, json=torrents)
            if "/torrents/delete" in path:
                deleted.append(request)
                return httpx.Response(200)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        # Override the internal client
        client = QBitClient("http://qbit:8082", "user", "pass")
        client._client = httpx.AsyncClient(transport=transport)
        return client, deleted

    async def test_login(self, mock_qbit):
        client, _ = mock_qbit
        await client.login()
        assert client._sid == "test123"

    async def test_get_torrents(self, mock_qbit):
        client, _ = mock_qbit
        await client.login()
        torrents = await client.get_torrents()
        assert len(torrents) == 2
        assert torrents[0]["hash"] == "abc123"

    async def test_delete_torrent(self, mock_qbit):
        client, deleted = mock_qbit
        await client.login()
        await client.delete_torrent("abc123")
        assert len(deleted) == 1
