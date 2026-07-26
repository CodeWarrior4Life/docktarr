"""Post-import media QA — catches "dud" video files that import cleanly.

Born from the 2026-07-26 incident: "For All Mankind (2019) - S05E06
[WEBDL-2160p][HDR10][h265]-NT.mkv" imported fine and was valid HEVC Main10
PQ/BT.2020 video, but carried ZERO HDR metadata — no Mastering Display Color
Volume (MDCV), no Content Light Level (CLL), no Dolby Vision configuration,
no HDR10+. Players that tone-map (Plex Desktop, mpv) assume a 10,000-nit
peak for metadata-less PQ and render the picture near-black — the user sees
"audio but no picture". Sibling episodes with DV Profile 8.1 + HDR10+
metadata played fine. A previous incident class: Dolby Vision Profile 5
files (single-layer, no HDR10 fallback) are unplayable ("color space not
supported") on non-DV clients; a Sonarr/Radarr custom-format avoid-rule
exists but files still slip in.

Detection rules (each candidate file is probed with ffprobe, JSON output):

1. **DUD-DV5** — the video stream carries a "DOVI configuration record"
   with ``dv_profile == 5`` (single-layer IPTPQc2, no HDR10 base layer)
   → flag ``dovi_p5_no_fallback``.
2. **DUD-BARE-PQ** — ``color_transfer == smpte2084`` (PQ) AND no Mastering
   display metadata AND no Content light level metadata AND no DOVI
   configuration record AND no HDR10+ dynamic metadata → flag
   ``pq_missing_hdr_metadata``. Frame side data comes from a second probe:
   ``ffprobe -select_streams v:0 -show_frames -read_intervals "%+#1"``
   (first frame only — cheap even on 2160p HEVC).
3. **DUD-NO-VIDEO** — no real video stream at all (attached-pic cover art
   doesn't count), or the video duration is < ``truncation_ratio`` (default
   25%) of the arr-reported runtime → flag ``missing_or_truncated_video``.

Two scan modes, mirroring :mod:`docktarr.imposter_detector`:

* recent: :func:`run_media_qa` — probes files from downloadFolderImported
  history within ``lookback`` (cheap; runs hourly by default).
* backfill: :func:`run_media_qa_backfill` — walks every monitored
  series/movie and probes every file (expensive; opt-in, slow cadence).

**How ffprobe runs.** The docktarr container mounts no media; like
:mod:`docktarr.artwork_health`'s presence spot-check, this module ``docker
exec``s into the *arr container itself* (via ``DockerManager.exec_run``),
which by definition has the media mounted at exactly the paths the arr API
reports, and — since Sonarr v4 / Radarr v4+ — bundles ``ffprobe`` for its
own media analysis. The binary is discovered once per container (``ffprobe``
on PATH, then the known linuxserver/hotio bundle locations) and cached;
``MEDIA_QA_FFPROBE_PATH`` / ``MEDIA_QA_FFPROBE_CONTAINER`` override the
binary / target container (an override container must share the arr's mount
layout). No ffprobe found → single deduped ``media_qa.error``, never a crash.

Action on detection is **ALERT-ONLY by default**: a ``media_qa.flagged``
event (Telegram + webhook) naming the series/movie, the flag reason, and the
file path. ``auto_remediate`` (default **off**) opts in to the same path
:mod:`docktarr.imposter_detector` uses — delete the file via the arr API and
trigger an EpisodeSearch / MoviesSearch re-search, emitting
``media_qa.remediated``.

Safety: a probe *failure* (ffprobe non-zero / unparseable output) is NEVER
treated as a dud verdict — a stale NFS mount would otherwise mass-flag (and,
with auto-remediate on, mass-delete) a healthy library. Failures emit a
deduped ``media_qa.error`` instead. Verdicts are cached per (path, size) so
a file is probed and alerted once, not every tick.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docktarr.arrclient import ArrClient
    from docktarr.docker_manager import DockerManager
    from docktarr.http_health import HealthState
    from docktarr.notifier import Notifier

log = logging.getLogger("docktarr.media_qa")

# Flag reasons (the three dud classes)
FLAG_DV5 = "dovi_p5_no_fallback"
FLAG_BARE_PQ = "pq_missing_hdr_metadata"
FLAG_NO_VIDEO = "missing_or_truncated_video"

DEFAULT_TRUNCATION_RATIO = 0.25
# Same guard as imposter_detector: don't reason about runtimes for shorts.
MIN_RUNTIME_MINUTES = 10

# ffprobe argument sets. ``-v quiet`` keeps stderr out of the merged
# exec_run output so stdout parses as pure JSON.
_STREAMS_ARGS = (
    "-v",
    "quiet",
    "-print_format",
    "json",
    "-show_streams",
    "-show_format",
)
_FRAMES_ARGS = (
    "-v",
    "quiet",
    "-print_format",
    "json",
    "-select_streams",
    "v:0",
    "-show_frames",
    "-read_intervals",
    "%+#1",
)

# Where ffprobe lives, in probe order. Sonarr v4 / Radarr v4+ bundle ffprobe
# next to their executable; linuxserver images put the app under /app/<name>/bin,
# hotio under /app/bin, distro packages under /usr/lib/<name>/bin.
_FFPROBE_CANDIDATES = (
    "ffprobe",
    "/app/sonarr/bin/ffprobe",
    "/app/radarr/bin/ffprobe",
    "/app/bin/ffprobe",
    "/usr/lib/sonarr/bin/ffprobe",
    "/usr/lib/radarr/bin/ffprobe",
)

# Frame side-data types that count as "real HDR metadata is present".
_HDR_FRAME_SIDE_DATA = (
    "mastering display metadata",
    "content light level metadata",
    "hdr10+ dynamic metadata",
)

_DOVI_SIDE_DATA = "dovi configuration record"


@dataclass(frozen=True)
class MediaQaConfig:
    """Tunables for the media QA watchdog."""

    enabled: bool = False
    auto_remediate: bool = False  # ALERT-ONLY by default
    truncation_ratio: float = DEFAULT_TRUNCATION_RATIO
    ffprobe_path: str | None = None  # explicit binary path override
    ffprobe_container: str | None = None  # exec target override (else the arr)
    probe_timeout: float = 120.0


@dataclass
class MediaQaState:
    """Mutable state shared across scheduler ticks.

    ``verdicts`` caches probe outcomes per ``"{path}|{size}"`` — a clean file
    stores ``""``, a dud stores its flag — so each imported file is probed and
    alerted exactly once (a quality-upgrade replacement changes the size and
    re-probes). ``errored`` dedups probe-failure alerts the same way.
    ``ffprobe_bins`` caches the resolved ffprobe path per container.
    """

    verdicts: dict[str, str] = field(default_factory=dict)
    errored: set[str] = field(default_factory=set)
    ffprobe_bins: dict[str, str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class MediaCandidate:
    """A single file to probe, normalized across Sonarr/Radarr."""

    service: str
    item_id: int  # episode id (Sonarr) / movie id (Radarr) — for re-search
    file_id: int | None  # episodefile / moviefile id — for deletion
    name: str  # human-readable ("Show S05E06 - Title" / "Movie (2024)")
    path: str  # file path AS THE ARR CONTAINER SEES IT
    size: int | None
    expected_runtime_min: float | None


@dataclass
class MediaQaReport:
    service: str
    scanned: int = 0
    probed: int = 0
    flagged: int = 0
    remediated: int = 0
    probe_errors: int = 0
    flags: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "scanned": self.scanned,
            "probed": self.probed,
            "flagged": self.flagged,
            "remediated": self.remediated,
            "probe_errors": self.probe_errors,
            "flags": self.flags,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Pure evaluation (unit-testable, no I/O)
# ---------------------------------------------------------------------------


def _video_stream(probe: dict) -> dict | None:
    """First real video stream — attached-pic cover art doesn't count."""
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue
        return stream
    return None


