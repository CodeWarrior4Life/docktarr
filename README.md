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
| `DOCTARR_HOST_NAME` | No | - | Override detected hostname for SSH routing |
| `DOCTARR_SKIP_NETWORK_INIT` | No | `false` | Skip SSH connectivity check on startup |

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
