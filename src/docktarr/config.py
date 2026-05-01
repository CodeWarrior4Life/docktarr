from __future__ import annotations

import dataclasses
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from docktarr.yaml_config import YamlConfig, load_yaml_config

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
    container_name: str | None = None

    @property
    def effective_container_name(self) -> str:
        """Container name to inspect/restart for this service.

        Defaults to ``name.lower()`` (e.g. "Sonarr" -> "sonarr"). Override via
        ``container_name`` (or per-service env var like ``READARR_CONTAINER``)
        when the deployed container is named differently — e.g. our
        Readarr instance runs as ``readarr-audiobooks``.
        """
        return self.container_name or self.name.lower()


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
    telegram_bot_token: str | None
    telegram_chat_id: str | None
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
    imposter_backfill_enabled: bool
    imposter_backfill_interval: timedelta
    # v0.4: YAML-driven config
    yaml: YamlConfig = field(default_factory=YamlConfig)

    @classmethod
    def from_env(cls) -> Config:
        url = _require_env("PROWLARR_URL").rstrip("/")
        api_key = _require_env("PROWLARR_API_KEY")
        webhook_url = os.environ.get("WEBHOOK_URL", "").strip() or None
        events_raw = os.environ.get(
            "WEBHOOK_EVENTS",
            "added,pruned,digest,stall.cleared,"
            "qbit.restarted,qbit.stale_namespace_restart,"
            "qbit.unreachable_threshold_restart,qbit.restart_failed,"
            "arr.restarted,arr.unreachable_threshold_restart,arr.restart_failed,"
            "imposter.detected",
        ).strip()
        webhook_events = [e.strip() for e in events_raw.split(",") if e.strip()]
        telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None
        telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None

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
                container_override = (
                    os.environ.get(f"{prefix}_CONTAINER", "").strip() or None
                )
                arr_apps.append(
                    ArrAppConfig(
                        url=app_url.rstrip("/"),
                        api_key=app_key,
                        name=name,
                        container_name=container_override,
                    )
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
            telegram_bot_token=telegram_bot_token,
            telegram_chat_id=telegram_chat_id,
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
            imposter_backfill_enabled=os.environ.get(
                "IMPOSTER_BACKFILL_ENABLED", "true"
            )
            .strip()
            .lower()
            not in ("0", "false", "no"),
            imposter_backfill_interval=parse_duration(
                os.environ.get("IMPOSTER_BACKFILL_INTERVAL", "7d")
            ),
        )

    @classmethod
    def from_env_and_yaml(
        cls, yaml_path: Path | str = "/config/docktarr.yaml"
    ) -> Config:
        base = cls.from_env()
        yaml_cfg = load_yaml_config(yaml_path)
        return dataclasses.replace(base, yaml=yaml_cfg)