def _dovi_record(stream: dict) -> dict | None:
    for sd in stream.get("side_data_list") or []:
        if _DOVI_SIDE_DATA in str(sd.get("side_data_type", "")).lower():
            return sd
    return None


def _frame_hdr_side_data(frames: dict) -> set[str]:
    """HDR-relevant side-data types found on the probed frame(s)."""
    found: set[str] = set()
    for frame in frames.get("frames") or []:
        for sd in frame.get("side_data_list") or []:
            sd_type = str(sd.get("side_data_type", "")).lower()
            for want in _HDR_FRAME_SIDE_DATA:
                if want in sd_type:
                    found.add(want)
    return found


def _parse_tag_duration(raw: str) -> float | None:
    """Parse an mkv tags DURATION like '00:55:00.123456789' to seconds."""
    parts = raw.strip().split(":")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        return None


def _duration_seconds(video: dict, fmt: dict) -> float | None:
    """Video duration in seconds: stream, then container, then mkv tags."""
    for raw in (video.get("duration"), fmt.get("duration")):
        if raw is None:
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    tags = video.get("tags") or {}
    for key, raw in tags.items():
        if key.upper().startswith("DURATION"):
            parsed = _parse_tag_duration(str(raw))
            if parsed is not None:
                return parsed
    return None


