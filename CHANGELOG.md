# Changelog

## 0.7.2 — 2026-05-14

### Liveness-first design

0.7.1 shipped a queue-depth signal. That reads the *symptom* (50+ stale
``unspecified`` commands), not the *disease* (a wedged Sonarr scheduler).
By the time queue depth crosses 50, the scheduler has already missed
several RssSync / ImportListSync cycles — operators see the alert after
the damage is underway.

0.7.2 inverts the primary signal: a wedged scheduled task is the trigger,
not its downstream effect.

### Added
- **`arr_scheduler_health` module — liveness probe as the PRIMARY wedge
  signal.** Polls Sonarr/Radarr ``GET /api/v3/system/task`` every tick
  and computes ``overdue_ratio = age_seconds / (interval_minutes * 60)``
  for each task. Critical short-interval tasks (``Rss Sync``,
  ``Import List Sync``, ``Refresh Monitored Downloads``,
  ``Messaging Cleanup``) trip ``is_wedged`` when their overdue ratio
  exceeds ``wedge_threshold`` (default 3.0×). Long-interval tasks
  (``Backup``, ``Refresh Series``) are filtered out by allowlist — their
  hour-to-day intervals make overdue ratios meaningless off-cycle. When
  any critical task is wedged, the probe (a) emits
  ``arr_scheduler.wedged``, (b) forces an immediate command-queue drain
  via the new ``force_drain_services`` parameter, regardless of the
  count/age gate. Composed with ``arr_command_queue`` on the same poll
  tick so both signals correlate.
- **Burst detector in ``arr_command_queue``.** Tracks per-service
  drain-candidate count across ticks. If delta ≥ ``burst_threshold``
  (default 20) AND delta-rate ≥ ``burst_rate_threshold`` (default
  0.5/sec, i.e. >30/min), drain immediately regardless of age threshold.
  Rationale: 891 commands in 24s = 37/sec — unmistakable, age gate is
  too slow. Cold-start tick (no prior baseline) explicitly suppresses
  burst detection — first observation isn't a "delta from zero".
- **StateStore persistence for burst baseline.** Previous-tick counts +
  timestamps are written to ``/config/state.json`` under a new
  ``arr_burst`` envelope so a docktarr restart mid-burst doesn't reset
  the rolling baseline. Legacy flat-shape state files are still loaded
  for backward compatibility — the loader auto-detects the envelope via
  an ``"indexers"`` key.
- New events: ``arr_command_queue.burst_detected`` (warn, includes
  ``delta`` + ``delta_rate``), ``arr_scheduler.wedged`` (warn, lists
  offending tasks + overdue ratios), ``arr_scheduler.error`` (per-service
  probe failure).
- New endpoint: ``GET /health/arr_scheduler`` — per-service liveness
  reports (tasks list, wedged_count, last_action, error).
- ``ArrClient.list_scheduled_tasks()`` — thin wrapper around
  ``GET /api/v{n}/system/task``.

### Changed
- **Tighter defaults to catch bursts inside a single Sonarr task cycle
  (5 min minimum).** ``poll_interval_seconds`` 60 → 15;
  ``drain_threshold_count`` 50 → 30; ``drain_age_seconds`` 600 → 120.
  Combined with the burst detector and liveness probe, the wedge
  pattern that ran 15 hours in the 2026-05-13 incident would now be
  detected within ~5 min of onset (or immediately if the burst rate
  trips), and fully drained within the next poll cycle.
- New env overrides:
  ``DOCKTARR_ARR_COMMAND_QUEUE_BURST_THRESHOLD``,
  ``DOCKTARR_ARR_COMMAND_QUEUE_BURST_RATE_THRESHOLD``,
  ``DOCKTARR_ARR_SCHEDULER_HEALTH_ENABLED``,
  ``DOCKTARR_ARR_SCHEDULER_WEDGE_THRESHOLD``,
  ``DOCKTARR_ARR_SCHEDULER_CRITICAL_TASKS``.
- Default ``WEBHOOK_EVENTS`` expanded to include the three new events.

### Notes
- The two probes intentionally share the ``arr_command_queue:`` YAML
  block — they're conceptually one feature ("keep the ARR scheduler
  healthy") with two signals, and a single block keeps the operational
  surface unified.
- ``run_arr_command_queue`` is now called once per tick by a small
  wrapper in ``main.py`` that runs the liveness probe first, harvests
  ``force_drain_services``, and passes it (plus the ``StateStore``) into
  the queue probe. ``state.save()`` is called after each tick so the
  burst baseline survives container restarts.

## 0.7.1 — 2026-05-14

### Added
- **`arr_command_queue` module — drains runaway Sonarr/Radarr command
  batches.** Watches `/api/v3/command` and auto-cancels stale
  `trigger=unspecified` `EpisodeSearch` / `SeasonSearch` / `MovieSearch`
  bursts before they wedge the scheduler. Discriminator preserves
  `trigger=manual` (UI clicks) and `trigger=scheduled` (Sonarr's own
  heartbeats); only the API-posted "no trigger field" pattern is drained.
  Dual safeguards: drain only fires when ≥50 candidates AND oldest is ≥10
  min old (both tunable). Driven by the 2026-05-13 incident where 891
  unspecified-trigger `EpisodeSearch` commands queued in 24s blocked
  `RssSync` + `ImportListSync` for 15 hours until manual `xargs -P 16 curl
  -X DELETE` cleared them.
- New events: `arr_command_queue.drained` (info, includes count + 3
  sample ids), `arr_command_queue.elevated` (warn, queue > 200 but
  thresholds not met — investigate the source), `arr_command_queue.error`
  (error, per-service probe failure).
- New endpoint: `GET /health/arr_command_queue` returns the latest
  per-service report (service, queued_count, started_count,
  oldest_queued_age_seconds, drained_count, last_action, error,
  sample_drained_ids).
