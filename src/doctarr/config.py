from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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
class ArrAppConfig:
    url: str
    api_key: str
    name: str


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
    # v0.2: stall detection
    qbit_url: str | None
    qbit_username: str | None
    qbit_password: str | None
    arr_apps: list[ArrAppConfig]
    stall_threshold: timedelta
    stall_interval: timedelta
    protected_categories: list[str]
    # v0.3: imposter detection
    imposter_interval: timedelta
    imposter_tolerance: float
    imposter_lookback: timedelta

    @classmethod
    def from_env(cls) -> Config:
        url = _require_env("PROWLARR_URL").rstrip("/")
        api_key = _require_env("PROWLARR_API_KEY")
        webhook_url = os.environ.get("WEBHOOK_URL", "").strip() or None
        events_raw = os.environ.get(
            "WEBHOOK_EVENTS", "added,pruned,digest,stall.cleared"
        ).strip()
        webhook_events = [e.strip() for e in events_raw.split(",") if e.strip()]

        # Build arr app list from env vars
        arr_apps = []
        for prefix, name in [
            ("SONARR", "Sonarr"),
            ("RADARR", "Radarr"),
            ("READARR", "Readarr"),
            ("BOOKSHELF", "Bookshelf"),
        ]:
            app_url = os.environ.get(f"{prefix}_URL", "").strip()
            app_key = os.environ.get(f"{prefix}_API_KEY", "").strip()
            if app_url and app_key:
                arr_apps.append(
                    ArrAppConfig(url=app_url.rstrip("/"), api_key=app_key, name=name)
                )

        protected_raw = os.environ.get("PROTECTED_CATEGORIES", "MAM").strip()
        protected = [c.strip() for c in protected_raw.split(",") if c.strip()]

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
            qbit_url=os.environ.get("QBITTORRENT_URL", "").strip() or None,
            qbit_username=os.environ.get("QBITTORRENT_USERNAME", "").strip() or None,
            qbit_password=os.environ.get("QBITTORRENT_PASSWORD", "").strip() or None,
            arr_apps=arr_apps,
            stall_threshold=parse_duration(os.environ.get("STALL_THRESHOLD", "6h")),
            stall_interval=parse_duration(os.environ.get("STALL_INTERVAL", "1h")),
            protected_categories=protected,
            imposter_interval=parse_duration(os.environ.get("IMPOSTER_INTERVAL", "1h")),
            imposter_tolerance=float(os.environ.get("IMPOSTER_TOLERANCE", "0.40")),
            imposter_lookback=parse_duration(
                os.environ.get("IMPOSTER_LOOKBACK", "24h")
            ),
        )
