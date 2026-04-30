import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from docktarr.arrclient import ArrClient
from docktarr.config import ArrAppConfig
from docktarr.notifier import Notifier
from docktarr.qbittorrent import QBitClient
from docktarr.stall_detector import run_stall_detector


def _make_torrent(
    hash: str,
    name: str,
    progress: float,
    category: str,
    age_hours: float,
    idle_hours: float,
    dlspeed: int = 0,
) -> dict:
    now = datetime.now(timezone.utc).timestamp()
    return {
        "hash": hash,
        "name": name,
        "progress": progress,
        "category": category,
        "added_on": int(now - age_hours * 3600),
        "last_activity": int(now - idle_hours * 3600),
        "dlspeed": dlspeed,
        "num_seeds": 0,
        "num_complete": 0,
    }


class TestStallDetector:
    @pytest.fixture
    def notifications(self):
        return []

    @pytest.fixture
    def notifier(self, notifications):
        class CapturingNotifier(Notifier):
            async def emit(self, event, payload):
                notifications.append({"event": event, "payload": payload})

        transport = httpx.MockTransport(lambda r: httpx.Response(204))
        return CapturingNotifier(
            httpx.AsyncClient(transport=transport), webhook_url=None, enabled_events=[]
        )

    def _make_qbit(self, torrents: list[dict]) -> QBitClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if "/auth/login" in request.url.path:
                resp = httpx.Response(200, text="Ok.")
                resp.headers["set-cookie"] = "SID=test; path=/"
                return resp
            if "/torrents/info" in request.url.path:
                return httpx.Response(200, json=torrents)
            return httpx.Response(200)

        client = QBitClient("http://qbit:8082", "user", "pass")
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client._sid = "test"
        return client

    def _make_arr(self, name: str, queue: list[dict], removed: list) -> ArrClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and "/queue" in request.url.path:
                return httpx.Response(
                    200, json={"records": queue, "totalRecords": len(queue)}
                )
            if request.method == "DELETE":
                qid = int(request.url.path.split("/")[-1])
                removed.append(qid)
                return httpx.Response(200)
            return httpx.Response(404)

        config = ArrAppConfig(url="http://test:8989", api_key="key", name=name)
        client = ArrClient(config)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client

    async def test_clears_stalled_torrent(self, notifier, notifications):
        torrents = [
            _make_torrent(
                "abc123",
                "Stalled Show S01E01",
                0.5,
                "tv-sonarr",
                age_hours=24,
                idle_hours=12,
            ),
        ]
        qbit = self._make_qbit(torrents)

        removed = []
        sonarr_queue = [{"id": 101, "downloadId": "abc123", "title": "Stalled Show"}]
        sonarr = self._make_arr("Sonarr", sonarr_queue, removed)

        await run_stall_detector(
            qbit=qbit,
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            stall_threshold=timedelta(hours=6),
            protected_categories=["MAM"],
        )

        assert 101 in removed
        assert any(n["event"] == "stall.cleared" for n in notifications)

    async def test_skips_protected_category(self, notifier, notifications):
        torrents = [
            _make_torrent(
                "abc123", "MAM Book", 0.1, "MAM", age_hours=24, idle_hours=12
            ),
        ]
        qbit = self._make_qbit(torrents)

        removed = []
        sonarr = self._make_arr("Sonarr", [], removed)

        await run_stall_detector(
            qbit=qbit,
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            stall_threshold=timedelta(hours=6),
            protected_categories=["MAM"],
        )

        assert len(removed) == 0

    async def test_skips_young_torrent(self, notifier, notifications):
        torrents = [
            _make_torrent(
                "abc123", "New Show", 0.1, "tv-sonarr", age_hours=2, idle_hours=2
            ),
        ]
        qbit = self._make_qbit(torrents)

        removed = []
        sonarr_queue = [{"id": 101, "downloadId": "abc123"}]
        sonarr = self._make_arr("Sonarr", sonarr_queue, removed)

        await run_stall_detector(
            qbit=qbit,
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            stall_threshold=timedelta(hours=6),
            protected_categories=["MAM"],
        )

        assert len(removed) == 0

    async def test_skips_completed_torrent(self, notifier, notifications):
        torrents = [
            _make_torrent(
                "abc123", "Done Show", 1.0, "tv-sonarr", age_hours=24, idle_hours=12
            ),
        ]
        qbit = self._make_qbit(torrents)

        removed = []
        sonarr = self._make_arr("Sonarr", [], removed)

        await run_stall_detector(
            qbit=qbit,
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            stall_threshold=timedelta(hours=6),
            protected_categories=["MAM"],
        )

        assert len(removed) == 0

    async def test_skips_torrent_not_in_arr_queue(self, notifier, notifications):
        torrents = [
            _make_torrent(
                "abc123", "Manual Torrent", 0.5, "manual", age_hours=24, idle_hours=12
            ),
        ]
        qbit = self._make_qbit(torrents)

        removed = []
        sonarr = self._make_arr("Sonarr", [], removed)

        await run_stall_detector(
            qbit=qbit,
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            stall_threshold=timedelta(hours=6),
            protected_categories=["MAM"],
        )

        assert len(removed) == 0
