# Doctarr

Autonomous indexer manager for [Prowlarr](https://prowlarr.com). Discovers, tests, and maintains public torrent indexers so you don't have to.

## What It Does

Prowlarr ships with 200+ public torrent indexer definitions, but you have to manually add, test, and clean up each one. Doctarr automates the entire lifecycle:

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
doctarr:
  image: ghcr.io/codewarrior4life/doctarr:latest
  container_name: doctarr
  environment:
    - PROWLARR_URL=http://prowlarr:9696
    - PROWLARR_API_KEY=your-api-key
    - TZ=America/New_York
  volumes:
    - ./config/doctarr:/config
  restart: unless-stopped
```

That's it. Doctarr will start discovering and testing indexers immediately.

## How It Works

Doctarr runs four independent jobs:

| Job | Default Interval | What It Does |
|-----|-----------------|--------------|
| Discovery | 6 hours | Scans Prowlarr schema for new public indexers |
| Tester | 2 hours | Health-checks all managed indexers |
| Pruner | 1 hour | Removes indexers broken for 12+ hours |
| Digest | Daily 8:00 AM | Sends summary via webhook |

### Safety

- **Private trackers are never touched.** Doctarr only manages indexers it creates, identified by a `doctarr` tag in Prowlarr.
- **User changes are respected.** Remove the tag and Doctarr stops managing that indexer.
- **Graceful recovery.** If state is lost, Doctarr rebuilds from Prowlarr.

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

## Webhook Events

| Event | When |
|-------|------|
| `indexer.added` | New indexer passes test and is enabled |
| `indexer.pruned` | Broken indexer removed after threshold |
| `indexer.degraded` | Working indexer starts failing |
| `prowlarr.unreachable` | Can't reach Prowlarr for 3+ cycles |
| `health.digest` | Daily summary |

## Development

```bash
git clone https://github.com/CodeWarrior4Life/doctarr.git
cd doctarr
python -m venv .venv && source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
pytest -v
```

## License

MIT
