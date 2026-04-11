import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from doctarr.arrclient import ArrClient
from doctarr.config import ArrAppConfig
from doctarr.imposter_detector import _parse_runtime_str, run_imposter_detector
from doctarr.notifier import Notifier


class TestParseRuntime:
    def test_hhmmss(self):
        assert abs(_parse_runtime_str("1:06:57") - 66.95) < 0.1

    def test_mmss(self):
        assert abs(_parse_runtime_str("42:30") - 42.5) < 0.1

    def test_empty(self):
        assert _parse_runtime_str("") is None

    def test_none(self):
        assert _parse_runtime_str(None) is None


class TestImposterDetector:
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

    def _make_sonarr(
        self,
        history: list[dict],
        episodes: dict[int, dict],
        deleted_files: list,
        commands: list,
    ) -> ArrClient:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            method = request.method

            if "/history" in path and method == "GET":
                return httpx.Response(200, json={"records": history})

            if "/episode/" in path and method == "GET":
                ep_id = int(path.split("/")[-1])
                ep = episodes.get(ep_id)
                if ep:
                    return httpx.Response(200, json=ep)
                return httpx.Response(404)

            if "/episodefile/" in path and method == "DELETE":
                file_id = int(path.split("/")[-1])
                deleted_files.append(file_id)
                return httpx.Response(200)

            if "/command" in path and method == "POST":
                body = json.loads(request.content)
                commands.append(body)
                return httpx.Response(201, json={"id": 1})

            return httpx.Response(404)

        config = ArrAppConfig(url="http://sonarr:8989", api_key="key", name="Sonarr")
        client = ArrClient(config)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client

    def _make_episode(
        self,
        ep_id: int,
        series: str,
        season: int,
        episode: int,
        title: str,
        expected_runtime: int,
        actual_runtime_str: str,
        file_id: int = 1,
    ) -> dict:
        return {
            "id": ep_id,
            "seriesId": 1,
            "seasonNumber": season,
            "episodeNumber": episode,
            "title": title,
            "runtime": expected_runtime,
            "hasFile": True,
            "series": {"title": series},
            "episodeFile": {
                "id": file_id,
                "relativePath": f"Season {season:02d}/{series} - S{season:02d}E{episode:02d}.mkv",
                "mediaInfo": {"runTime": actual_runtime_str},
            },
        }

    async def test_detects_imposter_episode(self, notifier, notifications):
        """Simulates Dark S01E05 being a completely different show (22 min instead of 51 min)."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 100, "date": now}]
        episodes = {
            100: self._make_episode(
                100, "Dark", 1, 5, "Truths", 51, "22:14", file_id=500
            )
        }
        deleted = []
        commands = []
        sonarr = self._make_sonarr(history, episodes, deleted, commands)

        await run_imposter_detector(
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            lookback=timedelta(hours=24),
            tolerance=0.40,
        )

        assert 500 in deleted
        assert any(c.get("name") == "EpisodeSearch" for c in commands)
        assert any(n["event"] == "imposter.detected" for n in notifications)
        assert "Dark" in notifications[0]["payload"]["name"]

    async def test_detects_last_frontier_imposter(self, notifier, notifications):
        """The Last Frontier episode replaced with different content (90 min instead of 44 min)."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 200, "date": now}]
        episodes = {
            200: self._make_episode(
                200,
                "The Last Frontier",
                1,
                3,
                "Into the Wild",
                44,
                "1:30:22",
                file_id=600,
            )
        }
        deleted = []
        commands = []
        sonarr = self._make_sonarr(history, episodes, deleted, commands)

        await run_imposter_detector(
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            lookback=timedelta(hours=24),
            tolerance=0.40,
        )

        assert 600 in deleted
        assert any(n["event"] == "imposter.detected" for n in notifications)

    async def test_passes_legitimate_episode(self, notifier, notifications):
        """Normal episode with runtime within tolerance."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 300, "date": now}]
        episodes = {
            300: self._make_episode(
                300, "Breaking Bad", 1, 1, "Pilot", 58, "57:42", file_id=700
            )
        }
        deleted = []
        commands = []
        sonarr = self._make_sonarr(history, episodes, deleted, commands)

        await run_imposter_detector(
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            lookback=timedelta(hours=24),
            tolerance=0.40,
        )

        assert len(deleted) == 0
        assert len(notifications) == 0

    async def test_skips_short_specials(self, notifier, notifications):
        """Short content like 5-min specials should be skipped."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 400, "date": now}]
        episodes = {
            400: self._make_episode(
                400, "Some Show", 0, 1, "Behind the Scenes", 5, "22:00", file_id=800
            )
        }
        deleted = []
        commands = []
        sonarr = self._make_sonarr(history, episodes, deleted, commands)

        await run_imposter_detector(
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            lookback=timedelta(hours=24),
            tolerance=0.40,
        )

        assert len(deleted) == 0

    async def test_no_sonarr_configured(self, notifier, notifications):
        """Gracefully handles no Sonarr in arr_clients."""
        await run_imposter_detector(
            arr_clients={},
            notifier=notifier,
            lookback=timedelta(hours=24),
            tolerance=0.40,
        )
        assert len(notifications) == 0
