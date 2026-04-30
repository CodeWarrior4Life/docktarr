from docktarr.state import IndexerStatus, IndexerState, StateStore


class TestIndexerState:
    def test_new_candidate(self):
        s = IndexerState.new_candidate("1337x", prowlarr_id=42)
        assert s.definition_name == "1337x"
        assert s.prowlarr_id == 42
        assert s.status == IndexerStatus.CANDIDATE
        assert s.failure_count == 0
        assert s.first_failure is None

    def test_to_dict_roundtrip(self):
        s = IndexerState.new_candidate("1337x", prowlarr_id=42)
        d = s.to_dict()
        s2 = IndexerState.from_dict(d)
        assert s2.definition_name == s.definition_name
        assert s2.prowlarr_id == s.prowlarr_id
        assert s2.status == s.status


class TestStateStore:
    def test_save_and_load(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(path)
        state = IndexerState.new_candidate("1337x", prowlarr_id=42)
        store.set(state)
        store.save()

        store2 = StateStore(path)
        store2.load()
        loaded = store2.get("1337x")
        assert loaded is not None
        assert loaded.prowlarr_id == 42
        assert loaded.status == IndexerStatus.CANDIDATE

    def test_remove(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(path)
        store.set(IndexerState.new_candidate("1337x", prowlarr_id=42))
        store.remove("1337x")
        assert store.get("1337x") is None

    def test_get_by_status(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(path)
        store.set(IndexerState.new_candidate("1337x", prowlarr_id=1))
        active = IndexerState.new_candidate("TPB", prowlarr_id=2)
        active.status = IndexerStatus.ACTIVE
        store.set(active)

        candidates = store.get_by_status(IndexerStatus.CANDIDATE)
        assert len(candidates) == 1
        assert candidates[0].definition_name == "1337x"

    def test_corrupt_file_recovery(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("NOT VALID JSON{{{")
        store = StateStore(path)
        store.load()
        assert len(store.all()) == 0
        assert (tmp_path / "state.json.bak").exists()

    def test_missing_file_starts_empty(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(path)
        store.load()
        assert len(store.all()) == 0

    def test_all_returns_list(self, tmp_path):
        path = tmp_path / "state.json"
        store = StateStore(path)
        store.set(IndexerState.new_candidate("1337x", prowlarr_id=1))
        items = store.all()
        assert len(items) == 1

    def test_set_and_get_hw_report(self, tmp_path):
        from pathlib import Path

        path = tmp_path / "state.json"
        s = StateStore(path=Path(path))
        assert s.hw_report is None
        s.set_hw_report({"zion": [{"kind": "quicksync"}]})
        assert s.hw_report == {"zion": [{"kind": "quicksync"}]}
