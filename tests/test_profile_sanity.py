"""Tests for profile_sanity — impossible-quality-profile detection (born from
the 2026-07-27 "three Seerr requests Failed with zero errors" incident).

The three real specimens recorded on 2026-07-27 are encoded as fixtures and
driven end-to-end:

* Sonarr series 678 "Diagnosis: Murder" (1993, SD-only, profile 5 Ultra-HD,
  0 of 184 episodes) → MUST FLAG.
* Radarr movie 1127 "Charlie's Angels" (1976, 74-minute TV pilot, inCinemas
  null, profile 7 UHD 4k Remux) → MUST FLAG.
* Radarr movie 1128 "Spider-Man: Brand New Day" (2026, announced,
  isAvailable false, inCinemas 2026-07-28) → MUST NEVER FLAG OR HEAL.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from docktarr.arrclient import ArrClient
from docktarr.config import ArrAppConfig
from docktarr.http_health import HealthState
from docktarr.notifier import Notifier
from docktarr.profile_sanity import (
    ProfileSanityConfig,
    ProfileSanityState,
    allowed_qualities,
    confidence_score,
    era_ceiling,
    failing_indexer_names,
    hard_gates,
    indexer_outage,
    profile_floor,
    quality_resolution,
    run_profile_sanity,
    starvation,
)


# ---------------------------------------------------------------------------
# Quality-profile fixtures (shapes recorded from the live APIs 2026-07-27)
# ---------------------------------------------------------------------------


def _q(qid: int, name: str, resolution: int) -> dict:
    return {"id": qid, "name": name, "resolution": resolution, "source": "unknown"}


def _flat(qid: int, name: str, resolution: int, allowed: bool) -> dict:
    """A flat profile item: {"quality": {...}, "items": [], "allowed": bool}."""
    return {"quality": _q(qid, name, resolution), "items": [], "allowed": allowed}


def _group(gid: int, name: str, children: list[dict], allowed: bool) -> dict:
    """A GROUP profile item: {"name", "items": [flat...], "allowed", "id"}."""
    return {"name": name, "items": children, "allowed": allowed, "id": gid}


def _sonarr_profiles() -> dict[int, dict]:
    """Sonarr profiles 1 / 2 / 5 with their MEASURED floors (480 / 480 / 720)."""
    return {
        1: {
            "id": 1,
            "name": "Any",
            "items": [
                # resolution 0 AND an unrecognised name → excluded from the floor
                _flat(0, "Unknown", 0, True),
                _flat(1, "SDTV", 480, True),
                _flat(2, "DVD", 480, True),
                _group(
                    1000,
                    "WEB 480p",
                    [
                        _flat(12, "WEBRip-480p", 480, True),
                        _flat(8, "WEBDL-480p", 480, True),
                    ],
                    True,
                ),
                _flat(4, "HDTV-720p", 720, True),
                _flat(9, "HDTV-1080p", 1080, True),
                _flat(16, "HDTV-2160p", 2160, True),
            ],
        },
        2: {
            "id": 2,
            "name": "SD",
            "items": [
                _flat(1, "SDTV", 480, True),
                _flat(2, "DVD", 480, True),
                _group(
                    1000,
                    "WEB 480p",
                    [
                        _flat(12, "WEBRip-480p", 480, True),
                        _flat(8, "WEBDL-480p", 480, True),
                    ],
                    True,
                ),
                _flat(4, "HDTV-720p", 720, False),
                _flat(9, "HDTV-1080p", 1080, False),
            ],
        },
        3: {
            "id": 3,
            "name": "HD-720p",
            "items": [
                _flat(1, "SDTV", 480, False),
                _flat(4, "HDTV-720p", 720, True),
                _flat(9, "HDTV-1080p", 1080, False),
            ],
        },
        5: {
            "id": 5,
            "name": "Ultra-HD",
            # Recorded live: Ultra-HD's floor is 720, NOT 2160 — the module must
            # never assume the profile's headline cutoff is its floor. The 720p
            # group is allowed while its CHILDREN carry allowed=false, which is
            # exactly the group-allowed-children case.
            "items": [
                _flat(1, "SDTV", 480, False),
                _flat(2, "DVD", 480, False),
                _group(
                    1001,
                    "WEB 720p",
                    [
                        _flat(14, "WEBRip-720p", 720, False),
                        _flat(5, "WEBDL-720p", 720, False),
                    ],
                    True,
                ),
                _flat(9, "HDTV-1080p", 1080, True),
                _flat(16, "HDTV-2160p", 2160, True),
                _flat(19, "Bluray-2160p", 2160, True),
            ],
        },
    }


def _radarr_profiles() -> dict[int, dict]:
    """Radarr profiles 1 / 7 / 8 with their MEASURED floors (480 / 720 / 720).

    Radarr DVD (id 2) and WORKPRINT (id 24) really do report ``resolution: 0``,
    so profile 1's 480 floor depends on the name→resolution fallback.
    """
    return {
        1: {
            "id": 1,
            "name": "Any",
            "items": [
                _flat(0, "Unknown", 0, True),
                _flat(24, "WORKPRINT", 0, True),
                _flat(2, "DVD", 0, True),
                _flat(1, "SDTV", 480, True),
                _group(
                    1002,
                    "WEB 720p",
                    [_flat(14, "WEBDL-720p", 720, True)],
                    True,
                ),
                _flat(7, "Bluray-1080p", 1080, True),
                _flat(31, "Remux-2160p", 2160, True),
            ],
        },
        7: {
            "id": 7,
            "name": "UHD 4k Remux",
            "items": [
                _flat(2, "DVD", 0, False),
                _flat(1, "SDTV", 480, False),
                _group(
                    1002,
                    "WEB 720p",
                    [_flat(14, "WEBDL-720p", 720, True)],
                    True,
                ),
                _flat(7, "Bluray-1080p", 1080, True),
                _flat(31, "Remux-2160p", 2160, True),
            ],
        },
        8: {
            "id": 8,
            "name": "FHD Remux",
            "items": [
                _flat(1, "SDTV", 480, False),
                _flat(14, "WEBDL-720p", 720, True),
                _flat(30, "Remux-1080p", 1080, True),
            ],
        },
    }


# ---------------------------------------------------------------------------
# Item fixtures — the three real specimens
# ---------------------------------------------------------------------------


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _diagnosis_murder(**over) -> dict:
    """Sonarr series 678, prior state recorded 2026-07-27."""
    item = {
        "id": 678,
        "title": "Diagnosis: Murder",
        "year": 1993,
        "status": "ended",
        "ended": True,
        "monitored": True,
        "qualityProfileId": 5,  # Ultra-HD → floor 720
        "runtime": 45,
        "added": _ago(45),
        "firstAired": "1993-10-29T00:00:00Z",
        "statistics": {
            "episodeFileCount": 0,
            "episodeCount": 178,
            "totalEpisodeCount": 184,
            "sizeOnDisk": 0,
        },
    }
    item.update(over)
    return item


def _charlies_angels(**over) -> dict:
    """Radarr movie 1127, prior state recorded 2026-07-27 (a 74-minute 1976
    TV pilot: inCinemas is null, so it was never theatrical)."""
    item = {
        "id": 1127,
        "title": "Charlie's Angels",
        "year": 1976,
        "status": "released",
        "monitored": True,
        "qualityProfileId": 7,  # UHD 4k Remux → floor 720
        "runtime": 74,
        "added": _ago(30),
        "hasFile": False,
        "movieFileId": 0,
        "isAvailable": True,
        "minimumAvailability": "released",
        "inCinemas": None,
        "digitalRelease": "1976-03-21T00:00:00Z",
        "physicalRelease": None,
        "sizeOnDisk": 0,
    }
    item.update(over)
    return item


def _spider_man(**over) -> dict:
    """Radarr movie 1128 — the unreleased title that must NEVER be touched."""
    item = {
        "id": 1128,
        "title": "Spider-Man: Brand New Day",
        "year": 2026,
        "status": "announced",
        "monitored": True,
        "qualityProfileId": 7,
        "runtime": 145,
        "added": _ago(30),
        "hasFile": False,
        "movieFileId": 0,
        "isAvailable": False,
        "minimumAvailability": "released",
        "inCinemas": "2026-07-28T00:00:00Z",
        "digitalRelease": None,
        "physicalRelease": None,
        "sizeOnDisk": 0,
    }
    item.update(over)
    return item


_NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pure rules: profile floor
# ---------------------------------------------------------------------------


class TestProfileFloor:
    def test_sonarr_measured_floors(self):
        profiles = _sonarr_profiles()
        assert profile_floor(profiles[1]) == 480  # Any
        assert profile_floor(profiles[2]) == 480  # SD
        # Ultra-HD's floor is 720, NOT 2160 (measured).
        assert profile_floor(profiles[5]) == 720

    def test_radarr_measured_floors(self):
        profiles = _radarr_profiles()
        assert profile_floor(profiles[1]) == 480  # Any (via DVD resolution-0)
        assert profile_floor(profiles[7]) == 720  # UHD 4k Remux
        assert profile_floor(profiles[8]) == 720  # FHD Remux

    def test_group_allowed_children_are_collected(self):
        # Ultra-HD's "WEB 720p" group is allowed while its children say
        # allowed=false; the children must still count or the floor jumps to
        # 1080 and the module manufactures false contradictions.
        group_only = {
            "id": 99,
            "name": "group-allowed",
            "items": [
                _group(
                    1001,
                    "WEB 720p",
                    [_flat(5, "WEBDL-720p", 720, False)],
                    True,
                ),
                _flat(9, "HDTV-1080p", 1080, True),
            ],
        }
        assert profile_floor(group_only) == 720

    def test_disallowed_group_children_excluded(self):
        profile = {
            "id": 98,
            "name": "sd-group-off",
            "items": [
                _group(
                    1000,
                    "WEB 480p",
                    [_flat(8, "WEBDL-480p", 480, False)],
                    False,
                ),
                _flat(9, "HDTV-1080p", 1080, True),
            ],
        }
        assert profile_floor(profile) == 1080

    def test_child_allowed_inside_disallowed_group_counts(self):
        profile = {
            "id": 97,
            "name": "child-on",
            "items": [
                _group(
                    1000,
                    "WEB 480p",
                    [_flat(8, "WEBDL-480p", 480, True)],
                    False,
                ),
                _flat(9, "HDTV-1080p", 1080, True),
            ],
        }
        assert profile_floor(profile) == 480

    def test_resolution_zero_falls_back_to_name_table(self):
        # Radarr DVD (id 2) and WORKPRINT (id 24) report resolution 0.
        assert quality_resolution(_q(2, "DVD", 0)) == 480
        assert quality_resolution(_q(24, "WORKPRINT", 0)) == 480
        assert quality_resolution(_q(1, "SDTV", 480)) == 480
        profile = {
            "id": 96,
            "name": "dvd-plus-1080",
            "items": [
                _flat(2, "DVD", 0, True),
                _flat(7, "Bluray-1080p", 1080, True),
            ],
        }
        assert profile_floor(profile) == 480

    def test_unknown_resolution_and_name_excluded(self):
        assert quality_resolution(_q(0, "Unknown", 0)) is None
        profile = {
            "id": 95,
            "name": "unknown-plus-720",
            "items": [
                _flat(0, "Unknown", 0, True),
                _flat(4, "HDTV-720p", 720, True),
            ],
        }
        assert profile_floor(profile) == 720

    def test_nothing_allowed_returns_none(self):
        profile = {"id": 94, "name": "empty", "items": [_flat(1, "SDTV", 480, False)]}
        assert profile_floor(profile) is None
        assert profile_floor({"id": 93, "name": "no-items"}) is None

    def test_allowed_qualities_flattens_nested_groups(self):
        names = [
            q["name"] for q in allowed_qualities(_sonarr_profiles()[5])
        ]
        assert "WEBDL-720p" in names and "WEBRip-720p" in names
        assert "SDTV" not in names and "DVD" not in names


# ---------------------------------------------------------------------------
# Pure rules: era ceiling (the core false-positive defence)
# ---------------------------------------------------------------------------


class TestEraCeiling:
    def test_pre_hd_series_gets_sd_ceiling(self):
        # Diagnosis: Murder (1993) — pre-HD TV masters are videotape/SD.
        assert era_ceiling("series", year=1993, runtime=45) == 576

    def test_modern_series_has_no_ceiling(self):
        assert era_ceiling("series", year=2019, runtime=59) is None
        assert era_ceiling("series", year=1998, runtime=45) is None  # boundary

    def test_pre_hd_tv_movie_without_cinema_release(self):
        # Charlie's Angels (1976): inCinemas null, runtime 74 → TV-sourced.
        assert (
            era_ceiling("movie", year=1976, runtime=74, in_cinemas=None) == 576
        )

    def test_pre_hd_theatrical_feature_gets_bluray_ceiling(self):
        # A theatrical film from the same year is remastered from the negative,
        # so a 720p floor is satisfiable and this must NOT be a contradiction.
        ceiling = era_ceiling(
            "movie", year=1976, runtime=120, in_cinemas="1976-06-01T00:00:00Z"
        )
        assert ceiling == 1080
        assert profile_floor(_radarr_profiles()[7]) <= ceiling

    def test_pre_hd_theatrical_but_pilot_runtime_is_tv_shaped(self):
        assert (
            era_ceiling(
                "movie", year=1976, runtime=74, in_cinemas="1976-06-01T00:00:00Z"
            )
            == 576
        )

    def test_modern_title_has_no_ceiling(self):
        assert era_ceiling("movie", year=2020, runtime=120, in_cinemas="x") is None
        assert era_ceiling("movie", year=2026, runtime=145) is None

    def test_missing_or_bad_year_has_no_ceiling(self):
        assert era_ceiling("series", year=None) is None
        assert era_ceiling("movie", year="not-a-year") is None

    def test_unknown_kind_has_no_ceiling(self):
        assert era_ceiling("book", year=1976) is None

    def test_sd_era_year_is_configurable(self):
        assert era_ceiling("series", year=2005, sd_era_year=2010) == 576


# ---------------------------------------------------------------------------
# Pure rules: hard gates
# ---------------------------------------------------------------------------


class TestHardGates:
    def test_specimens_pass(self):
        ok, reason = hard_gates("series", _diagnosis_murder(), now=_NOW)
        assert ok and reason == ""
        ok, reason = hard_gates("movie", _charlies_angels(), now=_NOW)
        assert ok and reason == ""

    def test_unmonitored_excluded(self):
        ok, reason = hard_gates("series", _diagnosis_murder(monitored=False), now=_NOW)
        assert not ok and "monitored" in reason

    def test_series_with_files_excluded(self):
        item = _diagnosis_murder(
            statistics={"episodeFileCount": 3, "episodeCount": 178}
        )
        ok, reason = hard_gates("series", item, now=_NOW)
        assert not ok and "satisfiable" in reason

    def test_series_with_no_aired_episodes_excluded(self):
        item = _diagnosis_murder(statistics={"episodeFileCount": 0, "episodeCount": 0})
        ok, reason = hard_gates("series", item, now=_NOW)
        assert not ok and "aired" in reason

    def test_movie_with_file_excluded(self):
        ok, reason = hard_gates("movie", _charlies_angels(hasFile=True), now=_NOW)
        assert not ok and "satisfiable" in reason

    def test_movie_with_movie_file_id_excluded(self):
        ok, reason = hard_gates("movie", _charlies_angels(movieFileId=42), now=_NOW)
        assert not ok and "satisfiable" in reason

    # --- gate 3, branch by branch --------------------------------------
    def test_series_missing_first_aired_excluded(self):
        ok, reason = hard_gates("series", _diagnosis_murder(firstAired=None), now=_NOW)
        assert not ok and "firstAired" in reason

    def test_series_future_first_aired_excluded(self):
        item = _diagnosis_murder(firstAired=_ago(-30))
        ok, reason = hard_gates("series", item, now=_NOW)
        assert not ok and "has not aired" in reason

    def test_series_upcoming_status_excluded(self):
        item = _diagnosis_murder(status="upcoming")
        ok, reason = hard_gates("series", item, now=_NOW)
        assert not ok and "upcoming" in reason

    def test_announced_movie_excluded(self):
        # Spider-Man: Brand New Day — the canonical must-never-flag case.
        ok, reason = hard_gates("movie", _spider_man(), now=_NOW)
        assert not ok and "announced" in reason

    def test_in_cinemas_status_excluded(self):
        item = _charlies_angels(status="inCinemas")
        ok, reason = hard_gates("movie", item, now=_NOW)
        assert not ok and "incinemas" in reason

    def test_tba_status_excluded(self):
        ok, _ = hard_gates("movie", _charlies_angels(status="tba"), now=_NOW)
        assert not ok

    def test_deleted_status_excluded(self):
        ok, _ = hard_gates("movie", _charlies_angels(status="deleted"), now=_NOW)
        assert not ok

    def test_not_available_movie_excluded(self):
        item = _charlies_angels(isAvailable=False)
        ok, reason = hard_gates("movie", item, now=_NOW)
        assert not ok and "isAvailable" in reason

    def test_future_cinema_release_with_no_other_dates_excluded(self):
        item = _charlies_angels(
            digitalRelease=None, physicalRelease=None, inCinemas=_ago(-14)
        )
        ok, reason = hard_gates("movie", item, now=_NOW)
        assert not ok and "future" in reason

    def test_no_release_dates_at_all_excluded(self):
        item = _charlies_angels(
            digitalRelease=None, physicalRelease=None, inCinemas=None
        )
        ok, reason = hard_gates("movie", item, now=_NOW)
        assert not ok and "release date" in reason

    def test_past_cinema_release_only_passes(self):
        item = _charlies_angels(
            digitalRelease=None, physicalRelease=None, inCinemas="1976-03-21T00:00:00Z"
        )
        ok, reason = hard_gates("movie", item, now=_NOW)
        assert ok and reason == ""

    def test_physical_release_only_passes(self):
        item = _charlies_angels(
            digitalRelease=None, physicalRelease="1990-01-01T00:00:00Z"
        )
        ok, _ = hard_gates("movie", item, now=_NOW)
        assert ok

    # --- gate 6 --------------------------------------------------------
    def test_queued_item_excluded(self):
        ok, reason = hard_gates(
            "series", _diagnosis_murder(), now=_NOW, in_queue=True
        )
        assert not ok and "queue" in reason


# ---------------------------------------------------------------------------
# Pure rules: starvation + confidence
# ---------------------------------------------------------------------------


_STARVE_KW = dict(
    min_starvation_age=timedelta(days=3),
    severe_starvation_age=timedelta(days=14),
)


class TestStarvation:
    def test_old_item_with_empty_history_is_severely_starved(self):
        starved, severe, detail = starvation(
            _diagnosis_murder(), [], now=_NOW, **_STARVE_KW
        )
        assert starved and severe and "no grab" in detail

    def test_recently_added_item_is_not_starved(self):
        starved, severe, detail = starvation(
            _diagnosis_murder(added=_ago(1)), [], now=_NOW, **_STARVE_KW
        )
        assert not starved and not severe and "younger" in detail

    def test_moderately_old_item_is_starved_but_not_severe(self):
        starved, severe, _ = starvation(
            _diagnosis_murder(added=_ago(5)), [], now=_NOW, **_STARVE_KW
        )
        assert starved and not severe

    def test_grabbed_event_clears_starvation(self):
        history = [
            {"eventType": "downloadFailed", "date": _ago(2)},
            {"eventType": "grabbed", "date": _ago(2)},
        ]
        starved, _, detail = starvation(
            _diagnosis_murder(), history, now=_NOW, **_STARVE_KW
        )
        assert not starved and "satisfiable" in detail

    def test_imported_event_clears_starvation(self):
        history = [{"eventType": "downloadFolderImported", "date": _ago(2)}]
        starved, _, _ = starvation(
            _diagnosis_murder(), history, now=_NOW, **_STARVE_KW
        )
        assert not starved

    def test_unrelated_history_events_do_not_clear_starvation(self):
        history = [
            {"eventType": "downloadFailed", "date": _ago(2)},
            {"eventType": "episodeFileDeleted", "date": _ago(2)},
        ]
        starved, _, _ = starvation(
            _diagnosis_murder(), history, now=_NOW, **_STARVE_KW
        )
        assert starved

    def test_missing_added_timestamp_is_not_starved(self):
        starved, _, detail = starvation(
            _diagnosis_murder(added=None), [], now=_NOW, **_STARVE_KW
        )
        assert not starved and "added" in detail


class TestConfidenceScore:
    def test_diagnosis_murder_scores_high(self):
        score, signals = confidence_score(
            "series",
            _diagnosis_murder(),
            floor=720,
            ceiling=576,
            starved=True,
            severe=True,
        )
        # 2 contradiction + 2 starved + 1 severe + 2 pre-HD + 1 pilot runtime
        assert score == 8
        assert any("floor 720p" in s for s in signals)

    def test_charlies_angels_scores_high(self):
        score, _ = confidence_score(
            "movie",
            _charlies_angels(),
            floor=720,
            ceiling=576,
            starved=True,
            severe=True,
        )
        assert score == 8

    def test_modern_title_only_scores_contradiction_and_starvation(self):
        score, _ = confidence_score(
            "movie",
            _charlies_angels(year=2015, runtime=120),
            floor=2160,
            ceiling=1080,
            starved=True,
            severe=False,
        )
        assert score == 4

    def test_feature_runtime_does_not_earn_the_pilot_point(self):
        score, _ = confidence_score(
            "movie",
            _charlies_angels(runtime=120),
            floor=720,
            ceiling=1080,
            starved=True,
            severe=True,
        )
        assert score == 7


# ---------------------------------------------------------------------------
# Fake arr services
# ---------------------------------------------------------------------------


_HEALTHY_INDEXERS = [
    {
        "id": 1,
        "name": "Nebulance (Prowlarr)",
        "enableAutomaticSearch": True,
        "enableRss": True,
        "protocol": "torrent",
    },
    {
        "id": 2,
        "name": "RuTor (Prowlarr)",
        "enableAutomaticSearch": False,
        "enableRss": True,
        "protocol": "torrent",
    },
]

# Real observed messages from /api/v3/health.
_STATUS_CHECK = {
    "source": "IndexerStatusCheck",
    "type": "warning",
    "message": (
        "Indexers unavailable due to failures: Demonoid Clone (Prowlarr), "
        "Elitetorrent-wf (Prowlarr), RuTor (Prowlarr)"
    ),
    "wikiUrl": "https://wiki.servarr.com/",
}
_LONG_TERM_CHECK = {
    "source": "IndexerLongTermStatusCheck",
    "type": "warning",
    "message": (
        "Indexers unavailable due to failures for more than 6 hours: "
        "Torrent Downloads (Prowlarr)"
    ),
    "wikiUrl": "https://wiki.servarr.com/",
}
_ALL_DOWN_CHECK = {
    "source": "IndexerStatusCheck",
    "type": "error",
    "message": "All indexers are unavailable due to failures",
    "wikiUrl": "https://wiki.servarr.com/",
}
_NO_AUTO_SEARCH_CHECK = {
    "source": "IndexerCheck",
    "type": "warning",
    "message": (
        "No indexers available with Automatic Search enabled, Sonarr will not "
        "provide any automatic search results"
    ),
    "wikiUrl": "https://wiki.servarr.com/",
}


def _make_arr(
    name: str,
    *,
    items: dict[int, dict],
    profiles: dict[int, dict],
    history: dict[int, list[dict]] | None = None,
    queue: list[dict] | None = None,
    indexers: list[dict] | None = None,
    health: list[dict] | None = None,
    health_status: int = 200,
    indexer_status: int = 200,
    puts: list[dict] | None = None,
    commands: list[dict] | None = None,
    fresh_items: dict[int, dict] | None = None,
) -> ArrClient:
    """MockTransport-backed Sonarr/Radarr double.

    ``fresh_items`` overrides the by-id GET so a test can simulate the item
    changing between the library snapshot and the pre-write re-fetch.
    """
    kind = "series" if name == "Sonarr" else "movie"
    endpoint = "series" if kind == "series" else "movie"
    history = history or {}
    queue = queue if queue is not None else []
    indexers = indexers if indexers is not None else _HEALTHY_INDEXERS
    health = health if health is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/api/v3/qualityprofile" and method == "GET":
            return httpx.Response(200, json=list(profiles.values()))
        if path == "/api/v3/indexer" and method == "GET":
            if indexer_status != 200:
                return httpx.Response(indexer_status, json={"message": "boom"})
            return httpx.Response(200, json=indexers)
        if path == "/api/v3/health" and method == "GET":
            if health_status != 200:
                return httpx.Response(health_status, json={"message": "boom"})
            return httpx.Response(200, json=health)
        if path == "/api/v3/queue" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "page": 1,
                    "pageSize": 1000,
                    "totalRecords": len(queue),
                    "records": queue,
                },
            )
        if path == f"/api/v3/history/{endpoint}" and method == "GET":
            # PR #8 precedent: a string eventType query param 400s on Sonarr v4.
            if "eventType" in request.url.params:
                return httpx.Response(400, json={"message": "invalid eventType"})
            key = "seriesId" if kind == "series" else "movieId"
            raw = request.url.params.get(key)
            item_id = int(raw) if raw else 0
            return httpx.Response(200, json=history.get(item_id, []))
        if path == f"/api/v3/{endpoint}" and method == "GET":
            return httpx.Response(200, json=list(items.values()))
        if path == "/api/v3/command" and method == "POST":
            if commands is not None:
                commands.append(json.loads(request.content))
            return httpx.Response(201, json={"id": 1})
        if path.startswith(f"/api/v3/{endpoint}/"):
            item_id = int(path.rsplit("/", 1)[1])
            if method == "GET":
                source = fresh_items or items
                item = source.get(item_id)
                return (
                    httpx.Response(200, json=item)
                    if item is not None
                    else httpx.Response(404)
                )
            if method == "PUT":
                body = json.loads(request.content)
                if puts is not None:
                    puts.append(body)
                return httpx.Response(202, json=body)
        return httpx.Response(404)

    url = "http://sonarr:8989" if kind == "series" else "http://radarr:7878"
    client = ArrClient(ArrAppConfig(url=url, api_key="key", name=name))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


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


def _cfg(**kw) -> ProfileSanityConfig:
    base = dict(enabled=True)
    base.update(kw)
    return ProfileSanityConfig(**base)


def _events_of(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e["event"] == name]


# ---------------------------------------------------------------------------
# Stack guard: indexer outage
# ---------------------------------------------------------------------------


class TestIndexerOutage:
    async def test_healthy_stack_reports_no_outage(self):
        client = _make_arr("Sonarr", items={}, profiles=_sonarr_profiles())
        outage, reason = await indexer_outage(client)
        assert not outage and reason == ""

    async def test_partial_failures_with_a_live_auto_indexer_is_no_outage(self):
        # Nebulance (the only auto-search indexer) is NOT in the failing list.
        client = _make_arr(
            "Sonarr",
            items={},
            profiles=_sonarr_profiles(),
            health=[_STATUS_CHECK, _LONG_TERM_CHECK],
        )
        outage, _ = await indexer_outage(client)
        assert not outage

    async def test_no_auto_search_indexer_is_an_outage(self):
        client = _make_arr(
            "Sonarr",
            items={},
            profiles=_sonarr_profiles(),
            indexers=[
                {"id": 1, "name": "Nebulance", "enableAutomaticSearch": False},
            ],
        )
        outage, reason = await indexer_outage(client)
        assert outage and "Automatic Search" in reason

    async def test_no_indexers_available_message_is_an_outage(self):
        client = _make_arr(
            "Sonarr",
            items={},
            profiles=_sonarr_profiles(),
            health=[_NO_AUTO_SEARCH_CHECK],
        )
        outage, reason = await indexer_outage(client)
        assert outage and "No indexers available" in reason

    async def test_all_indexers_unavailable_message_is_an_outage(self):
        client = _make_arr(
            "Sonarr", items={}, profiles=_sonarr_profiles(), health=[_ALL_DOWN_CHECK]
        )
        outage, reason = await indexer_outage(client)
        assert outage and "All indexers are unavailable" in reason

    async def test_every_auto_search_indexer_failing_is_an_outage(self):
        client = _make_arr(
            "Sonarr",
            items={},
            profiles=_sonarr_profiles(),
            indexers=[
                {"id": 1, "name": "RuTor (Prowlarr)", "enableAutomaticSearch": True},
                {
                    "id": 2,
                    "name": "Demonoid Clone (Prowlarr)",
                    "enableAutomaticSearch": True,
                },
            ],
            health=[_STATUS_CHECK],
        )
        outage, reason = await indexer_outage(client)
        assert outage and "every Automatic-Search indexer" in reason

    async def test_health_api_failure_fails_closed(self):
        client = _make_arr(
            "Sonarr", items={}, profiles=_sonarr_profiles(), health_status=500
        )
        outage, reason = await indexer_outage(client)
        assert outage and "failing closed" in reason

    async def test_indexer_api_failure_fails_closed(self):
        client = _make_arr(
            "Sonarr", items={}, profiles=_sonarr_profiles(), indexer_status=500
        )
        outage, reason = await indexer_outage(client)
        assert outage and "failing closed" in reason

    def test_failing_indexer_names_parsed_from_real_messages(self):
        names = failing_indexer_names([_STATUS_CHECK, _LONG_TERM_CHECK])
        assert names == {
            "demonoid clone",
            "elitetorrent-wf",
            "rutor",
            "torrent downloads",
        }

    def test_non_indexer_health_checks_ignored(self):
        assert (
            failing_indexer_names(
                [{"source": "DownloadClientCheck", "message": "Broken: qBittorrent"}]
            )
            == set()
        )


# ---------------------------------------------------------------------------
# End-to-end: the three real specimens
# ---------------------------------------------------------------------------


class TestRealSpecimens:
    async def test_disabled_returns_empty_and_emits_nothing(self):
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[_make_arr("Sonarr", items={}, profiles={})],
            config=ProfileSanityConfig(enabled=False),
            notifier=notifier,
        )
        assert reports == [] and events == []

    async def test_diagnosis_murder_flagged(self):
        puts: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            profiles=_sonarr_profiles(),
            puts=puts,
        )
        notifier, events = _make_notifier()
        health = HealthState()

        reports = await run_profile_sanity(
            arr_clients=[sonarr],
            config=_cfg(),
            notifier=notifier,
            health_state=health,
        )

        assert len(reports) == 1
        report = reports[0]
        assert report.eligible == 1 and report.starved == 1
        assert report.flagged == 1 and report.alerted == 1 and report.healed == 0
        assert report.suppressed is None and report.error is None
        flag = report.flags[0]
        assert flag["id"] == 678
        assert flag["profile_name"] == "Ultra-HD"
        assert flag["floor"] == 720 and flag["ceiling"] == 576
        assert flag["recommended"] == "Any"
        assert flag["confidence"] == 8
        flagged = _events_of(events, "profile_sanity.flagged")
        assert len(flagged) == 1
        payload = flagged[0]["payload"]
        assert payload["name"] == "Diagnosis: Murder"
        assert payload["profile_name"] == "Ultra-HD"
        assert "576p" in payload["why"] and "720p" in payload["why"]
        assert payload["recommended"] == "Any"
        # alert-only by default
        assert puts == []
        assert health.profile_sanity[0]["flagged"] == 1

    async def test_flagged_event_renders_through_the_notifier_template(self):
        # A missing template key would silently degrade the alert to a repr.
        from docktarr.notifier import _TEMPLATES

        sonarr = _make_arr(
            "Sonarr", items={678: _diagnosis_murder()}, profiles=_sonarr_profiles()
        )
        notifier, events = _make_notifier()
        await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        payload = _events_of(events, "profile_sanity.flagged")[0]["payload"]
        message = _TEMPLATES["profile_sanity.flagged"].format(**payload)
        assert "Diagnosis: Murder" in message
        assert "Ultra-HD" in message
        assert "Recommended profile" in message

    async def test_charlies_angels_flagged_and_spider_man_untouched(self):
        puts: list[dict] = []
        commands: list[dict] = []
        radarr = _make_arr(
            "Radarr",
            items={1127: _charlies_angels(), 1128: _spider_man()},
            profiles=_radarr_profiles(),
            puts=puts,
            commands=commands,
        )
        notifier, events = _make_notifier()

        reports = await run_profile_sanity(
            arr_clients=[radarr],
            # auto-heal ON to prove the unreleased title is not healed either
            config=_cfg(auto_heal=True, search_after_heal=True),
            notifier=notifier,
        )

        report = reports[0]
        assert report.scanned == 2
        assert report.eligible == 1  # Spider-Man excluded by gate 3
        assert report.flagged == 1 and report.healed == 1
        names = [f["name"] for f in report.flags]
        assert names == ["Charlie's Angels"]
        assert all(
            "Spider-Man" not in json.dumps(e["payload"]) for e in events
        )
        # Exactly one PUT, and it is Charlie's Angels.
        assert len(puts) == 1
        assert puts[0]["id"] == 1127
        assert puts[0]["qualityProfileId"] == 1
        assert [c.get("movieIds") for c in commands] == [[1127]]

    async def test_spider_man_alone_produces_no_events_and_no_put(self):
        puts: list[dict] = []
        radarr = _make_arr(
            "Radarr",
            items={1128: _spider_man()},
            profiles=_radarr_profiles(),
            puts=puts,
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[radarr], config=_cfg(auto_heal=True), notifier=notifier
        )
        assert reports[0].eligible == 0 and reports[0].flagged == 0
        assert events == [] and puts == []


# ---------------------------------------------------------------------------
# End-to-end: gate behaviour
# ---------------------------------------------------------------------------


class TestGatesEndToEnd:
    async def test_grabbed_history_prevents_flagging(self):
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            profiles=_sonarr_profiles(),
            history={678: [{"eventType": "grabbed", "date": _ago(4)}]},
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].eligible == 1 and reports[0].starved == 0
        assert reports[0].flagged == 0 and events == []

    async def test_item_with_files_not_flagged(self):
        item = _diagnosis_murder(
            statistics={"episodeFileCount": 184, "episodeCount": 178}
        )
        sonarr = _make_arr(
            "Sonarr", items={678: item}, profiles=_sonarr_profiles()
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].eligible == 0 and reports[0].flagged == 0 and events == []

    async def test_item_younger_than_min_starvation_age_not_flagged(self):
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder(added=_ago(1))},
            profiles=_sonarr_profiles(),
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].starved == 0 and reports[0].flagged == 0 and events == []

    async def test_queued_item_not_flagged(self):
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            profiles=_sonarr_profiles(),
            queue=[{"id": 9, "seriesId": 678, "episodeId": 5, "title": "..."}],
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].eligible == 0 and reports[0].flagged == 0 and events == []

    async def test_satisfiable_profile_not_flagged(self):
        # Same starved 1993 series, but on SD (floor 480 <= ceiling 576): a
        # starved title on a satisfiable profile is out of scope.
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder(qualityProfileId=2)},
            profiles=_sonarr_profiles(),
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].starved == 1 and reports[0].flagged == 0 and events == []

    async def test_modern_title_never_flagged(self):
        item = _diagnosis_murder(
            id=999, title="Modern Show", year=2024, firstAired=_ago(400)
        )
        sonarr = _make_arr(
            "Sonarr", items={999: item}, profiles=_sonarr_profiles()
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].flagged == 0 and events == []

    async def test_alert_is_deduped_across_ticks(self):
        sonarr = _make_arr(
            "Sonarr", items={678: _diagnosis_murder()}, profiles=_sonarr_profiles()
        )
        notifier, events = _make_notifier()
        state = ProfileSanityState()
        for _ in range(3):
            await run_profile_sanity(
                arr_clients=[sonarr], config=_cfg(), notifier=notifier, state=state
            )
        assert len(_events_of(events, "profile_sanity.flagged")) == 1

    async def test_non_sonarr_radarr_clients_skipped(self):
        lidarr = _make_arr("Sonarr", items={}, profiles={})
        lidarr.name = "Lidarr"
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[lidarr], config=_cfg(), notifier=notifier
        )
        assert reports == [] and events == []


# ---------------------------------------------------------------------------
# Stack guards, end to end (the mass-flag regression tests)
# ---------------------------------------------------------------------------


def _flaggable_library(count: int) -> dict[int, dict]:
    """``count`` otherwise-flaggable SD-era starved series on Ultra-HD."""
    return {
        i: _diagnosis_murder(id=i, title=f"Old Show {i}")
        for i in range(1, count + 1)
    }


class TestStackGuards:
    async def test_all_auto_indexers_failing_flags_nothing(self):
        items = _flaggable_library(30)
        puts: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items=items,
            profiles=_sonarr_profiles(),
            indexers=[
                {"id": 1, "name": "RuTor (Prowlarr)", "enableAutomaticSearch": True},
                {
                    "id": 2,
                    "name": "Demonoid Clone (Prowlarr)",
                    "enableAutomaticSearch": True,
                },
            ],
            health=[_STATUS_CHECK],
            puts=puts,
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(auto_heal=True), notifier=notifier
        )
        assert reports[0].flagged == 0 and reports[0].healed == 0
        assert reports[0].suppressed
        assert puts == []
        assert _events_of(events, "profile_sanity.flagged") == []
        assert len(_events_of(events, "profile_sanity.indexer_outage")) == 1

    async def test_no_indexers_available_flags_nothing(self):
        sonarr = _make_arr(
            "Sonarr",
            items=_flaggable_library(30),
            profiles=_sonarr_profiles(),
            health=[_NO_AUTO_SEARCH_CHECK],
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].flagged == 0
        assert _events_of(events, "profile_sanity.flagged") == []
        assert len(_events_of(events, "profile_sanity.indexer_outage")) == 1

    async def test_health_api_failure_flags_nothing(self):
        sonarr = _make_arr(
            "Sonarr",
            items=_flaggable_library(30),
            profiles=_sonarr_profiles(),
            health_status=500,
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].flagged == 0
        assert _events_of(events, "profile_sanity.flagged") == []
        assert len(_events_of(events, "profile_sanity.indexer_outage")) == 1

    async def test_indexer_outage_alert_is_deduped(self):
        sonarr = _make_arr(
            "Sonarr",
            items=_flaggable_library(2),
            profiles=_sonarr_profiles(),
            health=[_ALL_DOWN_CHECK],
        )
        notifier, events = _make_notifier()
        state = ProfileSanityState()
        for _ in range(3):
            await run_profile_sanity(
                arr_clients=[sonarr], config=_cfg(), notifier=notifier, state=state
            )
        assert len(_events_of(events, "profile_sanity.indexer_outage")) == 1

    async def test_library_wide_starvation_ratio_suppresses_flags(self):
        # 25 starved-but-satisfiable items (profile 3, modern) plus the real
        # specimen: the ratio guard must withhold everything.
        items: dict[int, dict] = {
            i: _diagnosis_murder(
                id=i,
                title=f"New Show {i}",
                year=2024,
                firstAired=_ago(400),
                qualityProfileId=3,
            )
            for i in range(100, 125)
        }
        items[678] = _diagnosis_murder()
        puts: list[dict] = []
        sonarr = _make_arr(
            "Sonarr", items=items, profiles=_sonarr_profiles(), puts=puts
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(auto_heal=True), notifier=notifier
        )
        report = reports[0]
        assert report.eligible == 26 and report.starved == 26
        assert report.flagged == 0 and report.healed == 0 and puts == []
        assert report.suppressed and "starved" in report.suppressed
        assert _events_of(events, "profile_sanity.flagged") == []
        suspected = _events_of(events, "profile_sanity.outage_suspected")
        assert len(suspected) == 1
        assert suspected[0]["payload"]["eligible"] == 26

    async def test_small_library_is_not_ratio_suppressed(self):
        # Below min_library_size the ratio guard must not fire, or a small
        # library could never be diagnosed at all.
        sonarr = _make_arr(
            "Sonarr", items=_flaggable_library(3), profiles=_sonarr_profiles()
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].suppressed is None
        assert reports[0].flagged == 3
        assert _events_of(events, "profile_sanity.outage_suspected") == []

    async def test_queue_failure_fails_closed(self):
        sonarr = _make_arr(
            "Sonarr", items=_flaggable_library(3), profiles=_sonarr_profiles()
        )
        # Break only the queue endpoint.
        original = sonarr._client

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v3/queue":
                return httpx.Response(500, json={"message": "boom"})
            return original._transport.handler(request)  # type: ignore[attr-defined]

        sonarr._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].flagged == 0 and reports[0].error
        assert _events_of(events, "profile_sanity.flagged") == []
        assert len(_events_of(events, "profile_sanity.error")) == 1


# ---------------------------------------------------------------------------
# Auto-heal rails
# ---------------------------------------------------------------------------


class TestAutoHeal:
    async def test_default_is_alert_only(self):
        puts: list[dict] = []
        commands: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            profiles=_sonarr_profiles(),
            puts=puts,
            commands=commands,
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(), notifier=notifier
        )
        assert reports[0].flagged == 1 and reports[0].healed == 0
        assert puts == [] and commands == []
        assert _events_of(events, "profile_sanity.healed") == []

    async def test_heal_puts_full_object_with_new_profile(self):
        puts: list[dict] = []
        commands: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            profiles=_sonarr_profiles(),
            puts=puts,
            commands=commands,
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(auto_heal=True), notifier=notifier
        )
        assert reports[0].healed == 1
        assert len(puts) == 1
        body = puts[0]
        assert body["qualityProfileId"] == 1  # "Any"
        # full object preserved, only the profile mutated
        assert body["title"] == "Diagnosis: Murder"
        assert body["statistics"]["totalEpisodeCount"] == 184
        assert body["monitored"] is True
        healed = _events_of(events, "profile_sanity.healed")
        assert len(healed) == 1
        payload = healed[0]["payload"]
        assert payload["old_profile_id"] == 5
        assert payload["old_profile_name"] == "Ultra-HD"
        assert payload["new_profile_id"] == 1
        assert payload["new_profile_name"] == "Any"
        # search_after_heal defaults off
        assert commands == []
        assert payload["searched"] == ""

    async def test_search_after_heal_posts_series_search(self):
        commands: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            profiles=_sonarr_profiles(),
            commands=commands,
        )
        notifier, events = _make_notifier()
        await run_profile_sanity(
            arr_clients=[sonarr],
            config=_cfg(auto_heal=True, search_after_heal=True),
            notifier=notifier,
        )
        assert commands == [{"name": "SeriesSearch", "seriesIds": [678]}]
        payload = _events_of(events, "profile_sanity.healed")[0]["payload"]
        assert "triggered a search" in payload["searched"]

    async def test_max_heals_per_tick_respected(self):
        puts: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items=_flaggable_library(3),
            profiles=_sonarr_profiles(),
            puts=puts,
        )
        notifier, _events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr],
            config=_cfg(auto_heal=True, max_heals_per_tick=1),
            notifier=notifier,
        )
        assert reports[0].flagged == 3 and reports[0].healed == 1
        assert len(puts) == 1

    async def test_max_flags_per_tick_respected(self):
        sonarr = _make_arr(
            "Sonarr", items=_flaggable_library(5), profiles=_sonarr_profiles()
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(max_flags_per_tick=2), notifier=notifier
        )
        assert reports[0].alerted == 2
        assert len(_events_of(events, "profile_sanity.flagged")) == 2
        assert len(reports[0].flags) == 2

    async def test_safe_profile_not_found_blocks_heal(self):
        puts: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            profiles=_sonarr_profiles(),
            puts=puts,
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr],
            config=_cfg(auto_heal=True, safe_profile="Nonexistent"),
            notifier=notifier,
        )
        assert reports[0].flagged == 1 and reports[0].healed == 0 and puts == []
        payload = _events_of(events, "profile_sanity.flagged")[0]["payload"]
        assert "not found" in payload["recommended"]

    async def test_safe_profile_above_ceiling_blocks_heal(self):
        # "HD-720p" has floor 720, still above the 576 ceiling → never heal
        # onto a profile that is just as impossible.
        puts: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            profiles=_sonarr_profiles(),
            puts=puts,
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr],
            config=_cfg(auto_heal=True, safe_profile="HD-720p"),
            notifier=notifier,
        )
        assert reports[0].healed == 0 and puts == []
        payload = _events_of(events, "profile_sanity.flagged")[0]["payload"]
        assert "still above" in payload["recommended"]

    async def test_file_arriving_before_the_write_blocks_heal(self):
        # Gate 2 is re-asserted against a FRESH copy right before the PUT.
        puts: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            fresh_items={
                678: _diagnosis_murder(
                    statistics={"episodeFileCount": 1, "episodeCount": 178}
                )
            },
            profiles=_sonarr_profiles(),
            puts=puts,
        )
        notifier, _events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(auto_heal=True), notifier=notifier
        )
        assert reports[0].flagged == 1 and reports[0].healed == 0 and puts == []

    async def test_put_failure_emits_error_and_does_not_count_a_heal(self):
        sonarr = _make_arr(
            "Sonarr", items={678: _diagnosis_murder()}, profiles=_sonarr_profiles()
        )
        original = sonarr._client

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "PUT":
                return httpx.Response(500, json={"message": "boom"})
            return original._transport.handler(request)  # type: ignore[attr-defined]

        sonarr._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr], config=_cfg(auto_heal=True), notifier=notifier
        )
        assert reports[0].healed == 0
        assert len(_events_of(events, "profile_sanity.error")) == 1


# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------


class TestConfidenceThresholds:
    async def test_score_below_alert_threshold_does_not_alert(self):
        sonarr = _make_arr(
            "Sonarr", items={678: _diagnosis_murder()}, profiles=_sonarr_profiles()
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr],
            # the specimen scores 8; require 9
            config=_cfg(alert_min_confidence=9),
            notifier=notifier,
        )
        assert reports[0].flagged == 0 and events == []

    async def test_score_between_thresholds_alerts_but_never_heals(self):
        puts: list[dict] = []
        sonarr = _make_arr(
            "Sonarr",
            items={678: _diagnosis_murder()},
            profiles=_sonarr_profiles(),
            puts=puts,
        )
        notifier, events = _make_notifier()
        reports = await run_profile_sanity(
            arr_clients=[sonarr],
            config=_cfg(
                auto_heal=True, alert_min_confidence=3, heal_min_confidence=9
            ),
            notifier=notifier,
        )
        assert reports[0].flagged == 1 and reports[0].healed == 0
        assert len(_events_of(events, "profile_sanity.flagged")) == 1
        assert _events_of(events, "profile_sanity.healed") == []
        assert puts == []
