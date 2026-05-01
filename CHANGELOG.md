# Changelog

## 0.5.3 — 2026-05-01

### Fixed
- **Startup chicken-and-egg with broken qBit.** If `qbit.login()` raised at
  startup (qBit container in restart-loop, gluetun namespace stale, etc.),
  docktarr crashed with exit 1 before the scheduler started — meaning
  `qbit_health`, the very module designed to recover qBit, never ran. Caught
  in production while deploying 0.5.2 over an existing Pattern 1 outage.
  Now wraps `qbit.login()` in try/except and logs a warning; `qbit_health`
  is wired up and probes/restarts qBit on its normal cadence regardless.

## 0.5.2 — 2026-05-01

### Fixed
- **arr_services: container recovery.** The arr_services job logged warnings
  when Sonarr/Radarr/Readarr/Bookshelf were unreachable but never took
  corrective action. Three containers (bookshelf, readarr-audiobooks,
  audiobookshelf) had been dead for 4 / 4 / 2 days under a transient NFS
  mount failure that long since resolved itself; docktarr never restarted
  them. Same gap-class as the qbit_health "running but unreachable" gap
  fixed in 0.5.1 — same shape of fix.

  - Container exited (any code) → restart, emit `arr.restarted`.
  - Container running but API unreachable for N consecutive ticks
    (default 3 ≈ 15 min, higher than qbit_health's 2 because arr apps
    have legitimately slow startups for db upgrades / library scans) →
    restart, emit `arr.unreachable_threshold_restart`.
  - Per-service restart cooldown (default 15 min) prevents hammering
    docker when restarts keep failing because of an underlying
    infrastructure issue (e.g. a dead NFS volume).
  - Container not found / docker error during restart → log + emit
    `arr.restart_failed`. No crash.

### Added
- `ArrAppConfig.container_name` (optional) and `effective_container_name`
  property. Defaults to `name.lower()`; override per-service via env vars
  `SONARR_CONTAINER`, `RADARR_CONTAINER`, `READARR_CONTAINER`,
  `BOOKSHELF_CONTAINER`. The Readarr instance commonly runs as
  `readarr-audiobooks` so override is required there.
- `ArrClient.container_name` exposed for external use.
- `ArrServicesState` dataclass tracking per-service `consecutive_unreachable`
  counter and `last_restart_attempt` timestamp across scheduler ticks.
- `/health/arr_services` endpoint and `arr_services` field on the main
  `/health` snapshot. Per-service: `name`, `url`, `status`, `http_status`,
  `error`, `container_name`, `container_status`, `last_action`.
- New env vars: `ARR_UNREACHABLE_THRESHOLD` (default `3`),
  `ARR_RESTART_COOLDOWN` (default `15m`), `<SERVICE>_CONTAINER` per service.
- Notifier events: `arr.restarted`, `arr.unreachable_threshold_restart`,
  `arr.restart_failed` (alongside existing `service.down`).

### Changed
- `run_arr_services` accepts new keyword-only args (`docker_manager`,
  `state`, `health_state`, `running_unreachable_threshold`,
  `restart_cooldown`); the legacy 2-arg form continues to work with
  log+notify behavior only (no recovery).

## 0.5.1 — 2026-04-30

### Fixed
- **qbit_health: stale gluetun namespace recovery.** When gluetun restarted (e.g.
  Watchtower image update) but qBit's container kept running, qBit's network
  namespace would silently break — its API became unreachable while Docker still
  reported the container healthy. The previous logic explicitly returned with no
  action ("may be mid-startup — will re-check next tick") so the arr stack
  stayed broken until manual intervention. qbit_health now compares
  `gluetun.started_at` vs `qbittorrent.started_at`; if the VPN is newer, the
  namespace is stale and qBit is restarted with a `qbit.stale_namespace_restart`
  event.
- **qbit_health: consecutive-unreachable threshold.** Safety net for cases the
  namespace comparison can't decide (VPN container missing, missing timestamps,
  clock skew). After N consecutive "running but unreachable" ticks (default 2 →
  ~10 min at the default 5-min interval), qbit_health restarts qBit with a
  `qbit.unreachable_threshold_restart` event. Tunable via
  `QBIT_HEALTH_UNREACHABLE_THRESHOLD`.

### Added
- `ContainerInfo.started_at: datetime | None` parsed from Docker
  `State.StartedAt`.
- `QbitHealthState` dataclass — per-instance counter persisted across scheduler
  ticks.
