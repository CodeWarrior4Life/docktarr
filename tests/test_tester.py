import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from docktarr.notifier import Notifier
from docktarr.prowlarr import ProwlarrClient
from docktarr.state import IndexerState, IndexerStatus, StateStore
from docktarr.tester import run_tester


class TestTester:
    @pytest.fixture
    def state_store(self, tmp_path):
        return StateStore(tmp_path / "state.json")

    @pytest.fixture
    def notifications(self):
        return []

    @pytest.fixture
    def notifier(self, notifications):
        class CapturingNotifier(Notifier):
            async def emit(self, event, payload):
                notifications.append({"event": event, "payload": payload})

        transport = httpx.MockTransport(lambda r: httpx.Response(204))
        return CapturingNotifier(
            httpx.AsyncClient(transport=transport),
            webhook_url=None,
            enabled_events=[],
        )

    def _make_prowlarr(
        self, indexers: list[dict], test_results: dict[int, bool]
    ) -> ProwlarrClient:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            method = request.method
            if path == "/api/v1/indexer" and method == "GET":
                return httpx.Response(200, json=indexers)
            if path == "/api/v1/indexer/test" and method == "POST":
                body = json.loads(request.content)
                idx_id = body.get("id", 0)
                if test_results.get(idx_id, False):
                    return httpx.Response(200, json={})
                return httpx.Response(400, json=[{"errorMessage": "Connection failed"}])
            if method == "PUT" and path.startswith("/api/v1/indexer/"):
                body = json.loads(request.content)
                return httpx.Response(200, json=body)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport, base_url="http://prowlarr:9696")
        return ProwlarrClient(http, api_key="test")

    async def test_candidate_passes_becomes_active(
        self, state_store, notifier, notifications
    ):
        indexers = [
            {
                "id": 1,
                "definitionName": "1337x",
                "name": "1337x",
                "tags": [5],
                "enable": False,
            }
        ]
        prowlarr = self._make_prowlarr(indexers, {1: True})
        state_store.set(IndexerState.new_candidate("1337x", prowlarr_id=1))

        await run_tester(prowlarr, state_store, notifier, tag_id=5, test_delay=0)

        s = state_store.get("1337x")
        assert s.status == IndexerStatus.ACTIVE
        assert s.last_tested is not None
        assert any(n["event"] == "added" for n in notifications)

    async def test_candidate_fails_stays_candidate(self, state_store, notifier):
        indexers = [
            {
                "id": 1,
                "definitionName": "1337x",
                "name": "1337x",
                "tags": [5],
                "enable": False,
            }
        ]
        prowlarr = self._make_prowlarr(indexers, {1: False})
        state_store.set(IndexerState.new_candidate("1337x", prowlarr_id=1))

        await run_tester(prowlarr, state_store, notifier, tag_id=5, test_delay=0)

        s = state_store.get("1337x")
        assert s.status == IndexerStatus.CANDIDATE
        assert s.failure_count == 1

    async def test_active_fails_becomes_degraded(
        self, state_store, notifier, notifications
    ):
        indexers = [
            {
                "id": 1,
                "definitionName": "1337x",
                "name": "1337x",
                "tags": [5],
                "enable": True,
            }
        ]
        prowlarr = self._make_prowlarr(indexers, {1: False})
        active = IndexerState.new_candidate("1337x", prowlarr_id=1)
        active.status = IndexerStatus.ACTIVE
        state_store.set(active)

        await run_tester(prowlarr, state_store, notifier, tag_id=5, test_delay=0)

        s = state_store.get("1337x")
        assert s.status == IndexerStatus.DEGRADED
        assert s.failure_count == 1
        assert s.first_failure is not None
        assert any(n["event"] == "degraded" for n in notifications)

    async def test_degraded_passes_becomes_active(self, state_store, notifier):
        indexers = [
            {
                "id": 1,
                "definitionName": "1337x",
                "name": "1337x",
                "tags": [5],
                "enable": True,
            }
        ]
        prowlarr = self._make_prowlarr(indexers, {1: True})
        degraded = IndexerState.new_candidate("1337x", prowlarr_id=1)
        degraded.status = IndexerStatus.DEGRADED
        degraded.failure_count = 3
        degraded.first_failure = datetime.now(timezone.utc) - timedelta(hours=2)
        state_store.set(degraded)

        await run_tester(prowlarr, state_store, notifier, tag_id=5, test_delay=0)

        s = state_store.get("1337x")
        assert s.status == IndexerStatus.ACTIVE
        assert s.failure_count == 0
        assert s.first_failure is None
