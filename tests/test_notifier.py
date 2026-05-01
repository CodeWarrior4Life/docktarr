import json

import httpx
import pytest
from docktarr.notifier import Notifier


class TestNotifier:
    @pytest.fixture
    def captured_requests(self):
        return []

    @pytest.fixture
    def notifier(self, captured_requests):
        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(
                {
                    "url": str(request.url),
                    "body": json.loads(request.content),
                }
            )
            return httpx.Response(204)

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        return Notifier(
            client=http,
            webhook_url="https://discord.com/api/webhooks/123/abc",
            enabled_events=["added", "pruned", "degraded", "digest"],
        )

    async def test_emit_added(self, notifier, captured_requests):
        await notifier.emit(
            "added", {"name": "1337x", "tested_at": "2026-04-10T20:00:00"}
        )
        assert len(captured_requests) == 1
        body = captured_requests[0]["body"]
        assert "1337x" in body["content"]

    async def test_emit_pruned(self, notifier, captured_requests):
        await notifier.emit("pruned", {"name": "DeadTracker", "downtime_hours": 13.5})
        assert len(captured_requests) == 1

    async def test_filtered_event_not_sent(self, captured_requests):
        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(204)

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        notifier = Notifier(
            client=http,
            webhook_url="https://discord.com/api/webhooks/123/abc",
            enabled_events=["added"],
        )
        await notifier.emit("pruned", {"name": "Tracker"})
        assert len(captured_requests) == 0

    async def test_no_webhook_url_is_noop(self, captured_requests):
        notifier = Notifier(
            client=httpx.AsyncClient(), webhook_url=None, enabled_events=["added"]
        )
        await notifier.emit("added", {"name": "1337x"})
        assert len(captured_requests) == 0

    async def test_emit_digest(self, notifier, captured_requests):
        await notifier.emit(
            "digest",
            {
                "total_active": 25,
                "total_degraded": 3,
                "added_24h": 2,
                "pruned_24h": 1,
            },
        )
        assert len(captured_requests) == 1
        body = captured_requests[0]["body"]
        assert "25" in body["content"]


class TestTelegramSink:
    """Optional Telegram sink alongside the existing Discord webhook."""

    @pytest.fixture
    def captured(self):
        return {"discord": [], "telegram": []}

    def _make_notifier(self, captured, *, webhook=True, telegram=True):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "discord.com" in url:
                captured["discord"].append(
                    {"url": url, "body": json.loads(request.content)}
                )
            elif "api.telegram.org" in url:
                captured["telegram"].append(
                    {"url": url, "body": json.loads(request.content)}
                )
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        return Notifier(
            client=http,
            webhook_url="https://discord.com/api/webhooks/1/abc" if webhook else None,
            enabled_events=["added", "qbit.restarted"],
            telegram_bot_token="bot-token-123" if telegram else None,
            telegram_chat_id="-1001234567890" if telegram else None,
        )

    async def test_both_sinks_fire_when_both_configured(self, captured):
        n = self._make_notifier(captured)
        await n.emit("added", {"name": "1337x", "tested_at": "now"})
        assert len(captured["discord"]) == 1
        assert len(captured["telegram"]) == 1
        # Telegram body uses the sendMessage shape
        tg = captured["telegram"][0]
        assert "botbot-token-123/sendMessage" in tg["url"]
        assert tg["body"]["chat_id"] == "-1001234567890"
        assert "1337x" in tg["body"]["text"]

    async def test_telegram_only_when_webhook_unset(self, captured):
        n = self._make_notifier(captured, webhook=False)
        await n.emit("added", {"name": "Foo", "tested_at": "now"})
        assert len(captured["discord"]) == 0
        assert len(captured["telegram"]) == 1

    async def test_webhook_only_when_telegram_unset(self, captured):
        n = self._make_notifier(captured, telegram=False)
        await n.emit("added", {"name": "Foo", "tested_at": "now"})
        assert len(captured["discord"]) == 1
        assert len(captured["telegram"]) == 0

    async def test_telegram_disabled_when_chat_id_missing(self, captured):
        def handler(request):
            captured["telegram"].append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        n = Notifier(
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            webhook_url=None,
            enabled_events=["added"],
            telegram_bot_token="token",
            telegram_chat_id=None,
        )
        await n.emit("added", {"name": "Foo", "tested_at": "now"})
        assert captured["telegram"] == []

    async def test_filter_applies_to_telegram_sink_too(self, captured):
        n = self._make_notifier(captured)
        await n.emit("pruned", {"name": "x", "downtime_hours": 1.0})
        assert captured["discord"] == []
        assert captured["telegram"] == []

    async def test_telegram_failure_does_not_raise_or_block_webhook(self, captured):
        def handler(request):
            url = str(request.url)
            if "telegram" in url:
                raise httpx.ConnectError("telegram down")
            captured["discord"].append(
                {"url": url, "body": json.loads(request.content)}
            )
            return httpx.Response(204)

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport)
        n = Notifier(
            client=http,
            webhook_url="https://discord.com/api/webhooks/1/abc",
            enabled_events=["added"],
            telegram_bot_token="t",
            telegram_chat_id="42",
        )
        # Must not raise
        await n.emit("added", {"name": "Foo", "tested_at": "now"})
        assert len(captured["discord"]) == 1


def test_new_event_types_are_allowed():
    # The notifier module must recognize each of these event types.
    # If notifier uses KNOWN_EVENTS set → check membership.
    # If notifier uses templates dict → check keys.
    # If notifier is fully permissive → at minimum the templates registry should have entries.
    from docktarr import notifier as n

    required = {
        "hw.none_detected",
        "hw.degraded",
        "perms.drift",
        "perms.fixed",
        "perms.skipped_hardlinks",
        "qbit.restarted",
        "vpn.degraded",
        "disk.warning",
        "disk.critical",
        "service.down",
    }
    # Prefer KNOWN_EVENTS if it exists; else check templates attribute.
    allowed = None
    for attr in ("KNOWN_EVENTS", "_TEMPLATES", "TEMPLATES", "EVENTS"):
        if hasattr(n, attr):
            allowed = getattr(n, attr)
            if isinstance(allowed, dict):
                allowed = set(allowed.keys())
            break
    assert allowed is not None, "notifier module should expose a known-events surface"
    assert required.issubset(allowed), f"missing events: {required - allowed}"
