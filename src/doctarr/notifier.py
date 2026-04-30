from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TEMPLATES = {
    "added": "**[Doctarr]** Indexer added: **{name}** (tested at {tested_at})",
    "pruned": "**[Doctarr]** Indexer pruned: **{name}** (down for {downtime_hours:.1f}h)",
    "degraded": "**[Doctarr]** Indexer degraded: **{name}** (failures: {failure_count}, since {first_failure})",
    "prowlarr.unreachable": "**[Doctarr]** Prowlarr unreachable since {last_seen} ({cycles_missed} cycles missed)",
    "digest": (
        "**[Doctarr] Daily Digest**\n"
        "Active: {total_active} | Degraded: {total_degraded}\n"
        "Added (24h): {added_24h} | Pruned (24h): {pruned_24h}"
    ),
    "stall.cleared": "**[Doctarr]** Stalled download cleared: **{name}** (idle {idle_hours:.1f}h, {progress}) via {app}",
    "imposter.detected": "**[Doctarr]** IMPOSTER DETECTED: **{name}** -- expected {expected_minutes}m, got {actual_minutes}m ({diff_percent}% off). File deleted, re-searching.",
    "qbit.restarted": "**[Doctarr]** qBittorrent container **{container_name}** restarted (exit code {exit_code})",
    "vpn.restarted": "**[Doctarr]** Gluetun container **{container_name}** restarted ({reason})",
    "vpn.degraded": "**[Doctarr]** VPN degraded: {reason}",
    "disk.warning": "**[Doctarr]** Disk WARNING: **{path}** at {percent_used}% used ({free_gb} GB free of {total_gb} GB)",
    "disk.critical": "**[Doctarr]** Disk CRITICAL: **{path}** at {percent_used}% used ({free_gb} GB free of {total_gb} GB)",
    "service.down": "**[Doctarr]** ARR service DOWN: **{name}** — {reason} ({url})",
    "hw.none_detected": "**[Doctarr]** No hardware accelerator detected on **{host}**",
    "hw.degraded": "**[Doctarr]** Hardware accelerator degraded on **{host}**: {reason}",
    "perms.drift": "**[Doctarr]** Permissions drift on **{path}**: {drift_count} files with wrong ownership (expected {expected_uid}:{expected_gid})",
    "perms.fixed": "**[Doctarr]** Permissions fixed on **{path}**: {fixed} files now owned by {expected_uid}:{expected_gid}",
    "perms.skipped_hardlinks": "**[Doctarr]** Skipped {count} hardlinked files during chown (MAM safety). Sample paths: {sample_paths}",
}


class Notifier:
    def __init__(
        self,
        client: httpx.AsyncClient,
        webhook_url: str | None,
        enabled_events: list[str],
    ):
        self._client = client
        self._webhook_url = webhook_url
        self._enabled_events = set(enabled_events)

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        if not self._webhook_url:
            return
        if event not in self._enabled_events:
            return

        template = _TEMPLATES.get(event, f"**[Doctarr]** {event}: {{name}}")
        try:
            message = template.format(**payload)
        except KeyError:
            message = f"**[Doctarr]** {event}: {payload}"

        body = {"content": message}
        try:
            resp = await self._client.post(self._webhook_url, json=body)
            if resp.status_code >= 400:
                log.warning("Webhook returned %d for event %s", resp.status_code, event)
        except httpx.HTTPError as exc:
            log.warning("Webhook delivery failed for event %s: %s", event, exc)
