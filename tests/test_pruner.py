from datetime import datetime, timedelta, timezone

import httpx
import pytest
from doctarr.notifier import Notifier
from doctarr.prowlarr import ProwlarrClient
from doctarr.pruner import run_pruner
from doctarr.state import IndexerState, IndexerStatus, StateStore


class TestPruner:
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

    def _make_prowlarr(self, deleted_ids: list) -> ProwlarrClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                idx_id = int(request.url.path.split("/")[-1])
                deleted_ids.append(idx_id)
                return httpx.Response(200)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        http = httpx.AsyncClient(transport=transport, base_url="http://prowlarr:9696")
        return ProwlarrClient(http, api_key="test")

    async def test_prunes_indexer_past_threshold(
        self, state_store, notifier, notifications
    ):
        deleted = []
        prowlarr = self._make_prowlarr(deleted)

        degraded = IndexerState.new_candidate("DeadTracker", prowlarr_id=42)
        degraded.status = IndexerStatus.DEGRADED
        degraded.first_failure = datetime.now(timezone.utc) - timedelta(hours=13)
        degraded.failure_count = 6
        state_store.set(degraded)

        await run_pruner(
            prowlarr, state_store, notifier, prune_threshold=timedelta(hours=12)
        )

        assert 42 in deleted
        assert state_store.get("DeadTracker") is None
        assert any(n["event"] == "pruned" for n in notifications)

    async def test_does_not_prune_within_threshold(self, state_store, notifier):
        deleted = []
        prowlarr = self._make_prowlarr(deleted)

        degraded = IndexerState.new_candidate("SlowTracker", prowlarr_id=43)
        degraded.status = IndexerStatus.DEGRADED
        degraded.first_failure = datetime.now(timezone.utc) - timedelta(hours=6)
        degraded.failure_count = 3
        state_store.set(degraded)

        await run_pruner(
            prowlarr, state_store, notifier, prune_threshold=timedelta(hours=12)
        )

        assert len(deleted) == 0
        assert state_store.get("SlowTracker") is not None

    async def test_ignores_active_and_candidate(self, state_store, notifier):
        deleted = []
        prowlarr = self._make_prowlarr(deleted)

        state_store.set(IndexerState.new_candidate("Candidate", prowlarr_id=1))
        active = IndexerState.new_candidate("Active", prowlarr_id=2)
        active.status = IndexerStatus.ACTIVE
        state_store.set(active)

        await run_pruner(
            prowlarr, state_store, notifier, prune_threshold=timedelta(hours=12)
        )

        assert len(deleted) == 0