- `QbitHealthConfig.vpn_container_name` (default `"gluetun"`) and
  `running_unreachable_threshold` (default `2`).
- `/health/qbit` endpoint and `qbit_health` field on the main `/health`
  snapshot. Surfaces last-tick reachability, container status, both
  `started_at` timestamps, whether stale-namespace was detected, the
  consecutive-unreachable counter, and the last action taken.
- New env vars: `QBIT_VPN_CONTAINER`, `QBIT_HEALTH_UNREACHABLE_THRESHOLD`.
- Notifier events: `qbit.stale_namespace_restart`,
  `qbit.unreachable_threshold_restart`.

### Changed
- Startup banner now logs the actual installed version
  (`importlib.metadata.version("docktarr")`) instead of the hardcoded
  `Doctarr v0.2.0 starting` string.

## 0.5.0 — 2026-04-30

### Renamed
- Project renamed from `doctarr` to `docktarr`. The pun was always there.
- Python package: `doctarr` → `docktarr`
- Docker image: `ghcr.io/codewarrior4life/doctarr` → `ghcr.io/codewarrior4life/docktarr`
- GitHub repo: `CodeWarrior4Life/doctarr` → `CodeWarrior4Life/docktarr`
- Default config path: `/config/doctarr.yaml` → `/config/docktarr.yaml`

### Migration
- Anyone importing `doctarr` must update imports to `docktarr`.
- Anyone running the old image must point at `ghcr.io/codewarrior4life/docktarr:latest` or pin to `doctarr:0.4.1` (the last release under the old name).
- Anyone with a clone needs `git remote set-url origin git@github.com:CodeWarrior4Life/docktarr.git` (GitHub redirects but updating origin is cleaner).

### No behavior changes
- Pure rename. `0.4.1 → 0.5.0` reflects the breaking nature of the package name change. No code paths, no defaults, no scheduling, no module behavior changed.

## 0.4.1 (2026-04-21)

### imposter_detector — new heuristic + backfill mode
- **Network/source sanity check**: streaming-only networks (Netflix, Apple TV+, Disney+, Prime Video, Hulu, Paramount+, HBO Max/Max, Peacock, Crunchyroll) flagged when the Sonarr quality source is broadcast (`television` / `televisionRaw`). Netflix originals cannot come from OTA broadcast — this catches imposters whose runtime is superficially plausible.
- **Backfill mode** (`IMPOSTER_BACKFILL_ENABLED=true`, default on, weekly): walks every monitored series and re-evaluates every episode file. Catches imposters imported before a heuristic existed. Recent-history scan continues hourly.
- Episode fetch now includes `series` (network) via `?includeSeries=true`.
- Notifier payload now includes `reason`, `quality_source`, `network`.
- Real-world case: `Dark (Netflix) S01E02 "Lies"` — HDTV-1080p source, 42m vs 44m ep.runtime (4.5% off, invisible to runtime heuristic), caught by source/network check.

## Safety (0.4.0 hardening commit)
- `permissions_health`: hardlink-aware chown — files with `nlink > 1` are skipped to prevent incidentally mutating inodes referenced by MAM/qBit torrent files. Documented MAM compliance in README.

---

## 0.4.0 (2026-04-19)

### New Modules
- **hw_capability**: Per-host GPU/accelerator detection via SSH (Intel QuickSync, NVIDIA NVENC, AMD VCN).
- **media_container_audit**: Verifies media containers (Plex today; Tdarr/Jellyfin/Emby later) have HW passthrough + provider-specific prefs.
- **permissions_health**: Scans Plex library paths for ownership/mode drift; optional auto-fix with rate limits and Plex-refresh trigger.

### Consolidation
- Folded arr-orchestrator into doctarr as a set of jobs: `qbit_health`, `vpn_health`, `disk_health`, `arr_services`. Single package, single deployment.

### Infrastructure
- New: async SSH client (`asyncssh`), YAML config layer, async Docker wrapper, `/health` HTTP endpoint on port 8080.
- Extended notifier events: `hw.*`, `perms.*`, `qbit.*`, `vpn.*`, `disk.*`, `service.*`.

### Breaking
- None. Existing env-var config continues to work; YAML config is additive.

### Migration
- `scripts/migrate_orchestrator_config.py` converts orchestrator `config.yaml` → `doctarr.yaml` + `.env`. See README.

---

## 0.3.0

Initial public release. Autonomous Prowlarr indexer lifecycle management: discover, test, add, monitor, prune, re-discover.
