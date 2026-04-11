from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import timedelta

_DURATION_RE = re.compile(r"^(\d+)\s*([smhd]?)$", re.IGNORECASE)

_UNITS = {
    "": "seconds",
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}


def parse_duration(value: str) -> timedelta:
    """Parse a human-friendly duration string like '6h', '30s', '2m', '1d'."""
    m = _DURATION_RE.match(value.strip())
    if not m:
        raise ValueError(
            f"Invalid duration: {value!r}. Use format like '6h', '30s', '2m', '1d'."
        )
    amount = int(m.group(1))
    unit = _UNITS[m.group(2).lower()]
    return timedelta(**{unit: amount})


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} environment variable is required but not set.")
    return value


@dataclass(frozen=True)
class Config:
    prowlarr_url: str
    prowlarr_api_key: str
    discovery_interval: timedelta
    test_interval: timedelta
    prune_interval: timedelta
    prune_threshold: timedelta
    test_delay: timedelta
    webhook_url: str | None
    webhook_events: list[str]
    digest_time: str
    log_level: str
    tz: str

    @classmethod
    def from_env(cls) -> Config:
        url = _require_env("PROWLARR_URL").rstrip("/")
        api_key = _require_env("PROWLARR_API_KEY")
        webhook_url = os.environ.get("WEBHOOK_URL", "").strip() or None
        events_raw = os.environ.get("WEBHOOK_EVENTS", "added,pruned,digest").strip()
        webhook_events = [e.strip() for e in events_raw.split(",") if e.strip()]

        return cls(
            prowlarr_url=url,
            prowlarr_api_key=api_key,
            discovery_interval=parse_duration(
                os.environ.get("DISCOVERY_INTERVAL", "6h")
            ),
            test_interval=parse_duration(os.environ.get("TEST_INTERVAL", "2h")),
            prune_interval=parse_duration(os.environ.get("PRUNE_INTERVAL", "1h")),
            prune_threshold=parse_duration(os.environ.get("PRUNE_THRESHOLD", "12h")),
            test_delay=parse_duration(os.environ.get("TEST_DELAY", "2s")),
            webhook_url=webhook_url,
            webhook_events=webhook_events,
            digest_time=os.environ.get("DIGEST_TIME", "08:00").strip(),
            log_level=os.environ.get("LOG_LEVEL", "info").strip().lower(),
            tz=os.environ.get("TZ", "UTC").strip(),
        )
