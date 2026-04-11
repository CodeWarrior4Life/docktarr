import json

import httpx
import pytest
from doctarr.notifier import Notifier


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
