# Docktarr

Autonomous indexer manager for [Prowlarr](https://prowlarr.com). Discovers, tests, and maintains public torrent indexers so you don't have to.

## What It Does

Prowlarr ships with 200+ public torrent indexer definitions, but you have to manually add, test, and clean up each one. Docktarr automates the entire lifecycle:

1. **Discovers** all public indexers from Prowlarr's built-in catalog
2. **Tests** each one to verify it's actually working
3. **Adds** working indexers to your Prowlarr instance
4. **Monitors** health with periodic checks
5. **Prunes** indexers that stay broken for 12+ hours
6. **Re-discovers** previously pruned indexers if they come back online

Zero configuration beyond your Prowlarr URL and API key. Set it and forget it.

## Quick Start

Add to your existing docker-compose stack:

```yaml
docktarr:
  image: ghcr.io/codewarrior4life/docktarr:latest
  container_name: docktarr
  environment:
    - PROWLARR_URL=http://prowlarr:9696
    - PROWLARR_API_KEY=your-api-key
    - TZ=America/New_York
  volumes:
    - ./config/docktarr:/config
  restart: unless-stopped
```

That's it. Docktarr will start discovering and testing indexers immediately.

## How It Works

Docktarr runs four independent jobs:

| Job | Default Interval | What It Does |
|-----|-----------------|--------------|
| Discovery | 6 hours | Scans Prowlarr schema for new public indexers |
| Tester | 2 hours | Health-checks all managed indexers |
| Pruner | 1 hour | Removes indexers broken for 12+ hours |
| Digest | Daily 8:00 AM | Sends summary via webhook |

### Safety

- **Private trackers are never touched.** Docktarr only manages indexers it creates, identified by a `docktarr` tag in Prowlarr.
- **User changes are respected.** Remove the tag and Docktarr stops managing that indexer.
- **Graceful recovery.** If state is lost, Docktarr rebuilds from Prowlarr.

## Configuration

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PROWLARR_URL` | Yes | - | Prowlarr base URL |
| `PROWLARR_API_KEY` | Yes | - | Prowlarr API key |
| `DISCOVERY_INTERVAL` | No | `6h` | How often to scan for new indexers |
| `TEST_INTERVAL` | No | `2h` | How often to health-check indexers |
| `PRUNE_INTERVAL` | No | `1h` | How often to check for removal |
| `PRUNE_THRESHOLD` | No | `12h` | How long broken before pruning |
| `TEST_DELAY` | No | `2s` | Delay between test calls |
| `WEBHOOK_URL` | No | - | Discord/generic webhook URL |
| `WEBHOOK_EVENTS` | No | `added,pruned,digest` | Events to notify on |
| `DIGEST_TIME` | No | `08:00` | Daily digest time (24h, local TZ) |
| `LOG_LEVEL` | No | `info` | debug, info, warning, error |
| `TZ` | No | `UTC` | Timezone |
| `ZION_SUDO_USER` | No | - | Username for SSH to Zion |
| `ZION_SUDO_PASSWORD` | No | - | Password for SSH to Zion |
| `MEGACITY_SUDO_USER` | No | - | Username for SSH to Megacity |
| `MEGACITY_SUDO_PASSWORD` | No | - | Password for SSH to Megacity |
| `QBITTORRENT_URL` | No | - | qBittorrent Web UI URL |
| `QBITTORRENT_USERNAME` | No | - | qBittorrent username |
| `QBITTORRENT_PASSWORD` | No | - | qBittorrent password |
| `QBITTORRENT_CONTAINER` | No | `qbittorrent` | Container name for restart |
| `VPN_HEALTHCHECK_URL` | No | - | URL returning 200 when VPN is healthy |
| `DISK_HEALTH_PATHS` | No | - | Comma-separated paths to check free space |
| `PLEX_URL` | No | - | Plex base URL (e.g. `http://plex:32400`) |
| `PLEX_TOKEN` | No | - | Plex auth token |
| `QBIT_HEALTH_INTERVAL` | No | `5m` | qBittorrent health check interval |
| `VPN_HEALTH_INTERVAL` | No | `5m` | VPN health check interval |
| `DISK_HEALTH_INTERVAL` | No | `15m` | Disk free-space check interval |
| `ARR_SERVICES_INTERVAL` | No | `5m` | *arr service liveness check interval |
| `HW_CAPABILITY_INTERVAL` | No | `24h` | Hardware capability scan interval |
| `MEDIA_AUDIT_INTERVAL` | No | `12h` | Media container audit interval |
| `PERMS_HEALTH_INTERVAL` | No | `6h` | Permissions health scan interval |
| `DOCKTARR_HOST_NAME` | No | - | Override detected hostname for SSH routing |
| `DOCKTARR_SKIP_NETWORK_INIT` | No | `false` | Skip SSH connectivity check on startup |
| `ARTWORK_HEALTH_ENABLED` | No | `false` | Master switch for the artwork-health watchdog |
| `ARTWORK_HEALTH_INTERVAL` | No | `6h` | Artwork-health tick interval |
| `ARTWORK_HEALTH_AUTO_HEAL` | No | `true` | Re-enable a disabled Kodi (XBMC) / Emby metadata consumer |
| `ARTWORK_HEALTH_CHECK_IMAGE_FIELDS` | No | `false` | Also alert if the consumer's image sub-toggles are off |
| `ARTWORK_HEALTH_PRESENCE_CHECK` | No | `true` | Spot-check recent items for poster/fanart on disk (needs Docker) |
| `ARTWORK_HEALTH_PRESENCE_SAMPLE_SIZE` | No | `10` | How many most-recently-added items to spot-check per arr |
| `ARTWORK_HEALTH_PRESENCE_AUTO_REFRESH` | No | `false` | Trigger RefreshSeries/RefreshMovie for items missing artwork |
| `ARTWORK_HEALTH_DEBOUNCE` | No | `1` | Consecutive breaching ticks before an alert fires (then deduped) |
| `MEDIA_QA_ENABLED` | No | `false` | Master switch for post-import dud-file detection |
| `MEDIA_QA_INTERVAL` | No | `1h` | Media QA tick interval (recent-imports scan) |
| `MEDIA_QA_LOOKBACK` | No | `24h` | Import-history window scanned per tick |
| `MEDIA_QA_BACKFILL_ENABLED` | No | `false` | Also probe the entire monitored library on a slow cadence |
| `MEDIA_QA_BACKFILL_INTERVAL` | No | `7d` | Full-library backfill interval |
| `MEDIA_QA_AUTO_REMEDIATE` | No | `false` | Delete flagged files + trigger re-search (alert-only when false) |
| `MEDIA_QA_TRUNCATION_RATIO` | No | `0.25` | Flag video shorter than this fraction of the arr-reported runtime |
| `MEDIA_QA_FFPROBE_PATH` | No | auto | Explicit ffprobe path inside the probed container |
| `MEDIA_QA_FFPROBE_CONTAINER` | No | the arr container | Alternate container to run ffprobe in (must share the arr's media mounts) |
| `PROFILE_SANITY_ENABLED` | No | `false` | Master switch for impossible-quality-profile detection |
| `PROFILE_SANITY_INTERVAL` | No | `6h` | Profile-sanity tick interval |
| `PROFILE_SANITY_AUTO_HEAL` | No | `false` | Reassign flagged items to the safe profile (alert-only when false) |
| `PROFILE_SANITY_SAFE_PROFILE` | No | `Any` | Quality profile (BY NAME) a flagged item is reassigned to |
| `PROFILE_SANITY_SEARCH_AFTER_HEAL` | No | `false` | Trigger SeriesSearch/MoviesSearch after a reassignment |
| `PROFILE_SANITY_MIN_STARVATION_AGE` | No | `3d` | Minimum age since `added` before an item can be called starved |
| `PROFILE_SANITY_SEVERE_STARVATION_AGE` | No | `14d` | Age at which starvation adds an extra confidence point |
| `PROFILE_SANITY_SD_ERA_YEAR` | No | `1998` | Titles older than this are treated as pre-HD |
| `PROFILE_SANITY_TV_MOVIE_RUNTIME_MAX` | No | `100` | Runtime (min) below which a pre-HD movie looks TV-sourced |
| `PROFILE_SANITY_ALERT_MIN_CONFIDENCE` | No | `3` | Confidence score required to alert |
| `PROFILE_SANITY_HEAL_MIN_CONFIDENCE` | No | `5` | Confidence score required to auto-heal (always ≥ the alert score) |
| `PROFILE_SANITY_MAX_STARVED_RATIO` | No | `0.5` | Suppress the whole service above this starved/eligible ratio |
| `PROFILE_SANITY_MIN_LIBRARY_SIZE` | No | `20` | Eligible items needed before the starved-ratio guard applies |
| `PROFILE_SANITY_MAX_HEALS_PER_TICK` | No | `5` | Blast-radius cap on reassignments per tick |
| `PROFILE_SANITY_MAX_FLAGS_PER_TICK` | No | `10` | Alert-volume cap per tick |
| `PROFILE_SANITY_DEBOUNCE` | No | `1` | Consecutive breaching ticks before an alert fires (then deduped) |

## Webhook Events

| Event | When |
|-------|------|
| `indexer.added` | New indexer passes test and is enabled |
| `indexer.pruned` | Broken indexer removed after threshold |
| `indexer.degraded` | Working indexer starts failing |
| `prowlarr.unreachable` | Can't reach Prowlarr for 3+ cycles |
| `health.digest` | Daily summary |

## Hardware Capability Detection

Docktarr SSH-connects to each configured host and detects available hardware accelerators (Intel QuickSync, NVIDIA NVENC, AMD VCN). Results feed the media container audit.

Configure hosts in `docktarr.yaml`:

```yaml
hosts:
  zion:
    ssh_ref: zion           # resolves ZION_SUDO_USER / ZION_SUDO_PASSWORD from env
    roles: [plex, tdarr]
  megacity:
    ssh_ref: megacity
    roles: [tdarr]
```

The `/health` endpoint (port 8080) exposes the latest capability snapshot at `GET /health`.

## Media Container Audit

Verifies that media containers (Plex, with Tdarr/Jellyfin/Emby planned) have hardware passthrough configured correctly and host-specific prefs applied. Runs every 12 hours by default (`MEDIA_AUDIT_INTERVAL`).

Audit results appear in the `/health` response and trigger `hw.*` webhook events on state changes.

## Permissions Health

Scans Plex library paths for ownership/mode drift. Optionally auto-fixes with configurable rate limits and triggers a Plex library refresh afterward.

```yaml
permissions_health:
  paths:
    - /media/movies
    - /media/tv
  expected_uid: 1000
  expected_gid: 1000
  expected_mode: "0755"
  auto_fix: false          # set true to enable repair
  fix_rate_limit: 500      # max files fixed per run
```

Events emitted: `perms.drift_detected`, `perms.fix_applied`, `perms.fix_failed`.

### MAM Compliance

**Never point `paths:` at raw download directories** (e.g. `/data/Downloads`). ARR apps hardlink imported files — the library-side file and the download-side file share an inode. Chowning the library file changes the inode owner for both, which can prevent qBittorrent from reading the torrent data and stops seeding. On private trackers like MyAnonaMouse (MAM), losing seed time below ratio thresholds triggers account consequences.

Docktarr enforces two safeguards automatically:

1. **Hardlink skip**: any file with `nlink > 1` (i.e. referenced from more than one path) is silently skipped during `auto_fix`. A `perms.skipped_hardlinks` webhook event fires with a count and sample paths so you can investigate.
2. **Downloads-path warning**: if a configured path contains `downloads` or `mam` (case-insensitive), Docktarr logs a WARNING at scan time reminding you to set `auto_fix: false`.

## Artwork Health

Keeps Plex/Jellyfin/Kodi artwork intact by guarding the Sonarr/Radarr metadata
pipeline. Off by default — set `ARTWORK_HEALTH_ENABLED=true` to activate.

Born from the 2026-07-15 blank-artwork incident: Sonarr's **"Kodi (XBMC) /
Emby"** metadata consumer (`XbmcMetadata`) had been disabled, so no
`poster.jpg` / `fanart.jpg` / season art / `*-thumb.jpg` / `.nfo` sidecars
were ever written to disk and every downstream player showed blank posters.

Two responsibilities per tick (default every `ARTWORK_HEALTH_INTERVAL` = 6h):

1. **Consumer-drift guard (root-cause fix).** GETs `/api/v3/metadata` on each
   configured Sonarr/Radarr and finds the `XbmcMetadata` consumer. If it is
   disabled it emits `artwork_health.consumer_disabled` and — when
   `ARTWORK_HEALTH_AUTO_HEAL` is on (default) — GETs the full consumer object,
   flips `enable=true`, PUTs it back (preserving every other field), and emits
   `artwork_health.consumer_reenabled`. With `ARTWORK_HEALTH_CHECK_IMAGE_FIELDS`
   it additionally alerts (`artwork_health.consumer_images_disabled`) when the
   per-image toggles (`seriesImages`/`seasonImages`/`episodeImages` for Sonarr,
   `movieImages` for Radarr) are off. Re-enabling is safe and reversible, which
   is why auto-heal defaults on.
2. **Artwork presence spot-check (alert-only).** For the
   `ARTWORK_HEALTH_PRESENCE_SAMPLE_SIZE` most-recently-added items it reads each
   item's `path` from the arr API and `docker exec`s into the arr container
   (the same mechanism `mount_audit` uses — no host mount assumed) to confirm
   `poster.jpg`/`fanart.jpg` exist on disk, emitting `artwork_health.artwork_missing`
   with a count + sample. No auto-heal for presence in v1; set
   `ARTWORK_HEALTH_PRESENCE_AUTO_REFRESH=true` to also trigger a
   `RefreshSeries`/`RefreshMovie` for the missing items. Requires Docker access;
   degrades to skipped (consumer guard still runs) when unavailable.

Alerts are deduped (`ARTWORK_HEALTH_DEBOUNCE`): one alert per incident, re-armed
only after the condition clears. State is surfaced at `GET /health` (and
`GET /health/artwork_health`) under the `artwork_health` key. Requires Sonarr
and/or Radarr to be configured (`SONARR_URL`/`SONARR_API_KEY`, etc.).

## Media QA

Catches "dud" video files that import cleanly but don't actually play. Off by
default — set `MEDIA_QA_ENABLED=true` to activate.

Born from the 2026-07-26 incident: a 2160p episode imported fine and was valid
HEVC Main10 PQ/BT.2020 video, but carried **zero HDR metadata** — no mastering
display color volume, no content light level, no Dolby Vision config, no
HDR10+. Players that tone-map (Plex Desktop, mpv) assume a 10,000-nit peak for
metadata-less PQ and render the picture near-black: "audio but no picture".

Each tick (default `MEDIA_QA_INTERVAL` = 1h) it pulls the
`downloadFolderImported` history from Sonarr/Radarr within `MEDIA_QA_LOOKBACK`
and probes each new file with ffprobe (JSON output), applying three rules:

1. **`dovi_p5_no_fallback`** — the video stream carries a DOVI configuration
   record with `dv_profile == 5` (single-layer, no HDR10 base layer):
   unplayable ("color space not supported") on non-DV clients.
2. **`pq_missing_hdr_metadata`** — `color_transfer == smpte2084` (PQ) with no
   mastering-display, content-light-level, Dolby Vision, or HDR10+ metadata
   (first-frame side data via `-show_frames -read_intervals "%+#1"`).
3. **`missing_or_truncated_video`** — no real video stream at all, or video
   duration below `MEDIA_QA_TRUNCATION_RATIO` (default 25%) of the
   arr-reported runtime.

ffprobe runs via `docker exec` into the arr container itself (the same
mechanism the artwork presence spot-check uses — no host mount assumed): the
arr container has the media mounted at exactly the paths its API reports, and
Sonarr v4 / Radarr v4+ bundle ffprobe. The binary is auto-discovered and
cached; override with `MEDIA_QA_FFPROBE_PATH` / `MEDIA_QA_FFPROBE_CONTAINER`.

Detection is **alert-only by default**: a `media_qa.flagged` event (Telegram +
webhook) names the series/movie, the flag reason, and the file path. Set
`MEDIA_QA_AUTO_REMEDIATE=true` to opt in to the imposter-detector remediation
path — delete the file via the arr API and trigger an
`EpisodeSearch`/`MoviesSearch` (`media_qa.remediated`). Probe *failures* are
never treated as duds (a stale mount must not mass-flag the library); they
emit a deduped `media_qa.error` instead. Verdicts are cached per (path, size)
so each file is probed and alerted once. `MEDIA_QA_BACKFILL_ENABLED=true` adds
a full-library scan every `MEDIA_QA_BACKFILL_INTERVAL` (default 7d) to
retro-catch duds imported before the module existed. State is surfaced at
`GET /health` (and `GET /health/media_qa`) under the `media_qa` key.

## Profile Sanity

Catches items assigned a quality profile they can **never** satisfy. Off by
default — set `PROFILE_SANITY_ENABLED=true` to activate.

Born from the 2026-07-27 incident: three Seerr requests read **Failed** with
zero errors anywhere — no failed download, no import error, a green
`/api/v3/health`, live indexers. Each item had simply been given a profile
nothing could ever fill, so Sonarr/Radarr searched forever and nothing errored:

- Sonarr series 678 **Diagnosis: Murder** (1993, ended, SD-only masters) sat on
  profile 5 "Ultra-HD" → 0 of 184 episodes, forever.
- Radarr movie 1127 **Charlie's Angels** (1976, a 74-minute TV pilot that was
  never in cinemas) sat on profile 7 "UHD 4k Remux" → no file, forever.

Nothing in the *arr stack surfaces this class of failure: from the app's point
of view an unsatisfiable profile is indistinguishable from "the release just
hasn't been posted yet". Each tick (default `PROFILE_SANITY_INTERVAL` = 6h) the
module automates the manual diagnosis — compare a title's era and plausible
sources against the *floor* of its quality profile.

**Profile floor.** `/api/v3/qualityprofile` items are either flat
(`{"quality": {...}, "items": [], "allowed": bool}`) or a group
(`{"name": "WEB 720p", "items": [...], "allowed": bool}`); a quality counts as
allowed when its own or its parent group's `allowed` is true. The floor is the
minimum resolution over allowed qualities, with `resolution: 0` (Radarr DVD,
WORKPRINT) resolved through a name table and otherwise excluded. Measured
2026-07-27: Sonarr `Any`=480, `SD`=480, `HD-720p`=720, `Ultra-HD`=**720**;
Radarr `Any`=480, `UHD 4k Remux`=720, `FHD Remux`=720. Note Ultra-HD's floor is
720, *not* 2160 — the module never assumes a profile's headline cutoff is its
floor.

**Era ceiling** — the best source that plausibly exists for the title, and the
core false-positive defence. Pre-`PROFILE_SANITY_SD_ERA_YEAR` (1998) *series*
were mastered to videotape/SD, so they cap at 576. For *movies* year alone is
deliberately not enough: pre-HD theatrical films are routinely remastered from
the negative, so the SD cap applies only when the title looks TV-sourced (no
`inCinemas` date, or a runtime below `PROFILE_SANITY_TV_MOVIE_RUNTIME_MAX`) —
exactly Charlie's Angels. A pre-HD film *with* a theatrical date and a feature
runtime gets 1080 instead, so a 720 floor stays satisfiable and it is correctly
not flagged. Modern titles get no ceiling at all and can never be flagged.

**Hard gates** (all must pass): monitored; **zero files** (any file proves the
profile is satisfiable); **released/airable** — the unreleased-title exclusion
(Radarr 1128 "Spider-Man: Brand New Day", `announced`, `isAvailable` false,
`inCinemas` in the future, has no file for an entirely healthy reason);
floor > ceiling (without that contradiction there is no impossible profile);
**starvation** — older than `PROFILE_SANITY_MIN_STARVATION_AGE` (3d) with no
`grabbed` and no `downloadFolderImported` event in its per-item history (a
single grab proves the profile IS satisfiable and the fault was transient); and
not currently in the download queue.

**Confidence score** gates alert vs heal: contradiction +2, starved +2 (+1 past
`PROFILE_SANITY_SEVERE_STARVATION_AGE`), pre-HD era +2, TV-pilot-shaped runtime
+1. Alerts at `PROFILE_SANITY_ALERT_MIN_CONFIDENCE` (3), heals only at
`PROFILE_SANITY_HEAL_MIN_CONFIDENCE` (5).

**Outage guards.** A stack-wide search outage looks exactly like starvation, so
two fail-closed guards run before anything is flagged. `profile_sanity.indexer_outage`
fires and the service is skipped entirely when no indexer has Automatic Search
enabled, when `/api/v3/health` reports "all indexers are unavailable" / "no
indexers available", when every auto-search indexer is named in an
`Indexer*Check` failure message, or when either API call *fails*
(`/api/v3/indexerstatus` is not used — it 404s on Sonarr v4).
`profile_sanity.outage_suspected` fires and the service is skipped when more
than `PROFILE_SANITY_MAX_STARVED_RATIO` (50%) of at least
`PROFILE_SANITY_MIN_LIBRARY_SIZE` (20) eligible items are starved — an outage
the health endpoint didn't surface, or a brand-new library.

Detection is **alert-only by default**: `profile_sanity.flagged` names the item,
its current profile, why the profile is impossible (floor vs ceiling in plain
words) and the recommended profile. `PROFILE_SANITY_AUTO_HEAL=true` opts in to
reassigning the item — resolved BY NAME from `PROFILE_SANITY_SAFE_PROFILE`
(default `Any`), skipped entirely if that name is missing or its own floor still
exceeds the ceiling — with the zero-files gate re-asserted against a freshly
fetched copy immediately before the write, the full item object PUT with only
`qualityProfileId` mutated, and before/after ids + names emitted in
`profile_sanity.healed` so every change is trivially reversible. A **global**
quality profile is never modified. `PROFILE_SANITY_SEARCH_AFTER_HEAL=true` also
triggers a `SeriesSearch`/`MoviesSearch`. `PROFILE_SANITY_MAX_HEALS_PER_TICK`
(5) caps the blast radius and `PROFILE_SANITY_MAX_FLAGS_PER_TICK` (10) caps
alert volume; alerts are deduped per item (`PROFILE_SANITY_DEBOUNCE`). State is
surfaced at `GET /health` (and `GET /health/profile_sanity`) under the
`profile_sanity` key.

## Consolidating arr-orchestrator

Docktarr 0.4 folds the arr-orchestrator jobs into the same process. No separate deployment needed:

| Job | Interval env var | What it checks |
|-----|-----------------|----------------|
| `qbit_health` | `QBIT_HEALTH_INTERVAL` | qBittorrent reachability; restarts container if stuck |
| `vpn_health` | `VPN_HEALTH_INTERVAL` | VPN tunnel via `VPN_HEALTHCHECK_URL` |
| `disk_health` | `DISK_HEALTH_INTERVAL` | Free space on `DISK_HEALTH_PATHS` |
| `arr_services` | `ARR_SERVICES_INTERVAL` | Liveness of configured *arr service URLs |

All jobs are opt-in: set the relevant env vars and they activate. Leave them unset and Docktarr behaves exactly as 0.3.

A migration script for existing orchestrator configs is at `scripts/migrate_orchestrator_config.py`.

## Development

```bash
git clone https://github.com/CodeWarrior4Life/docktarr.git
cd docktarr
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest -v
```

## License

MIT
