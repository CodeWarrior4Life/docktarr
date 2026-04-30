# Changelog

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