- `ArrClient.list_commands()` and `ArrClient.delete_command(id)` —
  thin wrappers around `GET /api/v{n}/command` and `DELETE
  /api/v{n}/command/{id}`. Scoped to Sonarr/Radarr (v3) in the scheduler
  wire-up.
- New YAML section `arr_command_queue:` and env overrides
  `DOCKTARR_ARR_COMMAND_QUEUE_ENABLED`,
  `DOCKTARR_ARR_COMMAND_QUEUE_POLL_INTERVAL_SECONDS`,
  `DOCKTARR_ARR_COMMAND_QUEUE_DRAIN_THRESHOLD_COUNT`,
  `DOCKTARR_ARR_COMMAND_QUEUE_DRAIN_AGE_SECONDS`,
  `DOCKTARR_ARR_COMMAND_QUEUE_DRAIN_COMMAND_NAMES`,
  `DOCKTARR_ARR_COMMAND_QUEUE_ELEVATED_WARN_COUNT`. Enabled by default
  with conservative thresholds (50 candidates / 600s); set
  `DOCKTARR_ARR_COMMAND_QUEUE_ENABLED=false` to disable.

## 0.7.0 — 2026-05-03

### Added
- **`plex_throttle` module — Plex-aware qBittorrent download cap.** Polls Plex
  `/status/sessions` on a fixed interval (default 30s). Three tiers: transcode
  active → tight cap (default 5 MB/s); direct play active → moderate cap
  (default 30 MB/s); idle (after grace) → unrestricted. Idempotent — only
  hits qBit when the target changes. Grace window (default 60s) prevents
  flapping when a stream pauses or buffers. Plex unreachable is treated as
  "lost visibility, don't lift caps blindly" — the prior limit is preserved.
  qBit failures are logged + retried on the next tick, never crash the job.
  Driven by S108 incident 2026-05-03 where unrelated disk-I/O contention
  surfaced the absence of any Plex-aware orchestration; bandwidth-class
  contention is the immediate fix this module ships, disk-class contention
  remains follow-up work.
- New env vars: `PLEX_THROTTLE_ENABLED`, `PLEX_URL`, `PLEX_TOKEN`,
  `PLEX_THROTTLE_INTERVAL`, `PLEX_THROTTLE_IDLE_LIMIT_KBPS`,
  `PLEX_THROTTLE_DIRECTPLAY_LIMIT_KBPS`,
  `PLEX_THROTTLE_TRANSCODE_LIMIT_KBPS`, `PLEX_THROTTLE_GRACE`. Disabled by
  default; opt-in per deployment. Reuses the `plex_client` already created
  by `permissions_health` if both are enabled, otherwise creates its own.
- New event: `plex_throttle.applied` (emitted only when the cap actually
  changes — no event spam from no-op ticks).
- New endpoint: `GET /health/plex_throttle` returns the latest snapshot
  (plex_state, active_sessions, transcode_sessions, target_kbps,
  applied_kbps, last_active_at, in_grace, last_action, error).
- `QBitClient.set_download_limit(bytes_per_sec)` and `get_download_limit()`
  for the throttle module. Both follow the existing 403→re-login retry
  pattern.

### Fixed
- `__version__` in `src/docktarr/__init__.py` was hardcoded `"0.3.0"` and had
  drifted across four releases. Now sourced from the installed package via
  `importlib.metadata` semantics in `main.py` (already correct there); the
  `__init__.py` literal is updated to match for any code that imports it
  directly.

## 0.6.0 — 2026-05-01

### Fixed
- **qbit_health: restart on any non-running status, not just exit 137.** Today's
  zion outage saw qBit exit with 255 (volume mount failed during a gluetun
  cycle / NFS hiccup) and the previous logic logged `exited_no_auto_restart`
  and did nothing. Cascade: dead qBit → arr stack failures, stack stayed dark
  until manual intervention. Now any `status != "running"` triggers a restart
  attempt, with a per-instance cooldown (default 15 min) and `qbit.restart_failed`
  event when the restart itself raises (e.g. the underlying volume is still
  broken). Same shape as `arr_services` recovery shipped in 0.5.2.

### Added
- **Telegram notifier sink.** `Notifier` now supports an optional Telegram
  outbound sink alongside the existing Discord webhook. Configure with
  `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` env vars. Both sinks fire
  independently; either, both, or neither can be configured. Sink failures
  are logged but never raised. Plain text rendering (Discord-style `**bold**`
  is stripped before send so we don't fight Telegram's Markdown parser).
  Inbound bot semantics (slash commands, ACL) are deliberately out of scope —
  see `lattice-telegram` for projects that need that.
- `Notifier.telegram_enabled` property (true when both bot_token and chat_id
  are set).
- `QbitHealthState.last_restart_attempt` for cooldown tracking.
- `QbitHealthConfig.restart_cooldown` (default 15 min).
- New event template: `qbit.restart_failed`. Templates added for the
  0.5.x events that were previously falling through to the generic format:
  `qbit.stale_namespace_restart`, `qbit.unreachable_threshold_restart`,
  `arr.restarted`, `arr.unreachable_threshold_restart`, `arr.restart_failed`.
- `WEBHOOK_EVENTS` default expanded to include the operational events from
  0.5.x (qbit.* and arr.* family + `imposter.detected`). Previously only
  `added,pruned,digest,stall.cleared` were enabled by default, so all the
  new recovery events were silently dropped unless operators opted in.

### Changed
- `last_action` value `restart_after_exit` is now used for non-137 exits
  (was `exited_no_auto_restart`). 137 retains the explicit
  `restart_exit_137` action label so timeline analysis can still see which
  variant of Pattern 1 triggered.

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