def needs_frame_probe(probe: dict) -> bool:
    """True when the DUD-BARE-PQ rule needs first-frame side data: a PQ
    (smpte2084) video stream with no DOVI configuration record. DV streams
    are decided from stream side data alone; SDR streams need nothing."""
    video = _video_stream(probe)
    if video is None:
        return False
    if str(video.get("color_transfer", "")).lower() != "smpte2084":
        return False
    return _dovi_record(video) is None


def evaluate_media(
    probe: dict,
    frames: dict | None,
    *,
    expected_runtime_min: float | None,
    truncation_ratio: float = DEFAULT_TRUNCATION_RATIO,
) -> tuple[str | None, str]:
    """Apply the three dud rules to ffprobe output. Returns (flag, detail);
    flag is None for a healthy file. ``frames`` may be None when
    :func:`needs_frame_probe` said it wasn't required."""
    video = _video_stream(probe)
    fmt = probe.get("format") or {}

    # --- Rule 3a: no video stream at all -------------------------------
    if video is None:
        return FLAG_NO_VIDEO, "no video stream in container"

    # --- Rule 1: Dolby Vision Profile 5 (no HDR10 fallback) ------------
    dovi = _dovi_record(video)
    if dovi is not None:
        try:
            dv_profile = int(dovi.get("dv_profile", -1))
        except (TypeError, ValueError):
            dv_profile = -1
        if dv_profile == 5:
            return (
                FLAG_DV5,
                "Dolby Vision profile 5 (single-layer, no HDR10 fallback) — "
                "unplayable on non-DV clients",
            )

    # --- Rule 2: PQ transfer with zero HDR metadata ---------------------
    if str(video.get("color_transfer", "")).lower() == "smpte2084" and dovi is None:
        if frames is not None and not _frame_hdr_side_data(frames):
            return (
                FLAG_BARE_PQ,
                "PQ (smpte2084) video with no mastering-display, "
                "content-light-level, Dolby Vision, or HDR10+ metadata — "
                "tone-mapping players assume a 10,000-nit peak and render "
                "near-black",
            )

    # --- Rule 3b: truncated video ---------------------------------------
    if expected_runtime_min and expected_runtime_min >= MIN_RUNTIME_MINUTES:
        duration = _duration_seconds(video, fmt)
        if duration is not None:
            floor = expected_runtime_min * 60 * truncation_ratio
            if duration < floor:
                return (
                    FLAG_NO_VIDEO,
                    f"video duration {duration / 60:.1f}m is below "
                    f"{truncation_ratio:.0%} of the expected "
                    f"{expected_runtime_min:.0f}m runtime",
                )

    return None, ""


# ---------------------------------------------------------------------------
# ffprobe via docker exec
# ---------------------------------------------------------------------------


