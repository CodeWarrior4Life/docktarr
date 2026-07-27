"""Impossible-quality-profile detector for Sonarr/Radarr ("profile sanity").

Born from the 2026-07-27 incident: three Seerr requests read **Failed** with
zero errors anywhere — no failed download, no import error, green
``/api/v3/health``, a live indexer set. The cause was that each item had been
assigned a quality profile it can *never* satisfy, so Sonarr/Radarr searched
forever and nothing ever errored:

* Sonarr series 678 **Diagnosis: Murder** (1993, ended, SD-only masters) sat
  on quality profile 5 "Ultra-HD" → 0 of 184 episodes, forever.
* Radarr movie 1127 **Charlie's Angels** (1976, a 74-minute TV pilot, never
  in cinemas) sat on quality profile 7 "UHD 4k Remux" → no file, forever.

Nothing in the *arr stack surfaces this class of failure: an unsatisfiable
profile is indistinguishable, from the app's point of view, from "the release
just hasn't been posted yet". The manual diagnosis step — compare a title's
era / available real-world sources against the *floor* of its quality profile
— is what this module automates.

Detection is deliberately two-layered:

**HARD GATES** (every one must pass or the item is never flagged, never
healed):

1. ``monitored`` is true.
2. **Zero files.** Sonarr: ``statistics.episodeFileCount == 0`` *and*
   ``statistics.episodeCount > 0`` (there are aired episodes to get). Radarr:
   no ``hasFile`` and no ``movieFileId``. Any file at all *proves* the profile
   is satisfiable.
3. **Released / airable.** The unreleased-title exclusion — an announced movie
   with no file is normal, not broken (Radarr 1128 "Spider-Man: Brand New Day",
   status ``announced``, ``isAvailable`` false, ``inCinemas`` 2026-07-28: MUST
   NEVER be flagged or healed).
4. **Profile floor is known, era ceiling is known, and floor > ceiling.**
   Without that contradiction there is no "impossible profile"; a starved title
   on a *satisfiable* profile is a different problem and out of scope.
5. **Starvation.** ``now - added >= min_starvation_age`` (default 3d) AND the
   item's per-item history contains no ``grabbed`` and no
   ``downloadFolderImported`` event. A single ``grabbed`` event proves the
   profile IS satisfiable and the failure is transient (download/import fault).
   This signal is weighted heavily — it is the most robust one and generalises
   past the era heuristics.
6. **Not currently in the download queue.**
7. **Stack-level guards pass** (below).

**CONFIDENCE SCORE** (computed only for items past every gate) gates alert vs
auto-heal: floor-above-ceiling contradiction +2, starved +2 (+1 more when
``now - added >= severe_starvation_age``), pre-HD era +2, TV-pilot-shaped
runtime +1. Alert at ``alert_min_confidence`` (default 3); auto-heal only at
``heal_min_confidence`` (default 5) — healing is strictly more conservative
than alerting.

**STACK-LEVEL GUARDS.** A stack-wide search outage looks *exactly* like
starvation, and mass-flagging (or worse, mass-healing) a library on an outage
would be far more damaging than the bug this module fixes. Two guards, both
fail-closed:

* :func:`indexer_outage` — no indexer has Automatic Search enabled, or
  ``/api/v3/health`` says "all indexers are unavailable" / "no indexers
  available", or every auto-search indexer is named in an
  ``Indexer*Check`` failure message. **If either API call fails that is also
  treated as an outage.** On outage: one deduped
  ``profile_sanity.indexer_outage`` and nothing is flagged for that service.
  (``/api/v3/indexerstatus`` is NOT used — it 404s on Sonarr v4.)
* Library-wide starvation ratio — if more than ``max_starved_ratio`` (default
  0.5) of the *eligible* items (gates 1-3 + 6) are starved and there are at
  least ``min_library_size`` (default 20) of them, emit
  ``profile_sanity.outage_suspected`` and flag nothing. Catches an outage the
  health endpoint does not surface, plus the brand-new-library case.

**AUTO-HEAL** is opt-in and OFF by default. When on, the item is reassigned —
per item, never globally — to a safe profile resolved BY NAME (default "Any");
if that name is not found, or its own floor still exceeds the item's ceiling,
nothing is healed and the alert says so. Gate 2 (zero files) is re-asserted
against a freshly fetched item immediately before the write, the full item
object is PUT with only ``qualityProfileId`` mutated, and before/after ids +
names are logged and emitted so every change is trivially reversible. A global
quality profile is NEVER modified.

Every network call is timeout-bounded and wrapped; a failure degrades to a
``profile_sanity.error`` event and an unflagged tick, never a crash.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from docktarr.arrclient import ArrClient
    from docktarr.http_health import HealthState
    from docktarr.notifier import Notifier

log = logging.getLogger("docktarr.profile_sanity")

# Radarr statuses that mean "this title cannot be downloaded yet" (lowercased).
_UNRELEASED_MOVIE_STATUS = frozenset({"announced", "incinemas", "tba", "deleted"})

# Per-item history events that PROVE the profile is satisfiable (lowercased).
_SATISFIABLE_EVENTS = frozenset({"grabbed", "downloadfolderimported"})

# Some qualities report ``resolution: 0`` — verified on Radarr DVD (id 2) and
# WORKPRINT (id 24). Zero means "unknown", not "zero lines"; fall back to a
# name table for the SD-ish sources, and if the name is unknown too (e.g.
# Sonarr's "Unknown") leave the quality out of the floor computation entirely
# rather than guess a floor that would manufacture a false contradiction.
_SD_NAME_RESOLUTIONS: dict[str, int] = {
    "SDTV": 480,
    "DVD": 480,
    "DVD-R": 480,
    "DVDSCR": 480,
    "REGIONAL": 480,
    "WORKPRINT": 480,
    "TELESYNC": 480,
    "TELECINE": 480,
    "CAM": 480,
}

# Health-check message fragments that mean the whole indexer set is out.
_TOTAL_OUTAGE_FRAGMENTS = (
    "all indexers are unavailable",
    "no indexers available",
)

_QUEUE_PAGE_SIZE = 1000


@dataclass(frozen=True)
class ProfileSanityConfig:
    """Tunables for the impossible-profile detector."""

    enabled: bool = False
    # --- remediation rails (all conservative by default) ---
    auto_heal: bool = False  # ALERT-ONLY by default
    safe_profile: str = "Any"  # resolved BY NAME, per item, never globally
    search_after_heal: bool = False
    max_heals_per_tick: int = 5  # blast-radius cap
    max_flags_per_tick: int = 10  # alert-volume cap
    # --- detection thresholds ---
    min_starvation_age: timedelta = timedelta(days=3)
    severe_starvation_age: timedelta = timedelta(days=14)
    sd_era_year: int = 1998
    tv_movie_runtime_max: int = 100
    alert_min_confidence: int = 3
    heal_min_confidence: int = 5
    # --- stack-level outage guards ---
    max_starved_ratio: float = 0.5
    min_library_size: int = 20
    # alert dedup: fire once the condition has held for this many ticks.
    debounce: int = 1


@dataclass
class ProfileSanityState:
    """Mutable per-instance state shared across scheduler ticks.

    Keyed by ``"{service}:{item_id}"`` for per-item flags and
    ``"{service}:{signal}"`` for stack-level signals, it counts consecutive
    breaching ticks so alerts are deduped — an item alerts once and stays
    quiet until the condition clears.
    """

    breach_ticks: dict[str, int] = field(default_factory=dict)
    debounce: int = 1

    def note(self, key: str, breaching: bool) -> bool:
        """Advance the counter for ``key`` and return True iff an alert should
        fire on THIS tick (the counter just crossed the debounce threshold).
        Non-breaching ticks reset the counter; while the condition persists the
        alert is suppressed, re-arming only after it clears."""
        if not breaching:
            self.breach_ticks.pop(key, None)
            return False
        count = self.breach_ticks.get(key, 0) + 1
        self.breach_ticks[key] = count
        return count == max(1, self.debounce)


@dataclass
class ProfileSanityReport:
    service: str
    profiles: int = 0
    scanned: int = 0
    eligible: int = 0
    starved: int = 0
    flagged: int = 0
    alerted: int = 0
    healed: int = 0
    suppressed: str | None = None  # outage reason; flags withheld this tick
    flags: list[dict] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "profiles": self.profiles,
            "scanned": self.scanned,
            "eligible": self.eligible,
            "starved": self.starved,
            "flagged": self.flagged,
            "alerted": self.alerted,
            "healed": self.healed,
            "suppressed": self.suppressed,
            "flags": self.flags,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Pure evaluation (unit-testable, no I/O)
# ---------------------------------------------------------------------------


def _parse_dt(raw: object) -> datetime | None:
    """Parse an arr timestamp (ISO-8601, ``Z`` or offset, or bare date)."""
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def quality_resolution(quality: dict) -> int | None:
    """Vertical resolution of a quality, or None when it can't be determined.

    ``resolution: 0`` is "unknown" (Radarr DVD id 2, WORKPRINT id 24 both
    report it), so fall back to the SD name table; an unrecognised name yields
    None and the caller must leave the quality out of the floor.
    """
    try:
        resolution = int(quality.get("resolution"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        resolution = 0
    if resolution > 0:
        return resolution
    name = str(quality.get("name") or "").strip().upper()
    return _SD_NAME_RESOLUTIONS.get(name)


def allowed_qualities(profile: dict) -> list[dict]:
    """Every allowed quality in a profile, flattened out of its groups.

    ``/api/v3/qualityprofile`` items are either flat —
    ``{"quality": {...}, "items": [], "allowed": bool}`` — or a GROUP:
    ``{"name": "WEB 720p", "items": [<flat entries>], "allowed": bool,
    "id": 1000}``. Verified live: a group's children mirror the group's
    ``allowed``, but collect a child when EITHER its own or its parent's
    ``allowed`` is true so the floor is never overstated.
    """
    out: list[dict] = []
    for entry in profile.get("items") or []:
        children = entry.get("items") or []
        group_allowed = bool(entry.get("allowed"))
        if children:
            for child in children:
                if bool(child.get("allowed")) or group_allowed:
                    quality = child.get("quality")
                    if isinstance(quality, dict):
                        out.append(quality)
        elif group_allowed:
            quality = entry.get("quality")
            if isinstance(quality, dict):
                out.append(quality)
    return out


def profile_floor(profile: dict) -> int | None:
    """Lowest allowed resolution in a quality profile, or None if unknowable.

    The floor — not the "headline" quality — is what decides satisfiability.
    Measured live 2026-07-27: Sonarr 1 Any=480, 2 SD=480, 3 HD-720p=720,
    4 HD-1080p=720, 5 **Ultra-HD=720**, 6 HD-720p/1080p=720; Radarr 1 Any=480,
    7 UHD 4k Remux=720, 8 FHD Remux=720. Note Ultra-HD's floor is 720, NOT
    2160 — never assume the profile's cutoff/name is its floor.
    """
    resolutions = [
        res
        for quality in allowed_qualities(profile)
        if (res := quality_resolution(quality)) is not None
    ]
    return min(resolutions) if resolutions else None


def era_ceiling(
    kind: str,
    *,
    year: int | None,
    runtime: int | None = None,
    in_cinemas: object = None,
    sd_era_year: int = 1998,
    tv_movie_runtime_max: int = 100,
) -> int | None:
    """Best real-world source resolution that plausibly EXISTS for a title.

    Returning None means "no ceiling / unknown", which means the item can never
    be flagged. This function is the core false-positive defence, so it is
    deliberately stingy:

    * **series** — pre-HD television was mastered to videotape or telecined to
      SD; releases are DVD-sourced, so a pre-``sd_era_year`` series tops out
      around 576 (PAL DVD). Modern series get no ceiling: an HD master exists,
      whether or not anyone has seeded it yet.
    * **movie** — year alone is NOT sufficient, and this is where a naive rule
      would do real damage: pre-HD *theatrical* films are routinely remastered
      to 1080p/2160p from the film negative, so "1966" says nothing about the
      best available source. An SD ceiling (576) therefore applies only when
      the title looks TV-sourced / non-theatrical: no ``inCinemas`` date, or a
      runtime below ``tv_movie_runtime_max`` (a TV movie or a series pilot).
      Charlie's Angels (1976, ``inCinemas`` null, runtime 74) is exactly that
      case. A pre-HD title WITH a theatrical date and a feature runtime gets
      1080 instead — a Blu-ray remaster is plausible, a 4K remux may not be —
      so a 720 floor is satisfiable and the title is correctly NOT flagged.
    """
    if year is None:
        return None
    try:
        year_int = int(year)
    except (TypeError, ValueError):
        return None
    if year_int >= sd_era_year:
        return None

    if kind == "series":
        return 576

    if kind == "movie":
        tv_shaped = (not in_cinemas) or (
            runtime is not None and runtime < tv_movie_runtime_max
        )
        return 576 if tv_shaped else 1080

    return None


def hard_gates(
    kind: str,
    item: dict,
    *,
    now: datetime,
    in_queue: bool = False,
) -> tuple[bool, str]:
    """Gates 1, 2, 3 and 6 — the item-intrinsic ones. Returns
    ``(eligible, reason_excluded)``; ``reason_excluded`` is "" when eligible.

    Kept pure (and separate from the profile/starvation gates) both so it is
    directly testable and because "eligible" is the denominator of the
    library-wide starvation-ratio outage guard.
    """
    # --- Gate 1: monitored -------------------------------------------------
    if not item.get("monitored"):
        return False, "not monitored"

    # --- Gate 2: ZERO files (any file proves the profile is satisfiable) ---
    if kind == "series":
        stats = item.get("statistics") or {}
        if (stats.get("episodeFileCount") or 0) > 0:
            return False, "has episode files — profile is satisfiable"
        if (stats.get("episodeCount") or 0) <= 0:
            return False, "no aired episodes to fetch"
    else:
        if item.get("hasFile") or item.get("movieFileId"):
            return False, "has a movie file — profile is satisfiable"

    # --- Gate 3: RELEASED / AIRABLE ---------------------------------------
    # The unreleased-title exclusion. Radarr 1128 "Spider-Man: Brand New Day"
    # (status announced, isAvailable false, inCinemas 2026-07-28, no digital or
    # physical release) has no file for an entirely healthy reason: the movie
    # does not exist yet. Items like that must never be flagged or healed.
    if kind == "series":
        first_aired = _parse_dt(item.get("firstAired"))
        if first_aired is None:
            return False, "no firstAired date — cannot prove anything has aired"
        if first_aired > now:
            return False, "first episode has not aired yet"
        if str(item.get("status") or "").strip().lower() == "upcoming":
            return False, "series status is upcoming"
    else:
        status = str(item.get("status") or "").strip().lower()
        if status in _UNRELEASED_MOVIE_STATUS:
            return False, f"movie status is {status!r} — not released"
        if item.get("isAvailable") is False:
            return False, "movie is not available yet (isAvailable=false)"
        digital = _parse_dt(item.get("digitalRelease"))
        physical = _parse_dt(item.get("physicalRelease"))
        if digital is None and physical is None:
            cinemas = _parse_dt(item.get("inCinemas"))
            if cinemas is None:
                # No release date of any kind — fail closed rather than reason
                # about a title whose availability we cannot establish.
                return False, "no digital, physical or cinema release date"
            if cinemas > now:
                return False, "still in the future (cinema release not reached)"

    # --- Gate 6: not currently downloading --------------------------------
    if in_queue:
        return False, "currently downloading (present in the arr queue)"

    return True, ""


def starvation(
    item: dict,
    history: list[dict],
    *,
    now: datetime,
    min_starvation_age: timedelta,
    severe_starvation_age: timedelta,
) -> tuple[bool, bool, str]:
    """Gate 5. Returns ``(starved, severe, detail)``.

    A single ``grabbed`` (or ``downloadFolderImported``) event in the item's
    own history proves the profile IS satisfiable — whatever went wrong was
    transient (download or import fault), not an impossible profile — so the
    item is never flagged.
    """
    added = _parse_dt(item.get("added"))
    if added is None:
        return False, False, "no added timestamp"
    age = now - added
    if age < min_starvation_age:
        return (
            False,
            False,
            f"added {age.days}d ago, younger than the "
            f"{min_starvation_age.days}d starvation floor",
        )
    for record in history:
        event = str(record.get("eventType") or "").strip().lower()
        if event in _SATISFIABLE_EVENTS:
            return False, False, f"history has a {event!r} event — profile IS satisfiable"
    return (
        True,
        age >= severe_starvation_age,
        f"no grab or import in {age.days}d",
    )


def confidence_score(
    kind: str,
    item: dict,
    *,
    floor: int,
    ceiling: int,
    starved: bool,
    severe: bool,
    sd_era_year: int = 1998,
    tv_movie_runtime_max: int = 100,
) -> tuple[int, list[str]]:
    """Score an item that has passed every hard gate. Returns
    ``(score, signals)``.

    Note the interaction with :func:`era_ceiling`: because a ceiling only
    exists for pre-``sd_era_year`` titles, every item that reaches this
    function scores at least 2 (contradiction) + 2 (starved) + 2 (pre-HD era)
    = 6 under the current ceiling rules. The thresholds are therefore operator
    tightening knobs and headroom for future, less era-bound ceiling rules —
    not a filter that trips on today's signals.
    """
    signals: list[str] = []
    score = 0

    # Always present: gate 4 is what got us here.
    score += 2
    signals.append(f"profile floor {floor}p above era ceiling {ceiling}p (+2)")

    if starved:
        score += 2
        signals.append("starved: no grab since it was added (+2)")
        if severe:
            score += 1
            signals.append("severely starved (+1)")

    year = item.get("year")
    try:
        year_int = int(year)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        year_int = None  # type: ignore[assignment]

    pre_hd = year_int is not None and year_int < sd_era_year
    if pre_hd:
        score += 2
        signals.append(f"pre-HD era ({year_int} < {sd_era_year}) (+2)")

    runtime = item.get("runtime")
    if pre_hd and isinstance(runtime, (int, float)) and runtime < tv_movie_runtime_max:
        score += 1
        signals.append(
            f"TV-movie/pilot-shaped runtime ({int(runtime)}m < "
            f"{tv_movie_runtime_max}m) (+1)"
        )

    return score, signals


def _normalize_indexer_name(name: str) -> str:
    """Lowercase an indexer name and drop a trailing ``(...)`` qualifier.

    Health messages name Prowlarr-synced indexers as "RuTor (Prowlarr)"; the
    ``/api/v3/indexer`` entry may or may not carry the same suffix, so compare
    on the normalized stem.
    """
    text = name.strip().lower()
    if text.endswith(")") and "(" in text:
        text = text[: text.rfind("(")].strip()
    return text


def failing_indexer_names(health_checks: list[dict]) -> set[str]:
    """Indexer names called out as failing by ``/api/v3/health``.

    Real observed messages: ``IndexerStatusCheck`` → "Indexers unavailable due
    to failures: Demonoid Clone (Prowlarr), Elitetorrent-wf (Prowlarr), RuTor
    (Prowlarr)"; ``IndexerLongTermStatusCheck`` → "Indexers unavailable due to
    failures for more than 6 hours: Torrent Downloads (Prowlarr)". Split on the
    first ``": "`` then on ``", "``.
    """
    names: set[str] = set()
    for check in health_checks or []:
        if not str(check.get("source") or "").startswith("Indexer"):
            continue
        message = str(check.get("message") or "")
        _, sep, tail = message.partition(": ")
        if not sep:
            continue
        for raw in tail.split(", "):
            name = raw.strip()
            if name:
                names.add(_normalize_indexer_name(name))
    return names


# ---------------------------------------------------------------------------
# arr API helpers
# ---------------------------------------------------------------------------


async def _get_json(client: "ArrClient", path: str, params: dict | None = None):
    v = client._api_version()
    resp = await client._client.get(
        f"{client._url}/api/{v}/{path}",
        params=params,
        headers=client._headers(),
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


async def _quality_profiles(client: "ArrClient") -> dict[int, dict]:
    data = await _get_json(client, "qualityprofile")
    profiles = data if isinstance(data, list) else data.get("records", [])
    return {int(p["id"]): p for p in profiles if p.get("id") is not None}


async def _library(client: "ArrClient", kind: str) -> list[dict]:
    data = await _get_json(client, "series" if kind == "series" else "movie")
    return data if isinstance(data, list) else data.get("records", [])


async def _queue_item_ids(client: "ArrClient", kind: str) -> set[int]:
    """Ids currently in the download queue. ``/api/v3/queue`` returns
    ``{page, pageSize, totalRecords, records:[...]}``; Sonarr records carry
    ``seriesId``, Radarr records carry ``movieId``."""
    data = await _get_json(client, "queue", {"pageSize": _QUEUE_PAGE_SIZE})
    records = data.get("records", []) if isinstance(data, dict) else (data or [])
    key = "seriesId" if kind == "series" else "movieId"
    ids: set[int] = set()
    for record in records:
        value = record.get(key)
        if value is not None:
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                continue
    return ids


async def _item_history(client: "ArrClient", kind: str, item_id: int) -> list[dict]:
    """Per-item history — a bare list.

    Precedent from PR #8: NEVER pass ``eventType`` as a query parameter
    (Sonarr v4 400s — it is an integer enum there). Always filter the string
    ``eventType`` client-side.
    """
    if kind == "series":
        data = await _get_json(client, "history/series", {"seriesId": item_id})
    else:
        data = await _get_json(client, "history/movie", {"movieId": item_id})
    return data if isinstance(data, list) else data.get("records", [])


async def indexer_outage(client: "ArrClient") -> tuple[bool, str]:
    """Is this service's search capability out? Returns ``(outage, reason)``.

    Fail-CLOSED: an unreachable ``/api/v3/indexer`` or ``/api/v3/health`` is
    reported as an outage, because a stack-wide search outage is
    indistinguishable from starvation and mass-flagging the library would be
    worse than missing a real impossible profile.

    ``/api/v3/indexerstatus`` is deliberately NOT used — it 404s on Sonarr v4.
    """
    try:
        indexers = await _get_json(client, "indexer")
    except Exception as exc:
        return True, f"indexer API unreachable ({exc}) — failing closed"
    if not isinstance(indexers, list):
        indexers = indexers.get("records", []) if isinstance(indexers, dict) else []

    auto_search = [i for i in indexers if i.get("enableAutomaticSearch")]
    if not auto_search:
        return True, "no indexer has Automatic Search enabled"

    try:
        checks = await _get_json(client, "health")
    except Exception as exc:
        return True, f"health API unreachable ({exc}) — failing closed"
    if not isinstance(checks, list):
        checks = checks.get("records", []) if isinstance(checks, dict) else []

    for check in checks:
        if not str(check.get("source") or "").startswith("Indexer"):
            continue
        message = str(check.get("message") or "")
        lowered = message.lower()
        for fragment in _TOTAL_OUTAGE_FRAGMENTS:
            if fragment in lowered:
                return True, message

    failing = failing_indexer_names(checks)
    if failing:
        auto_names = {
            _normalize_indexer_name(str(i.get("name") or "")) for i in auto_search
        }
        auto_names.discard("")
        if auto_names and auto_names <= failing:
            return True, (
                "every Automatic-Search indexer is reported failing: "
                + ", ".join(sorted(auto_names))
            )

    return False, ""


async def _reassign_profile(
    client: "ArrClient", kind: str, item: dict, new_profile_id: int
) -> None:
    """PUT the full item object back with ONLY ``qualityProfileId`` mutated.

    Per-item reassignment only — a global quality profile is never PUT and
    never loosened.
    """
    v = client._api_version()
    endpoint = "series" if kind == "series" else "movie"
    body = dict(item)
    body["qualityProfileId"] = new_profile_id
    resp = await client._client.put(
        f"{client._url}/api/{v}/{endpoint}/{item['id']}",
        json=body,
        headers=client._headers(),
        timeout=30.0,
    )
    resp.raise_for_status()


async def _trigger_search(client: "ArrClient", kind: str, item_id: int) -> None:
    v = client._api_version()
    if kind == "series":
        body = {"name": "SeriesSearch", "seriesIds": [item_id]}
    else:
        body = {"name": "MoviesSearch", "movieIds": [item_id]}
    resp = await client._client.post(
        f"{client._url}/api/{v}/command",
        json=body,
        headers=client._headers(),
        timeout=30.0,
    )
    resp.raise_for_status()


def _resolve_safe_profile(
    profiles: dict[int, dict], name: str, ceiling: int
) -> tuple[dict | None, str]:
    """Find the safe profile by NAME and verify it is actually satisfiable.

    Returns ``(profile, blocked_reason)``; ``profile`` is None (with a reason)
    when the name is missing or its own floor still exceeds the item's ceiling.
    """
    wanted = name.strip().lower()
    match = None
    for profile in profiles.values():
        if str(profile.get("name") or "").strip().lower() == wanted:
            match = profile
            break
    if match is None:
        return None, f"quality profile named {name!r} not found on this service"
    floor = profile_floor(match)
    if floor is not None and floor > ceiling:
        return None, (
            f"safe profile {name!r} has floor {floor}p, still above the "
            f"{ceiling}p ceiling for this title"
        )
    return match, ""


# ---------------------------------------------------------------------------
# Per-service check
# ---------------------------------------------------------------------------


async def _check_service(
    *,
    client: "ArrClient",
    config: ProfileSanityConfig,
    notifier: "Notifier",
    state: ProfileSanityState,
) -> ProfileSanityReport:
    service = client.name
    kind = "series" if service == "Sonarr" else "movie"
    report = ProfileSanityReport(service=service)
    now = datetime.now(timezone.utc)

    async def _error(detail: str) -> ProfileSanityReport:
        report.error = detail
        log.warning("profile_sanity[%s]: %s", service, detail)
        if state.note(f"{service}:error", True):
            await notifier.emit(
                "profile_sanity.error", {"service": service, "error": detail}
            )
        return report

    # --- Stack-level guard A: indexer outage (fail closed) ----------------
    outage, outage_reason = await indexer_outage(client)
    if outage:
        report.suppressed = outage_reason
        log.warning(
            "profile_sanity[%s]: indexer outage — %s. Flagging nothing this tick.",
            service,
            outage_reason,
        )
        if state.note(f"{service}:indexer_outage", True):
            await notifier.emit(
                "profile_sanity.indexer_outage",
                {"service": service, "reason": outage_reason},
            )
        return report
    state.note(f"{service}:indexer_outage", False)

    try:
        profiles = await _quality_profiles(client)
    except Exception as exc:
        return await _error(f"GET /qualityprofile failed: {exc}")
    report.profiles = len(profiles)

    try:
        items = await _library(client, kind)
    except Exception as exc:
        return await _error(f"library fetch failed: {exc}")
    report.scanned = len(items)

    try:
        queued = await _queue_item_ids(client, kind)
    except Exception as exc:
        # Gate 6 cannot be verified without the queue — fail closed.
        return await _error(f"queue fetch failed: {exc} — failing closed")

    state.note(f"{service}:error", False)

    # --- Gates 1-3 + 6 ----------------------------------------------------
    eligible: list[dict] = []
    for item in items:
        try:
            item_id = int(item.get("id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        ok, reason = hard_gates(kind, item, now=now, in_queue=item_id in queued)
        if ok:
            eligible.append(item)
        else:
            # Not flaggable → clear any prior dedup so it can alert again if
            # it ever becomes flaggable.
            state.note(f"{service}:{item_id}", False)
            log.debug(
                "profile_sanity[%s]: skip %s — %s",
                service,
                item.get("title"),
                reason,
            )
    report.eligible = len(eligible)

    # --- Gate 5: starvation, for every eligible item ----------------------
    # One history call per eligible item. Gate 2 (zero files) keeps that set
    # small on a healthy library — it is only the titles still waiting for
    # their first file.
    starved_map: dict[int, tuple[bool, bool, str]] = {}
    for item in eligible:
        item_id = int(item["id"])
        try:
            history = await _item_history(client, kind, item_id)
        except Exception as exc:
            # Cannot prove starvation → fail closed (treated as not starved).
            log.debug(
                "profile_sanity[%s]: history fetch failed for %s: %s",
                service,
                item.get("title"),
                exc,
            )
            starved_map[item_id] = (False, False, f"history unavailable ({exc})")
            continue
        starved_map[item_id] = starvation(
            item,
            history,
            now=now,
            min_starvation_age=config.min_starvation_age,
            severe_starvation_age=config.severe_starvation_age,
        )
    report.starved = sum(1 for v in starved_map.values() if v[0])

    # --- Stack-level guard B: library-wide starvation ratio ---------------
    if report.eligible >= config.min_library_size:
        ratio = report.starved / report.eligible
        if ratio > config.max_starved_ratio:
            report.suppressed = (
                f"{report.starved}/{report.eligible} eligible items starved "
                f"({ratio:.0%} > {config.max_starved_ratio:.0%})"
            )
            log.warning(
                "profile_sanity[%s]: outage suspected — %s. Flagging nothing.",
                service,
                report.suppressed,
            )
            if state.note(f"{service}:outage_suspected", True):
                await notifier.emit(
                    "profile_sanity.outage_suspected",
                    {
                        "service": service,
                        "starved": report.starved,
                        "eligible": report.eligible,
                        "ratio": f"{ratio:.0%}",
                    },
                )
            return report
    state.note(f"{service}:outage_suspected", False)

    # --- Gate 4 + confidence + action ------------------------------------
    for item in eligible:
        item_id = int(item["id"])
        title = str(item.get("title") or f"id {item_id}")
        dedup_key = f"{service}:{item_id}"

        profile_id = item.get("qualityProfileId")
        profile = profiles.get(int(profile_id)) if profile_id is not None else None
        floor = profile_floor(profile) if profile else None
        ceiling = era_ceiling(
            kind,
            year=item.get("year"),
            runtime=item.get("runtime"),
            in_cinemas=item.get("inCinemas"),
            sd_era_year=config.sd_era_year,
            tv_movie_runtime_max=config.tv_movie_runtime_max,
        )
        starved, severe, starve_detail = starved_map.get(
            item_id, (False, False, "not evaluated")
        )

        if floor is None or ceiling is None or floor <= ceiling:
            state.note(dedup_key, False)
            continue
        if not starved:
            # A starved-looking title with a satisfiable-looking profile, or a
            # title that HAS been grabbed before, is out of scope.
            state.note(dedup_key, False)
            continue

        score, signals = confidence_score(
            kind,
            item,
            floor=floor,
            ceiling=ceiling,
            starved=starved,
            severe=severe,
            sd_era_year=config.sd_era_year,
            tv_movie_runtime_max=config.tv_movie_runtime_max,
        )
        if score < config.alert_min_confidence:
            state.note(dedup_key, False)
            log.debug(
                "profile_sanity[%s]: %s scored %d < %d, not alerting",
                service,
                title,
                score,
                config.alert_min_confidence,
            )
            continue

        report.flagged += 1
        profile_name = str((profile or {}).get("name") or f"id {profile_id}")
        safe_profile, blocked = _resolve_safe_profile(
            profiles, config.safe_profile, ceiling
        )
        recommended = (
            str(safe_profile.get("name"))
            if safe_profile
            else f"could not resolve one — {blocked}"
        )
        why = (
            f"a {item.get('year')} "
            + ("series" if kind == "series" else "title")
            + f" has no source better than ~{ceiling}p, but every quality "
            f"allowed by {profile_name!r} is at least {floor}p"
        )
        if len(report.flags) < config.max_flags_per_tick:
            report.flags.append(
                {
                    "id": item_id,
                    "name": title,
                    "profile_id": profile_id,
                    "profile_name": profile_name,
                    "floor": floor,
                    "ceiling": ceiling,
                    "confidence": score,
                    "signals": signals,
                    "starvation": starve_detail,
                    "recommended": recommended,
                }
            )

        # Cap checked BEFORE state.note so a capped item keeps its dedup
        # counter and alerts on a later tick instead of being silently eaten.
        if report.alerted < config.max_flags_per_tick and state.note(dedup_key, True):
            report.alerted += 1
            log.warning(
                "profile_sanity[%s]: IMPOSSIBLE PROFILE — %s on %r: floor %dp > "
                "era ceiling %dp (confidence %d: %s). %s. Recommended: %s",
                service,
                title,
                profile_name,
                floor,
                ceiling,
                score,
                "; ".join(signals),
                starve_detail,
                recommended,
            )
            await notifier.emit(
                "profile_sanity.flagged",
                {
                    "service": service,
                    "name": title,
                    "profile_name": profile_name,
                    "profile_id": profile_id,
                    "floor": floor,
                    "ceiling": ceiling,
                    "why": why,
                    "starvation": starve_detail,
                    "confidence": score,
                    "recommended": recommended,
                },
            )

        # --- AUTO-HEAL (opt-in, strictly more conservative than alerting) --
        if not config.auto_heal:
            continue
        if score < config.heal_min_confidence:
            log.info(
                "profile_sanity[%s]: %s scored %d < heal threshold %d — alert only",
                service,
                title,
                score,
                config.heal_min_confidence,
            )
            continue
        if report.healed >= config.max_heals_per_tick:
            log.info(
                "profile_sanity[%s]: heal cap %d reached, skipping %s",
                service,
                config.max_heals_per_tick,
                title,
            )
            continue
        if safe_profile is None:
            log.warning(
                "profile_sanity[%s]: cannot heal %s — %s", service, title, blocked
            )
            continue

        # Re-assert gate 2 against a FRESH copy immediately before writing: a
        # file may have landed since the library snapshot, which would prove
        # the profile satisfiable and make the reassignment wrong.
        try:
            endpoint = "series" if kind == "series" else "movie"
            fresh = await _get_json(client, f"{endpoint}/{item_id}")
        except Exception as exc:
            log.warning(
                "profile_sanity[%s]: re-fetch before heal failed for %s: %s",
                service,
                title,
                exc,
            )
            continue
        ok, reason = hard_gates(kind, fresh, now=datetime.now(timezone.utc))
        if not ok:
            log.info(
                "profile_sanity[%s]: skipping heal of %s — %s", service, title, reason
            )
            continue

        new_profile_id = int(safe_profile["id"])
        try:
            await _reassign_profile(client, kind, fresh, new_profile_id)
        except Exception as exc:
            log.error(
                "profile_sanity[%s]: reassignment of %s failed: %s", service, title, exc
            )
            await notifier.emit(
                "profile_sanity.error",
                {"service": service, "error": f"heal of {title} failed: {exc}"},
            )
            continue

        report.healed += 1
        searched = ""
        if config.search_after_heal:
            try:
                await _trigger_search(client, kind, item_id)
                searched = " and triggered a search"
            except Exception as exc:
                log.warning(
                    "profile_sanity[%s]: search after heal failed for %s: %s",
                    service,
                    title,
                    exc,
                )
        log.warning(
            "profile_sanity[%s]: HEALED %s — quality profile %s (id %s) -> %s "
            "(id %d)%s",
            service,
            title,
            profile_name,
            profile_id,
            safe_profile.get("name"),
            new_profile_id,
            searched,
        )
        await notifier.emit(
            "profile_sanity.healed",
            {
                "service": service,
                "name": title,
                "old_profile_id": profile_id,
                "old_profile_name": profile_name,
                "new_profile_id": new_profile_id,
                "new_profile_name": str(safe_profile.get("name")),
                "searched": searched,
            },
        )
        # Healed → re-arm the dedup counter so a future relapse alerts again.
        state.note(dedup_key, False)

    return report


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


async def run_profile_sanity(
    *,
    arr_clients: list["ArrClient"],
    config: ProfileSanityConfig,
    notifier: "Notifier",
    state: ProfileSanityState | None = None,
    health_state: "HealthState | None" = None,
) -> list[ProfileSanityReport]:
    """Single-shot tick of the impossible-profile detector. Never raises."""
    if not config.enabled:
        return []
    if state is None:
        state = ProfileSanityState()
    state.debounce = config.debounce

    reports: list[ProfileSanityReport] = []
    for client in arr_clients:
        if getattr(client, "name", None) not in ("Sonarr", "Radarr"):
            continue
        try:
            report = await _check_service(
                client=client,
                config=config,
                notifier=notifier,
                state=state,
            )
        except Exception as exc:  # belt and braces — this module never crashes
            log.exception("profile_sanity[%s]: unexpected failure", client.name)
            report = ProfileSanityReport(service=client.name, error=str(exc))
            await notifier.emit(
                "profile_sanity.error",
                {"service": client.name, "error": str(exc)},
            )
        reports.append(report)
        log.info(
            "profile_sanity[%s]: scanned %d, eligible %d, starved %d, flagged %d, "
            "alerted %d, healed %d%s",
            report.service,
            report.scanned,
            report.eligible,
            report.starved,
            report.flagged,
            report.alerted,
            report.healed,
            f", SUPPRESSED ({report.suppressed})" if report.suppressed else "",
        )

    if health_state is not None and hasattr(health_state, "record_profile_sanity"):
        health_state.record_profile_sanity([r.to_dict() for r in reports])
    return reports
