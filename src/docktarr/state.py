from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

log = logging.getLogger(__name__)


class IndexerStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DEGRADED = "degraded"


@dataclass
class IndexerState:
    definition_name: str
    prowlarr_id: int
    status: IndexerStatus
    last_tested: datetime | None = None
    failure_count: int = 0
    first_failure: datetime | None = None
    last_failure: datetime | None = None

    @classmethod
    def new_candidate(cls, definition_name: str, prowlarr_id: int) -> IndexerState:
        return cls(
            definition_name=definition_name,
            prowlarr_id=prowlarr_id,
            status=IndexerStatus.CANDIDATE,
        )

    def to_dict(self) -> dict:
        return {
            "definition_name": self.definition_name,
            "prowlarr_id": self.prowlarr_id,
            "status": self.status.value,
            "last_tested": self.last_tested.isoformat() if self.last_tested else None,
            "failure_count": self.failure_count,
            "first_failure": self.first_failure.isoformat()
            if self.first_failure
            else None,
            "last_failure": self.last_failure.isoformat()
            if self.last_failure
            else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> IndexerState:
        return cls(
            definition_name=d["definition_name"],
            prowlarr_id=d["prowlarr_id"],
            status=IndexerStatus(d["status"]),
            last_tested=datetime.fromisoformat(d["last_tested"])
            if d.get("last_tested")
            else None,
            failure_count=d.get("failure_count", 0),
            first_failure=datetime.fromisoformat(d["first_failure"])
            if d.get("first_failure")
            else None,
            last_failure=datetime.fromisoformat(d["last_failure"])
            if d.get("last_failure")
            else None,
        )


class StateStore:
    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, IndexerState] = {}

    def load(self) -> None:
        if not self._path.exists():
            self._data = {}
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.warning("Corrupt state file, backing up and starting fresh: %s", exc)
            backup = self._path.with_suffix(".json.bak")
            self._path.rename(backup)
            self._data = {}
            return
        # 0.7.2: state file now supports two top-level shapes:
        #   - legacy flat map  { definition_name: IndexerState.to_dict, ... }
        #   - new envelope     { "indexers": {...}, "arr_burst": {...} }
        # Detect the envelope by looking for a reserved key; fall back to
        # legacy for backward-compat with existing /config/state.json files.
        try:
            if isinstance(raw, dict) and "indexers" in raw:
                indexers = raw.get("indexers", {})
                self._data = {
                    k: IndexerState.from_dict(v) for k, v in indexers.items()
                }
                burst = raw.get("arr_burst")
                if isinstance(burst, dict):
                    self._arr_burst = burst
            else:
                self._data = {k: IndexerState.from_dict(v) for k, v in raw.items()}
        except (KeyError, ValueError) as exc:
            log.warning("Corrupt state file, backing up and starting fresh: %s", exc)
            backup = self._path.with_suffix(".json.bak")
            self._path.rename(backup)
            self._data = {}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        indexers = {k: v.to_dict() for k, v in self._data.items()}
        # If any extension bucket is present, emit the new envelope shape;
        # otherwise stay legacy-flat so older docktarr versions can still
        # consume our state file in the event of a downgrade.
        burst = getattr(self, "_arr_burst", None)
        if burst:
            payload: dict = {"indexers": indexers, "arr_burst": burst}
        else:
            payload = indexers
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get(self, definition_name: str) -> IndexerState | None:
        return self._data.get(definition_name)

    def set(self, state: IndexerState) -> None:
        self._data[state.definition_name] = state

    def remove(self, definition_name: str) -> None:
        self._data.pop(definition_name, None)

    def get_by_status(self, status: IndexerStatus) -> list[IndexerState]:
        return [s for s in self._data.values() if s.status == status]

    def all(self) -> list[IndexerState]:
        return list(self._data.values())

    def set_hw_report(self, report) -> None:
        self._hw_report = report

    @property
    def hw_report(self):
        return getattr(self, "_hw_report", None)