def _parse_ffprobe_json(output: str) -> dict | None:
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        # exec_run merges stdout+stderr; salvage the JSON body if anything
        # leaked around it.
        start = output.find("{")
        end = output.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(output[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def _resolve_ffprobe(
    dm: "DockerManager",
    container: str,
    state: MediaQaState,
    override: str | None,
) -> str | None:
    """Find a working ffprobe binary inside ``container`` (cached)."""
    if container in state.ffprobe_bins:
        return state.ffprobe_bins[container]
    candidates = (override,) if override else _FFPROBE_CANDIDATES
    resolved: str | None = None
    for candidate in candidates:
        try:
            code, _ = await dm.exec_run(container, [candidate, "-version"])
        except Exception as exc:
            log.debug(
                "media_qa[%s]: ffprobe candidate %s probe failed: %s",
                container,
                candidate,
                exc,
            )
            continue
        if code == 0:
            resolved = candidate
            break
    state.ffprobe_bins[container] = resolved
    if resolved:
        log.info("media_qa[%s]: using ffprobe at %s", container, resolved)
    return resolved


async def _probe_file(
    dm: "DockerManager",
    container: str,
    ffprobe: str,
    path: str,
    timeout: float,
) -> tuple[dict | None, dict | None, str | None]:
    """Probe one file. Returns (streams_probe, frames_probe, error)."""
    code, out = await dm.exec_run(
        container, [ffprobe, *_STREAMS_ARGS, path], timeout=timeout
    )
    if code != 0:
        return None, None, f"ffprobe exited {code}"
    probe = _parse_ffprobe_json(out)
    if probe is None:
        return None, None, "unparseable ffprobe output"

    frames: dict | None = None
    if needs_frame_probe(probe):
        code, out = await dm.exec_run(
            container, [ffprobe, *_FRAMES_ARGS, path], timeout=timeout
        )
        if code == 0:
            frames = _parse_ffprobe_json(out)
        if frames is None:
            # Can't decide the bare-PQ rule without frame side data; treat as
            # a probe error rather than guessing either way.
            return probe, None, f"frame probe failed (exit {code})"
    return probe, frames, None


# ---------------------------------------------------------------------------
# Candidate gathering (arr APIs)
# ---------------------------------------------------------------------------


def _episode_candidate(episode: dict, series: dict | None = None) -> MediaCandidate | None:
    ep_file = episode.get("episodeFile")
    if not ep_file or not ep_file.get("path"):
        return None
    series = series or episode.get("series") or {}
    name = (
        f"{series.get('title', 'Unknown')} "
        f"S{episode.get('seasonNumber', 0):02d}"
        f"E{episode.get('episodeNumber', 0):02d}"
        f" - {episode.get('title', '')}"
    ).strip(" -")
    return MediaCandidate(
        service="Sonarr",
        item_id=episode.get("id", 0),
        file_id=ep_file.get("id"),
        name=name,
        path=ep_file["path"],
        size=ep_file.get("size"),
        expected_runtime_min=episode.get("runtime") or None,
    )


def _movie_candidate(movie: dict) -> MediaCandidate | None:
    mv_file = movie.get("movieFile")
    if not mv_file or not mv_file.get("path"):
        return None
    year = movie.get("year")
    name = f"{movie.get('title', 'Unknown')}" + (f" ({year})" if year else "")
    return MediaCandidate(
        service="Radarr",
        item_id=movie.get("id", 0),
        file_id=mv_file.get("id"),
        name=name,
        path=mv_file["path"],
        size=mv_file.get("size"),
        expected_runtime_min=movie.get("runtime") or None,
    )


async def _recent_candidates_sonarr(
    sonarr: "ArrClient", lookback: timedelta
) -> list[MediaCandidate]:
    """Recently imported episodes — same history lookback pattern as
    imposter_detector. Sonarr v4 rejects a string ``eventType`` query
    parameter with 400 (the param is an integer enum), so filter by the
    string ``eventType`` in the response client-side — safe across
    Sonarr versions."""
    cutoff = datetime.now(timezone.utc) - lookback
    resp = await sonarr._client.get(
        f"{sonarr._url}/api/v3/history",
        params={
            "pageSize": 100,
            "sortKey": "date",
            "sortDirection": "descending",
        },
        headers=sonarr._headers(),
    )
    resp.raise_for_status()
    records = resp.json().get("records", [])

    episode_ids: set[int] = set()
    for record in records:
        if record.get("eventType") != "downloadFolderImported":
            continue
        record_date = record.get("date", "")
        if record_date and record_date[:19] < cutoff.strftime("%Y-%m-%dT%H:%M:%S"):
            continue
        ep_id = record.get("episodeId")
        if ep_id:
            episode_ids.add(ep_id)

    candidates: list[MediaCandidate] = []
    for ep_id in sorted(episode_ids):
        try:
            resp = await sonarr._client.get(
                f"{sonarr._url}/api/v3/episode/{ep_id}",
                params={"includeSeries": "true"},
                headers=sonarr._headers(),
            )
            resp.raise_for_status()
        except Exception:
            continue
        candidate = _episode_candidate(resp.json())
        if candidate:
            candidates.append(candidate)
    return candidates


async def _recent_candidates_radarr(
    radarr: "ArrClient", lookback: timedelta
) -> list[MediaCandidate]:
    """Recently imported movies. Radarr's history eventType query parameter
    is an integer enum, so filter by the string eventType client-side."""
    cutoff = datetime.now(timezone.utc) - lookback
    resp = await radarr._client.get(
        f"{radarr._url}/api/v3/history",
        params={"pageSize": 100, "sortKey": "date", "sortDirection": "descending"},
        headers=radarr._headers(),
    )
    resp.raise_for_status()
    records = resp.json().get("records", [])

    movie_ids: set[int] = set()
    for record in records:
        if record.get("eventType") != "downloadFolderImported":
            continue
        record_date = record.get("date", "")
        if record_date and record_date[:19] < cutoff.strftime("%Y-%m-%dT%H:%M:%S"):
            continue
        movie_id = record.get("movieId")
        if movie_id:
            movie_ids.add(movie_id)

    candidates: list[MediaCandidate] = []
    for movie_id in sorted(movie_ids):
        try:
            resp = await radarr._client.get(
                f"{radarr._url}/api/v3/movie/{movie_id}",
                headers=radarr._headers(),
            )
            resp.raise_for_status()
        except Exception:
            continue
        candidate = _movie_candidate(resp.json())
        if candidate:
            candidates.append(candidate)
    return candidates


async def _backfill_candidates_sonarr(sonarr: "ArrClient") -> list[MediaCandidate]:
    resp = await sonarr._client.get(
        f"{sonarr._url}/api/v3/series",
        headers=sonarr._headers(),
    )
    resp.raise_for_status()
    candidates: list[MediaCandidate] = []
    for series in resp.json():
        if not series.get("monitored") or not series.get("id"):
            continue
        ep_resp = await sonarr._client.get(
            f"{sonarr._url}/api/v3/episode",
            params={"seriesId": series["id"], "includeEpisodeFile": "true"},
            headers=sonarr._headers(),
        )
        if ep_resp.status_code != 200:
            continue
        for episode in ep_resp.json():
            if not episode.get("hasFile"):
                continue
            candidate = _episode_candidate(episode, series=series)
            if candidate:
                candidates.append(candidate)
    return candidates


async def _backfill_candidates_radarr(radarr: "ArrClient") -> list[MediaCandidate]:
    resp = await radarr._client.get(
        f"{radarr._url}/api/v3/movie",
        headers=radarr._headers(),
    )
    resp.raise_for_status()
    candidates: list[MediaCandidate] = []
    for movie in resp.json():
        if not movie.get("monitored") or not movie.get("hasFile"):
            continue
        candidate = _movie_candidate(movie)
        if candidate:
            candidates.append(candidate)
    return candidates


# ---------------------------------------------------------------------------
# Remediation (opt-in; same path imposter_detector uses)
# ---------------------------------------------------------------------------


async def _remediate(client: "ArrClient", candidate: MediaCandidate) -> bool:
    """Delete the dud file via the arr API and trigger a re-search."""
    try:
        if client.name == "Sonarr":
            await client._client.delete(
                f"{client._url}/api/v3/episodefile/{candidate.file_id}",
                headers=client._headers(),
            )
            await client._client.post(
                f"{client._url}/api/v3/command",
                json={"name": "EpisodeSearch", "episodeIds": [candidate.item_id]},
                headers=client._headers(),
            )
        else:
            await client._client.delete(
                f"{client._url}/api/v3/moviefile/{candidate.file_id}",
                headers=client._headers(),
            )
            await client._client.post(
                f"{client._url}/api/v3/command",
                json={"name": "MoviesSearch", "movieIds": [candidate.item_id]},
                headers=client._headers(),
            )
        return True
    except Exception as exc:
        log.warning(
            "media_qa[%s]: remediation failed for %s: %s",
            client.name,
            candidate.path,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Per-service scan
# ---------------------------------------------------------------------------


async def _scan_candidates(
    *,
    client: "ArrClient",
    candidates: list[MediaCandidate],
    config: MediaQaConfig,
    notifier: "Notifier",
    state: MediaQaState,
    docker_manager: "DockerManager",
) -> MediaQaReport:
    service = client.name
    report = MediaQaReport(service=service)
    if not candidates:
        return report

    container = config.ffprobe_container or client.container_name
    ffprobe = await _resolve_ffprobe(
        docker_manager, container, state, config.ffprobe_path
    )
    if ffprobe is None:
        report.error = f"no ffprobe binary found in container {container!r}"
        error_key = f"{container}:no_ffprobe"
        if error_key not in state.errored:
            state.errored.add(error_key)
            log.error("media_qa[%s]: %s", service, report.error)
            await notifier.emit(
                "media_qa.error", {"service": service, "error": report.error}
            )
        return report

    for candidate in candidates:
        report.scanned += 1
        cache_key = f"{candidate.path}|{candidate.size}"
        if cache_key in state.verdicts:
            continue

        try:
            probe, frames, probe_error = await _probe_file(
                docker_manager,
                container,
                ffprobe,
                candidate.path,
                config.probe_timeout,
            )
        except Exception as exc:
            probe, frames, probe_error = None, None, str(exc)
        report.probed += 1

        if probe_error:
            # NEVER a dud verdict — a stale mount would mass-flag the library.
            report.probe_errors += 1
            if cache_key not in state.errored:
                state.errored.add(cache_key)
                log.warning(
                    "media_qa[%s]: probe error for %s: %s",
                    service,
                    candidate.path,
                    probe_error,
                )
                await notifier.emit(
                    "media_qa.error",
                    {
                        "service": service,
                        "error": f"{candidate.path}: {probe_error}",
                    },
                )
            continue

        flag, detail = evaluate_media(
            probe,
            frames,
            expected_runtime_min=candidate.expected_runtime_min,
            truncation_ratio=config.truncation_ratio,
        )
        if flag is None:
            state.verdicts[cache_key] = ""
            continue

        state.verdicts[cache_key] = flag
        report.flagged += 1
        if len(report.flags) < 10:
            report.flags.append(
                {"name": candidate.name, "reason": flag, "file": candidate.path}
            )
        log.warning(
            "media_qa[%s]: DUD FILE (%s): %s — %s. File: %s",
            service,
            flag,
            candidate.name,
            detail,
            candidate.path,
        )
        await notifier.emit(
            "media_qa.flagged",
            {
                "service": service,
                "name": candidate.name,
                "reason": flag,
                "detail": detail,
                "file": candidate.path,
            },
        )

        if config.auto_remediate and candidate.file_id:
            if await _remediate(client, candidate):
                report.remediated += 1
                # File is gone; drop the verdict so a re-grab re-probes.
                state.verdicts.pop(cache_key, None)
                log.info(
                    "media_qa[%s]: deleted %s and triggered re-search",
                    service,
                    candidate.path,
                )
                await notifier.emit(
                    "media_qa.remediated",
                    {
                        "service": service,
                        "name": candidate.name,
                        "reason": flag,
                        "file": candidate.path,
                    },
                )
            else:
                await notifier.emit(
                    "media_qa.error",
                    {
                        "service": service,
                        "error": f"remediation failed for {candidate.path}",
                    },
                )

    return report


# ---------------------------------------------------------------------------
# Top-level entry points
# ---------------------------------------------------------------------------


async def _run(
    *,
    arr_clients: list["ArrClient"],
    config: MediaQaConfig,
    notifier: "Notifier",
    docker_manager: "DockerManager | None",
    state: MediaQaState | None,
    health_state: "HealthState | None",
    gather,
    mode: str,
) -> list[MediaQaReport]:
    if not config.enabled:
        return []
    if docker_manager is None:
        log.debug("media_qa: no DockerManager available, skipping (%s)", mode)
        return []
    if state is None:
        state = MediaQaState()

    reports: list[MediaQaReport] = []
    for client in arr_clients:
        if getattr(client, "name", None) not in ("Sonarr", "Radarr"):
            continue
        try:
            candidates = await gather(client)
        except Exception as exc:
            log.warning(
                "media_qa[%s]: candidate gathering failed (%s): %s",
                client.name,
                mode,
                exc,
            )
            report = MediaQaReport(service=client.name, error=str(exc))
            await notifier.emit(
                "media_qa.error",
                {"service": client.name, "error": str(exc)},
            )
            reports.append(report)
            continue
        report = await _scan_candidates(
            client=client,
            candidates=candidates,
            config=config,
            notifier=notifier,
            state=state,
            docker_manager=docker_manager,
        )
        reports.append(report)
        log.info(
            "media_qa[%s] %s: scanned %d, probed %d, flagged %d, "
            "remediated %d, probe_errors %d",
            client.name,
            mode,
            report.scanned,
            report.probed,
            report.flagged,
            report.remediated,
            report.probe_errors,
        )

    if health_state is not None and hasattr(health_state, "record_media_qa"):
        health_state.record_media_qa([r.to_dict() for r in reports])
    return reports


async def run_media_qa(
    *,
    arr_clients: list["ArrClient"],
    config: MediaQaConfig,
    notifier: "Notifier",
    docker_manager: "DockerManager | None" = None,
    state: MediaQaState | None = None,
    health_state: "HealthState | None" = None,
    lookback: timedelta = timedelta(hours=24),
) -> list[MediaQaReport]:
    """Probe recently imported files for dud signals. Never raises."""

    async def gather(client: "ArrClient") -> list[MediaCandidate]:
        if client.name == "Sonarr":
            return await _recent_candidates_sonarr(client, lookback)
        return await _recent_candidates_radarr(client, lookback)

    return await _run(
        arr_clients=arr_clients,
        config=config,
        notifier=notifier,
        docker_manager=docker_manager,
        state=state,
        health_state=health_state,
        gather=gather,
        mode="recent",
    )


async def run_media_qa_backfill(
    *,
    arr_clients: list["ArrClient"],
    config: MediaQaConfig,
    notifier: "Notifier",
    docker_manager: "DockerManager | None" = None,
    state: MediaQaState | None = None,
    health_state: "HealthState | None" = None,
) -> list[MediaQaReport]:
    """Full-library scan: probe every monitored file. Expensive (one or two
    ffprobe invocations per file) — meant for a slow, opt-in cadence. Catches
    duds imported before this module existed. Never raises."""

    async def gather(client: "ArrClient") -> list[MediaCandidate]:
        if client.name == "Sonarr":
            return await _backfill_candidates_sonarr(client)
        return await _backfill_candidates_radarr(client)

    return await _run(
        arr_clients=arr_clients,
        config=config,
        notifier=notifier,
        docker_manager=docker_manager,
        state=state,
        health_state=health_state,
        gather=gather,
        mode="backfill",
    )
