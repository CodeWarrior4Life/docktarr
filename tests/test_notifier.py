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
