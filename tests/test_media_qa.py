"""Tests for media_qa — post-import dud-file detection (born from the
2026-07-26 "For All Mankind S05E06 metadata-less PQ" incident).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from docktarr.arrclient import ArrClient
from docktarr.config import ArrAppConfig
from docktarr.http_health import HealthState
from docktarr.media_qa import (
    FLAG_BARE_PQ,
    FLAG_DV5,
    FLAG_NO_VIDEO,
    MediaQaConfig,
    MediaQaState,
    evaluate_media,
    needs_frame_probe,
    run_media_qa,
    run_media_qa_backfill,
)
from docktarr.notifier import Notifier


# ---------------------------------------------------------------------------
# ffprobe JSON fixtures
# ---------------------------------------------------------------------------


def _probe(
    *,
    color_transfer: str | None = "smpte2084",
    stream_side_data: list[dict] | None = None,
    duration: str | None = "3300.0",
    streams: list[dict] | None = None,
    tags: dict | None = None,
) -> dict:
    """Streams+format ffprobe output for a single HEVC video stream."""
    if streams is None:
        video: dict = {
            "index": 0,
            "codec_type": "video",
            "codec_name": "hevc",
            "profile": "Main 10",
            "disposition": {"attached_pic": 0},
        }
        if color_transfer:
            video["color_transfer"] = color_transfer
            video["color_primaries"] = "bt2020"
        if stream_side_data is not None:
            video["side_data_list"] = stream_side_data
        if tags:
            video["tags"] = tags
        streams = [
            video,
            {"index": 1, "codec_type": "audio", "codec_name": "eac3"},
        ]
    fmt: dict = {}
    if duration is not None:
        fmt["duration"] = duration
    return {"streams": streams, "format": fmt}


def _dovi(profile: int) -> dict:
    return {
        "side_data_type": "DOVI configuration record",
        "dv_profile": profile,
        "dv_bl_signal_compatibility_id": 1 if profile == 8 else 0,
    }


def _frames(*side_data_types: str) -> dict:
    return {
        "frames": [
            {"side_data_list": [{"side_data_type": t} for t in side_data_types]}
        ]
    }


_EMPTY_FRAMES = {"frames": [{}]}
_HDR10_FRAMES = _frames(
    "Mastering display metadata", "Content light level metadata"
)
_HDR10PLUS_FRAMES = _frames("HDR10+ dynamic metadata (SMPTE 2094-40)")


# ---------------------------------------------------------------------------
# Pure rules: evaluate_media / needs_frame_probe
# ---------------------------------------------------------------------------


class TestEvaluateMedia:
    def test_dv_profile5_flagged(self):
        flag, detail = evaluate_media(
            _probe(stream_side_data=[_dovi(5)]),
            None,
            expected_runtime_min=59,
        )
        assert flag == FLAG_DV5
        assert "profile 5" in detail

    def test_dv_profile8_not_flagged(self):
        # DV 8.1 has an HDR10 base layer — dovi present means never bare-PQ.
        flag, _ = evaluate_media(
            _probe(stream_side_data=[_dovi(8)]),
            None,
            expected_runtime_min=59,
        )
        assert flag is None

    def test_bare_pq_flagged(self):
        flag, detail = evaluate_media(
            _probe(), _EMPTY_FRAMES, expected_runtime_min=59
        )
        assert flag == FLAG_BARE_PQ
        assert "10,000-nit" in detail

    def test_pq_with_mastering_and_cll_ok(self):
        flag, _ = evaluate_media(
            _probe(), _HDR10_FRAMES, expected_runtime_min=59
        )
        assert flag is None

    def test_pq_with_hdr10plus_only_ok(self):
        flag, _ = evaluate_media(
            _probe(), _HDR10PLUS_FRAMES, expected_runtime_min=59
        )
        assert flag is None

    def test_pq_without_frames_probe_is_inconclusive(self):
        # frames=None means the frame probe wasn't run/failed — never guess.
        flag, _ = evaluate_media(_probe(), None, expected_runtime_min=59)
        assert flag is None

    def test_sdr_ok(self):
        flag, _ = evaluate_media(
            _probe(color_transfer="bt709"), None, expected_runtime_min=59
        )
        assert flag is None

    def test_no_video_stream_flagged(self):
        probe = {
            "streams": [{"index": 0, "codec_type": "audio", "codec_name": "aac"}],
            "format": {"duration": "3300.0"},
        }
        flag, detail = evaluate_media(probe, None, expected_runtime_min=59)
        assert flag == FLAG_NO_VIDEO
        assert "no video stream" in detail

    def test_attached_pic_only_flagged_as_no_video(self):
        probe = {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "mjpeg",
                    "disposition": {"attached_pic": 1},
                },
                {"index": 1, "codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "3300.0"},
        }
        flag, _ = evaluate_media(probe, None, expected_runtime_min=59)
        assert flag == FLAG_NO_VIDEO

    def test_truncated_video_flagged(self):
        # 5 minutes of video for a 60-minute episode: < 25% of runtime.
        flag, detail = evaluate_media(
            _probe(color_transfer="bt709", duration="300.0"),
            None,
            expected_runtime_min=60,
        )
        assert flag == FLAG_NO_VIDEO
        assert "duration" in detail

    def test_duration_within_tolerance_ok(self):
        flag, _ = evaluate_media(
            _probe(color_transfer="bt709", duration="2700.0"),
            None,
            expected_runtime_min=60,
        )
        assert flag is None

    def test_short_expected_runtime_skips_truncation(self):
        flag, _ = evaluate_media(
            _probe(color_transfer="bt709", duration="60.0"),
            None,
            expected_runtime_min=5,  # below MIN_RUNTIME_MINUTES
        )
        assert flag is None

    def test_duration_from_mkv_tags(self):
        probe = _probe(
            color_transfer="bt709",
            duration=None,
            tags={"DURATION-eng": "00:05:00.000000000"},
        )
        flag, _ = evaluate_media(probe, None, expected_runtime_min=60)
        assert flag == FLAG_NO_VIDEO


class TestNeedsFrameProbe:
    def test_pq_without_dovi_needs_frames(self):
        assert needs_frame_probe(_probe()) is True

    def test_pq_with_dovi_does_not(self):
        assert needs_frame_probe(_probe(stream_side_data=[_dovi(8)])) is False

    def test_sdr_does_not(self):
        assert needs_frame_probe(_probe(color_transfer="bt709")) is False

    def test_no_video_does_not(self):
        assert needs_frame_probe({"streams": [], "format": {}}) is False


# ---------------------------------------------------------------------------
# Integration doubles
# ---------------------------------------------------------------------------


class FakeDockerManager:
    """ffprobe-over-docker-exec double. ``files`` maps path ->
    (streams_probe, frames_probe); ``frames_probe`` may be None (frame probe
    fails). Paths not in ``files`` fail the streams probe."""

    def __init__(
        self,
        files: dict[str, tuple[dict, dict | None]],
        *,
        ffprobe_available: bool = True,
    ):
        self.files = files
        self.ffprobe_available = ffprobe_available
        self.exec_calls: list[list[str]] = []

    async def exec_run(self, name, cmd, *, user=None, timeout=30.0):
        tokens = list(cmd)
        self.exec_calls.append(tokens)
        if "-version" in tokens:
            return (0, "ffprobe version 7.0") if self.ffprobe_available else (127, "")
        path = tokens[-1]
        entry = self.files.get(path)
        if entry is None:
            return (1, "")
        streams_probe, frames_probe = entry
        if "-show_frames" in tokens:
            if frames_probe is None:
                return (1, "")
            return (0, json.dumps(frames_probe))
        return (0, json.dumps(streams_probe))


def _make_notifier() -> tuple[Notifier, list[dict]]:
    events: list[dict] = []

    class CapturingNotifier(Notifier):
        async def emit(self, event: str, payload: dict) -> None:
            events.append({"event": event, "payload": payload})

    n = CapturingNotifier(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))),
        webhook_url=None,
        enabled_events=[],
    )
    return n, events


def _recent_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _make_sonarr(
    *,
    history: list[dict],
    episodes: dict[int, dict],
    series: list[dict] | None = None,
    series_episodes: list[dict] | None = None,
    deleted_files: list[int] | None = None,
    commands: list[dict] | None = None,
) -> ArrClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/api/v3/history") and method == "GET":
            # Sonarr v4 rejects a string eventType query parameter with 400
            # (integer enum) — enforce that so a regression to server-side
            # string filtering fails loudly.
            if "eventType" in request.url.params:
                return httpx.Response(400, json={"message": "invalid eventType"})
            return httpx.Response(200, json={"records": history})
        if "/api/v3/episode/" in path and method == "GET":
            ep = episodes.get(int(path.split("/")[-1]))
            return httpx.Response(200, json=ep) if ep else httpx.Response(404)
        if path.endswith("/api/v3/series") and method == "GET":
            return httpx.Response(200, json=series or [])
        if path.endswith("/api/v3/episode") and method == "GET":
            return httpx.Response(200, json=series_episodes or [])
        if "/api/v3/episodefile/" in path and method == "DELETE":
            if deleted_files is not None:
                deleted_files.append(int(path.split("/")[-1]))
            return httpx.Response(200)
        if path.endswith("/api/v3/command") and method == "POST":
            if commands is not None:
                commands.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404)

    cfg = ArrAppConfig(url="http://sonarr:8989", api_key="key", name="Sonarr")
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _make_radarr(
    *,
    history: list[dict],
    movies: dict[int, dict],
    deleted_files: list[int] | None = None,
    commands: list[dict] | None = None,
) -> ArrClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/api/v3/history") and method == "GET":
            return httpx.Response(200, json={"records": history})
        if "/api/v3/movie/" in path and method == "GET":
            movie = movies.get(int(path.split("/")[-1]))
            return httpx.Response(200, json=movie) if movie else httpx.Response(404)
        if path.endswith("/api/v3/movie") and method == "GET":
            return httpx.Response(200, json=list(movies.values()))
        if "/api/v3/moviefile/" in path and method == "DELETE":
            if deleted_files is not None:
                deleted_files.append(int(path.split("/")[-1]))
            return httpx.Response(200)
        if path.endswith("/api/v3/command") and method == "POST":
            if commands is not None:
                commands.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(404)

    cfg = ArrAppConfig(url="http://radarr:7878", api_key="key", name="Radarr")
    client = ArrClient(cfg)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


_EP_PATH = "/data/tv/For All Mankind/Season 05/For All Mankind - S05E06.mkv"


def _episode(ep_id: int = 10, file_id: int = 77, path: str = _EP_PATH) -> dict:
    return {
        "id": ep_id,
        "seriesId": 1,
        "seasonNumber": 5,
        "episodeNumber": 6,
        "title": "Peace on the Ground",
        "runtime": 59,
        "hasFile": True,
        "series": {"title": "For All Mankind"},
        "episodeFile": {"id": file_id, "path": path, "size": 8_000_000_000},
    }


def _cfg(**kw) -> MediaQaConfig:
    base = dict(enabled=True)
    base.update(kw)
    return MediaQaConfig(**base)


# ---------------------------------------------------------------------------
# run_media_qa — recent scan
# ---------------------------------------------------------------------------


class TestRunMediaQa:
    async def test_disabled_returns_empty(self):
        notifier, events = _make_notifier()
        reports = await run_media_qa(
            arr_clients=[],
            config=MediaQaConfig(enabled=False),
            notifier=notifier,
            docker_manager=FakeDockerManager({}),
        )
        assert reports == [] and events == []

    async def test_no_docker_manager_skips(self):
        notifier, events = _make_notifier()
        reports = await run_media_qa(
            arr_clients=[],
            config=_cfg(),
            notifier=notifier,
            docker_manager=None,
        )
        assert reports == [] and events == []

    async def test_bare_pq_flagged_alert_only(self):
        deleted: list[int] = []
        commands: list[dict] = []
        sonarr = _make_sonarr(
            history=[{"episodeId": 10, "eventType": "downloadFolderImported", "date": _recent_date()}],
            episodes={10: _episode()},
            deleted_files=deleted,
            commands=commands,
        )
        dm = FakeDockerManager({_EP_PATH: (_probe(), _EMPTY_FRAMES)})
        notifier, events = _make_notifier()
        health = HealthState()

        reports = await run_media_qa(
            arr_clients=[sonarr],
            config=_cfg(),
            notifier=notifier,
            docker_manager=dm,
            health_state=health,
        )

        assert len(reports) == 1
        assert reports[0].flagged == 1
        flagged = [e for e in events if e["event"] == "media_qa.flagged"]
        assert len(flagged) == 1
        payload = flagged[0]["payload"]
        assert payload["reason"] == FLAG_BARE_PQ
        assert payload["file"] == _EP_PATH
        assert "For All Mankind S05E06" in payload["name"]
        # alert-only: nothing deleted, no re-search
        assert deleted == [] and commands == []
        assert reports[0].remediated == 0
        # surfaced at /health
        assert health.media_qa[0]["flagged"] == 1

    async def test_dv5_flagged(self):
        sonarr = _make_sonarr(
            history=[{"episodeId": 10, "eventType": "downloadFolderImported", "date": _recent_date()}],
            episodes={10: _episode()},
        )
        dm = FakeDockerManager(
            {_EP_PATH: (_probe(stream_side_data=[_dovi(5)]), None)}
        )
        notifier, events = _make_notifier()

        reports = await run_media_qa(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier, docker_manager=dm
        )

        assert reports[0].flagged == 1
        assert events[0]["event"] == "media_qa.flagged"
        assert events[0]["payload"]["reason"] == FLAG_DV5
        # DV verdict comes from stream side data — no frame probe needed
        assert not any("-show_frames" in call for call in dm.exec_calls)

    async def test_healthy_dv8_hdr10plus_not_flagged(self):
        sonarr = _make_sonarr(
            history=[{"episodeId": 10, "eventType": "downloadFolderImported", "date": _recent_date()}],
            episodes={10: _episode()},
        )
        dm = FakeDockerManager(
            {_EP_PATH: (_probe(stream_side_data=[_dovi(8)]), None)}
        )
        notifier, events = _make_notifier()

        reports = await run_media_qa(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier, docker_manager=dm
        )

        assert reports[0].flagged == 0 and events == []

    async def test_auto_remediate_deletes_and_researches(self):
        deleted: list[int] = []
        commands: list[dict] = []
        sonarr = _make_sonarr(
            history=[{"episodeId": 10, "eventType": "downloadFolderImported", "date": _recent_date()}],
            episodes={10: _episode()},
            deleted_files=deleted,
            commands=commands,
        )
        dm = FakeDockerManager({_EP_PATH: (_probe(), _EMPTY_FRAMES)})
        notifier, events = _make_notifier()

        reports = await run_media_qa(
            arr_clients=[sonarr],
            config=_cfg(auto_remediate=True),
            notifier=notifier,
            docker_manager=dm,
        )

        assert deleted == [77]
        assert commands == [{"name": "EpisodeSearch", "episodeIds": [10]}]
        assert reports[0].remediated == 1
        assert [e["event"] for e in events] == [
            "media_qa.flagged",
            "media_qa.remediated",
        ]

    async def test_flag_deduped_across_ticks(self):
        sonarr = _make_sonarr(
            history=[{"episodeId": 10, "eventType": "downloadFolderImported", "date": _recent_date()}],
            episodes={10: _episode()},
        )
        dm = FakeDockerManager({_EP_PATH: (_probe(), _EMPTY_FRAMES)})
        notifier, events = _make_notifier()
        state = MediaQaState()

        for _ in range(2):
            await run_media_qa(
                arr_clients=[sonarr],
                config=_cfg(),
                notifier=notifier,
                docker_manager=dm,
                state=state,
            )

        assert len([e for e in events if e["event"] == "media_qa.flagged"]) == 1

    async def test_old_history_outside_lookback_skipped(self):
        old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        sonarr = _make_sonarr(
            history=[{"episodeId": 10, "eventType": "downloadFolderImported", "date": old}],
            episodes={10: _episode()},
        )
        dm = FakeDockerManager({_EP_PATH: (_probe(), _EMPTY_FRAMES)})
        notifier, events = _make_notifier()

        reports = await run_media_qa(
            arr_clients=[sonarr],
            config=_cfg(),
            notifier=notifier,
            docker_manager=dm,
            lookback=timedelta(hours=24),
        )

        assert reports[0].scanned == 0 and events == []

    async def test_sonarr_non_import_history_events_ignored(self):
        # Client-side eventType filter: grabbed/deleted records never probe.
        sonarr = _make_sonarr(
            history=[
                {"episodeId": 10, "eventType": "grabbed", "date": _recent_date()},
                {
                    "episodeId": 10,
                    "eventType": "episodeFileDeleted",
                    "date": _recent_date(),
                },
            ],
            episodes={10: _episode()},
        )
        dm = FakeDockerManager({_EP_PATH: (_probe(), _EMPTY_FRAMES)})
        notifier, events = _make_notifier()

        reports = await run_media_qa(
            arr_clients=[sonarr],
            config=_cfg(),
            notifier=notifier,
            docker_manager=dm,
        )

        assert reports[0].scanned == 0 and events == []

    async def test_probe_failure_is_error_not_flag(self):
        deleted: list[int] = []
        sonarr = _make_sonarr(
            history=[{"episodeId": 10, "eventType": "downloadFolderImported", "date": _recent_date()}],
            episodes={10: _episode()},
            deleted_files=deleted,
        )
        dm = FakeDockerManager({})  # path unknown -> ffprobe exits 1
        notifier, events = _make_notifier()

        reports = await run_media_qa(
            arr_clients=[sonarr],
            config=_cfg(auto_remediate=True),
            notifier=notifier,
            docker_manager=dm,
        )

        assert reports[0].probe_errors == 1
        assert reports[0].flagged == 0
        # even with auto_remediate on, a probe failure never deletes
        assert deleted == []
        assert [e["event"] for e in events] == ["media_qa.error"]

    async def test_no_ffprobe_binary_degrades_with_error(self):
        sonarr = _make_sonarr(
            history=[{"episodeId": 10, "eventType": "downloadFolderImported", "date": _recent_date()}],
            episodes={10: _episode()},
        )
        dm = FakeDockerManager(
            {_EP_PATH: (_probe(), _EMPTY_FRAMES)}, ffprobe_available=False
        )
        notifier, events = _make_notifier()

        reports = await run_media_qa(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier, docker_manager=dm
        )

        assert reports[0].error is not None
        assert [e["event"] for e in events] == ["media_qa.error"]
        assert "ffprobe" in events[0]["payload"]["error"]

    async def test_radarr_recent_flow_and_remediation(self):
        deleted: list[int] = []
        commands: list[dict] = []
        movie_path = "/data/movies/Dune Part Two (2024)/Dune.Part.Two.2024.mkv"
        radarr = _make_radarr(
            history=[
                {
                    "movieId": 5,
                    "eventType": "downloadFolderImported",
                    "date": _recent_date(),
                },
                # non-import events are ignored
                {"movieId": 6, "eventType": "grabbed", "date": _recent_date()},
            ],
            movies={
                5: {
                    "id": 5,
                    "title": "Dune: Part Two",
                    "year": 2024,
                    "runtime": 166,
                    "monitored": True,
                    "hasFile": True,
                    "movieFile": {"id": 42, "path": movie_path, "size": 30_000},
                }
            },
            deleted_files=deleted,
            commands=commands,
        )
        dm = FakeDockerManager({movie_path: (_probe(), _EMPTY_FRAMES)})
        notifier, events = _make_notifier()

        reports = await run_media_qa(
            arr_clients=[radarr],
            config=_cfg(auto_remediate=True),
            notifier=notifier,
            docker_manager=dm,
        )

        assert reports[0].flagged == 1
        assert events[0]["payload"]["name"] == "Dune: Part Two (2024)"
        assert deleted == [42]
        assert commands == [{"name": "MoviesSearch", "movieIds": [5]}]


# ---------------------------------------------------------------------------
# run_media_qa_backfill — full-library scan
# ---------------------------------------------------------------------------


class TestBackfill:
    async def test_backfill_walks_monitored_series(self):
        good_path = "/data/tv/Show/S01E01.mkv"
        dud_path = "/data/tv/Show/S01E02.mkv"
        series = [{"id": 1, "title": "Show", "monitored": True}]
        series_episodes = [
            {
                "id": 100,
                "seasonNumber": 1,
                "episodeNumber": 1,
                "title": "One",
                "runtime": 59,
                "hasFile": True,
                "episodeFile": {"id": 1, "path": good_path, "size": 100},
            },
            {
                "id": 101,
                "seasonNumber": 1,
                "episodeNumber": 2,
                "title": "Two",
                "runtime": 59,
                "hasFile": True,
                "episodeFile": {"id": 2, "path": dud_path, "size": 200},
            },
            {"id": 102, "hasFile": False},
        ]
        sonarr = _make_sonarr(
            history=[], episodes={}, series=series, series_episodes=series_episodes
        )
        dm = FakeDockerManager(
            {
                good_path: (_probe(), _HDR10_FRAMES),
                dud_path: (_probe(), _EMPTY_FRAMES),
            }
        )
        notifier, events = _make_notifier()

        reports = await run_media_qa_backfill(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier, docker_manager=dm
        )

        assert reports[0].scanned == 2
        assert reports[0].flagged == 1
        flagged = [e for e in events if e["event"] == "media_qa.flagged"]
        assert len(flagged) == 1
        assert flagged[0]["payload"]["file"] == dud_path

    async def test_backfill_unmonitored_movie_skipped(self):
        radarr = _make_radarr(
            history=[],
            movies={
                5: {
                    "id": 5,
                    "title": "Skip Me",
                    "monitored": False,
                    "hasFile": True,
                    "movieFile": {"id": 1, "path": "/data/movies/x.mkv", "size": 1},
                }
            },
        )
        dm = FakeDockerManager({})
        notifier, events = _make_notifier()

        reports = await run_media_qa_backfill(
            arr_clients=[radarr], config=_cfg(), notifier=notifier, docker_manager=dm
        )

        assert reports[0].scanned == 0 and events == []
