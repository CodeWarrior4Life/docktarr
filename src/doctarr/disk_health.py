"""Disk space health monitoring for Doctarr.

Ported from arr-orchestrator/disk.py ``check_disk_space``.

Behavior faithfully ported
--------------------------
1. For each configured path, call ``shutil.disk_usage`` to get used/free/total.
2. Compute percent-used.
3. If percent_used >= ``critical_pct`` → emit ``disk.critical`` notifier event.
4. If percent_used >= ``warning_pct`` (but below critical) → emit ``disk.warning``.
5. If below ``warning_pct`` → no event (healthy).

Deviations from orchestrator
-----------------------------
- Orchestrator monitored a single path from config['disk']['monitor_path'].
  Doctarr supports a list of ``DiskPath`` objects, one per monitored path.
  This is a deliberate generalisation (the orchestrator was single-disk).
- Orchestrator had a ``< 80%`` digest-only tier.  Doctarr drops the digest
  concept (no DailyDigest equivalent in doctarr).  The 80% soft-tier is
  omitted intentionally — the notifier's webhook_events list gates delivery,
  so operators who want verbose disk status can subscribe to all events.
- Orchestrator embedded MAM-specific cleanup suggestions in the critical
  message.  Doctarr keeps the message generic; MAM compliance context is
  documented in memory/mam_compliance.md.  A TODO is left for callers to
  inject custom notes via ``context`` field.
- Orchestrator used a module-level ``_last_check`` cooldown timer.  Doctarr
  is APScheduler-driven — the run interval is the scheduler tick, so no
  internal cooldown needed.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

from doctarr.notifier import Notifier

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiskPath:
    """Configuration for a single monitored disk path."""

    path: str
    warning_pct: float = 85.0
    critical_pct: float = 95.0


async def run_disk_health(
    paths: list[DiskPath],
    notifier: Notifier,
) -> list[dict[str, Any]]:
    """Single-shot disk health probe for all configured paths.

    Returns a list of per-path status dicts with keys:
      - path (str)
      - percent_used (float)
      - free_gb (float)
      - total_gb (float)
      - status (str): "ok" | "warning" | "critical" | "error"

    Called by APScheduler on each health-check tick.
    """
    results: list[dict[str, Any]] = []

    for dp in paths:
        result = await _check_path(dp, notifier)
        results.append(result)

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _check_path(dp: DiskPath, notifier: Notifier) -> dict[str, Any]:
    """Check a single disk path and emit events as needed."""
    try:
        usage = shutil.disk_usage(dp.path)
    except OSError as exc:
        log.error("disk_health: disk_usage(%r) failed: %s", dp.path, exc)
        return {"path": dp.path, "status": "error", "error": str(exc)}

    percent_used = (usage.used / usage.total) * 100
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)

    log.debug(
        "disk_health: %s — %.1f%% used (%.1f GB free / %.1f GB total)",
        dp.path,
        percent_used,
        free_gb,
        total_gb,
    )

    payload = {
        "path": dp.path,
        "percent_used": round(percent_used, 1),
        "free_gb": round(free_gb, 2),
        "total_gb": round(total_gb, 2),
    }

    if percent_used >= dp.critical_pct:
        log.warning(
            "disk_health: CRITICAL — %s at %.1f%% (%.1f GB free)",
            dp.path,
            percent_used,
            free_gb,
        )
        await notifier.emit("disk.critical", payload)
        return {**payload, "status": "critical"}

    if percent_used >= dp.warning_pct:
        log.warning(
            "disk_health: WARNING — %s at %.1f%% (%.1f GB free)",
            dp.path,
            percent_used,
            free_gb,
        )
        await notifier.emit("disk.warning", payload)
        return {**payload, "status": "warning"}

    return {**payload, "status": "ok"}
