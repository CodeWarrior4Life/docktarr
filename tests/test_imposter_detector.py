import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from doctarr.arrclient import ArrClient
from doctarr.config import ArrAppConfig
from doctarr.imposter_detector import (
    _parse_runtime_str,
    run_imposter_backfill,
    run_imposter_detector,
)
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
        custom_path: str | None = None,
        network: str | None = None,
        quality_source: str | None = None,
    ) -> dict:
        path = (
            custom_path
            or f"Season {season:02d}/{series} - S{season:02d}E{episode:02d}.mkv"
        )
        ep_file: dict = {
            "id": file_id,
            "relativePath": path,
            "mediaInfo": {"runTime": actual_runtime_str},
        }
        if quality_source is not None:
            ep_file["quality"] = {
                "quality": {"name": "HDTV-1080p", "source": quality_source}
            }
        series_obj: dict = {"title": series}
        if network is not None:
            series_obj["network"] = network
        return {
            "id": ep_id,
            "seriesId": 1,
            "seasonNumber": season,
            "episodeNumber": episode,
            "title": title,
            "runtime": expected_runtime,
            "hasFile": True,
            "series": series_obj,
            "episodeFile": ep_file,
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

    async def test_detects_short_imposter(self, notifier, notifications):
        """The Last Frontier episode replaced with much shorter content (20 min instead of 54 min)."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 200, "date": now}]
        episodes = {
            200: self._make_episode(
                200,
                "The Last Frontier",
                1,
                3,
                "Into the Wild",
                54,
                "20:15",
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

    async def test_skips_double_episode_file(self, notifier, notifications):
        """A file containing E01-E02 (double episode) should not be flagged."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 500, "date": now}]
        episodes = {
            500: self._make_episode(
                500,
                "Abbott Elementary",
                3,
                1,
                "Career Day",
                22,
                "44:10",
                file_id=900,
                custom_path="Season 03/Abbott Elementary - S03E01-E02 - Career Day.mkv",
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

    async def test_allows_slightly_longer_episode(self, notifier, notifications):
        """Episode at 2x expected (double-length finale) should NOT be flagged."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 600, "date": now}]
        episodes = {
            600: self._make_episode(
                600,
                "Some Show",
                1,
                10,
                "Finale",
                42,
                "1:22:00",
                file_id=1000,
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

    async def test_detects_streaming_with_broadcast_source(
        self, notifier, notifications
    ):
        """Real case: Dark (Netflix) with HDTV source + plausible runtime.
        Runtime alone can't catch this -- the file is 42m vs 44m expected (4.5% off),
        well within any reasonable tolerance. The network/source mismatch is the
        decisive signal: Netflix never airs on broadcast TV."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 2381, "date": now}]
        episodes = {
            2381: self._make_episode(
                2381,
                "Dark",
                1,
                2,
                "Lies",
                44,
                "42:17",
                file_id=819,
                network="Netflix",
                quality_source="television",
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

        assert 819 in deleted
        assert any(c.get("name") == "EpisodeSearch" for c in commands)
        assert any(
            n["event"] == "imposter.detected"
            and n["payload"]["reason"] == "streaming_with_broadcast_source"
            for n in notifications
        )

    async def test_passes_streaming_with_web_source(self, notifier, notifications):
        """Netflix show with WEBDL source and plausible runtime -- legitimate."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 2382, "date": now}]
        episodes = {
            2382: self._make_episode(
                2382,
                "Dark",
                1,
                3,
                "Past and Present",
                57,
                "56:30",
                file_id=820,
                network="Netflix",
                quality_source="web",
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

    async def test_passes_broadcast_show_with_broadcast_source(
        self, notifier, notifications
    ):
        """AMC show (Breaking Bad) with HDTV source is legitimate -- broadcast network
        is expected to have broadcast sources."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 2500, "date": now}]
        episodes = {
            2500: self._make_episode(
                2500,
                "Breaking Bad",
                1,
                1,
                "Pilot",
                58,
                "57:42",
                file_id=900,
                network="AMC",
                quality_source="television",
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

    async def test_detects_movie_file_as_imposter(self, notifier, notifications):
        """A 2h+ movie file labeled as a 51m episode (like Dark S01E01 at 127m)."""
        now = datetime.now(timezone.utc).isoformat()
        history = [{"episodeId": 700, "date": now}]
        episodes = {
            700: self._make_episode(
                700,
                "Dark",
                1,
                1,
                "Secrets",
                51,
                "2:06:57",
                file_id=1100,
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

        assert 1100 in deleted
        assert any(n["event"] == "imposter.detected" for n in notifications)


class TestImposterBackfill:
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
        series_list: list[dict],
        episodes_by_series: dict[int, list[dict]],
        deleted_files: list,
        commands: list,
    ) -> ArrClient:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            method = request.method
            if path.endswith("/api/v3/series") and method == "GET":
                return httpx.Response(200, json=series_list)
            if path.endswith("/api/v3/episode") and method == "GET":
                sid = int(request.url.params.get("seriesId", "0"))
                return httpx.Response(200, json=episodes_by_series.get(sid, []))
            if "/episodefile/" in path and method == "DELETE":
                deleted_files.append(int(path.split("/")[-1]))
                return httpx.Response(200)
            if "/command" in path and method == "POST":
                commands.append(json.loads(request.content))
                return httpx.Response(201, json={"id": 1})
            return httpx.Response(404)

        config = ArrAppConfig(url="http://sonarr:8989", api_key="key", name="Sonarr")
        client = ArrClient(config)
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return client

    async def test_backfill_flags_old_streaming_imposter(self, notifier, notifications):
        """Backfill retro-catches the Dark S1E2 case: pre-existing file never in
        recent history, but network/source mismatch is still decisive."""
        series_list = [
            {"id": 77, "title": "Dark", "network": "Netflix", "monitored": True}
        ]
        episodes_by_series = {
            77: [
                {
                    "id": 2381,
                    "seriesId": 77,
                    "seasonNumber": 1,
                    "episodeNumber": 2,
                    "title": "Lies",
                    "runtime": 44,
                    "hasFile": True,
                    "episodeFile": {
                        "id": 819,
                        "relativePath": "Season 01/Dark - S01E02 - Lies.mkv",
                        "mediaInfo": {"runTime": "42:17"},
                        "quality": {
                            "quality": {"name": "HDTV-1080p", "source": "television"}
                        },
                    },
                }
            ]
        }
        deleted: list = []
        commands: list = []
        sonarr = self._make_sonarr(series_list, episodes_by_series, deleted, commands)

        await run_imposter_backfill(
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            tolerance=0.40,
        )

        assert 819 in deleted
        assert any(c.get("name") == "EpisodeSearch" for c in commands)
        assert any(
            n["payload"].get("reason") == "streaming_with_broadcast_source"
            for n in notifications
        )

    async def test_backfill_skips_unmonitored_series(self, notifier, notifications):
        series_list = [
            {"id": 1, "title": "Old Show", "network": "Netflix", "monitored": False}
        ]
        episodes_by_series = {
            1: [
                {
                    "id": 10,
                    "seasonNumber": 1,
                    "episodeNumber": 1,
                    "title": "X",
                    "runtime": 50,
                    "hasFile": True,
                    "episodeFile": {
                        "id": 100,
                        "relativePath": "Season 01/Old Show - S01E01.mkv",
                        "mediaInfo": {"runTime": "20:00"},
                        "quality": {
                            "quality": {"name": "HDTV-1080p", "source": "television"}
                        },
                    },
                }
            ]
        }
        deleted: list = []
        commands: list = []
        sonarr = self._make_sonarr(series_list, episodes_by_series, deleted, commands)

        await run_imposter_backfill(
            arr_clients={"Sonarr": sonarr},
            notifier=notifier,
            tolerance=0.40,
        )

        assert deleted == []
        assert notifications == []

    async def test_backfill_no_sonarr(self, notifier, notifications):
        await run_imposter_backfill(arr_clients={}, notifier=notifier, tolerance=0.40)
        assert notifications == []
