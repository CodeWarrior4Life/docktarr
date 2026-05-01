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
    "qbit.stale_namespace_restart": "**[Doctarr]** qBittorrent restarted: stale gluetun namespace (vpn={vpn_started_at}, qbit={qbit_started_at})",
    "qbit.unreachable_threshold_restart": "**[Doctarr]** qBittorrent restarted: API unreachable for {consecutive_ticks} consecutive ticks (threshold {threshold})",
    "qbit.restart_failed": "**[Doctarr]** qBittorrent restart FAILED (exit_code={exit_code}): {error}",
    "arr.restarted": "**[Doctarr]** {name} container **{container_name}** restarted after exit (code {exit_code})",
    "arr.unreachable_threshold_restart": "**[Doctarr]** {name} restarted: API unreachable for {consecutive_ticks} consecutive ticks (threshold {threshold})",
    "arr.restart_failed": "**[Doctarr]** {name} restart FAILED (container {container_name}): {error}",
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
    """Multi-sink event notifier.

    Sinks are independent: configure either, both, or neither. Each sink failure
    is logged but never raised — one bad sink must not break delivery to the
    others, and notifier failures never crash the scheduler.

    Sinks
    -----
    - **Discord-style webhook** (existing): POSTs ``{"content": message}`` to
      ``webhook_url``. Templates use ``**bold**`` markdown.
    - **Telegram bot** (added 0.6.0): POSTs to
      ``https://api.telegram.org/bot{token}/sendMessage`` with ``chat_id`` and
      plain ``text``. Configure via ``telegram_bot_token`` + ``telegram_chat_id``.
      The chat_id is the destination chat (positive for users, negative for
      groups/channels). Send ``/start`` to your bot first to make it deliverable.

    Future inbound bot semantics (slash commands, ACL, roster) are out of scope
    here — see ``lattice-telegram`` for a project that needs that.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        webhook_url: str | None,
        enabled_events: list[str],
        *,
        telegram_bot_token: str | None = None,
        telegram_chat_id: str | None = None,
    ):
        self._client = client
        self._webhook_url = webhook_url
        self._enabled_events = set(enabled_events)
        self._telegram_bot_token = telegram_bot_token
        self._telegram_chat_id = telegram_chat_id

    @property
    def telegram_enabled(self) -> bool:
        return bool(self._telegram_bot_token and self._telegram_chat_id)

    async def emit(self, event: str, payload: dict[str, Any]) -> None:
        if event not in self._enabled_events:
            return

        template = _TEMPLATES.get(event, f"**[Doctarr]** {event}: {{name}}")
        try:
            message = template.format(**payload)
        except KeyError:
            message = f"**[Doctarr]** {event}: {payload}"

        if self._webhook_url:
            await self._send_webhook(event, message)
        if self.telegram_enabled:
            await self._send_telegram(event, message)

    async def _send_webhook(self, event: str, message: str) -> None:
        body = {"content": message}
        try:
            resp = await self._client.post(self._webhook_url, json=body)
            if resp.status_code >= 400:
                log.warning("Webhook returned %d for event %s", resp.status_code, event)
        except httpx.HTTPError as exc:
            log.warning("Webhook delivery failed for event %s: %s", event, exc)

    async def _send_telegram(self, event: str, message: str) -> None:
        # Strip Discord-style **bold** to plain text — Telegram's Markdown parser
        # is fussier than Discord's and we don't gain much from per-sink templates
        # for the kind of operational notifications docktarr emits. Plain text is
        # the safest default.
        text = message.replace("**", "")
        url = f"https://api.telegram.org/bot{self._telegram_bot_token}/sendMessage"
        body = {"chat_id": self._telegram_chat_id, "text": text}
        try:
            resp = await self._client.post(url, json=body)
            if resp.status_code >= 400:
                log.warning(
                    "Telegram returned %d for event %s: %s",
                    resp.status_code,
                    event,
                    resp.text[:200],
                )
        except Exception as exc:
            # httpx.HTTPError + anything else (DNS, TLS, etc.) — never raise
            # past the notifier boundary.
            log.warning("Telegram delivery failed for event %s: %s", event, exc)
