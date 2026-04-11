from datetime import timedelta

import pytest
from doctarr.config import Config, parse_duration


class TestParseDuration:
    def test_seconds(self):
        assert parse_duration("30s") == timedelta(seconds=30)

    def test_minutes(self):
        assert parse_duration("5m") == timedelta(minutes=5)

    def test_hours(self):
        assert parse_duration("6h") == timedelta(hours=6)

    def test_days(self):
        assert parse_duration("1d") == timedelta(days=1)

    def test_plain_number_is_seconds(self):
        assert parse_duration("120") == timedelta(seconds=120)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_duration("abc")


class TestConfig:
    def test_required_fields(self, monkeypatch):
        monkeypatch.setenv("PROWLARR_URL", "http://localhost:9696")
        monkeypatch.setenv("PROWLARR_API_KEY", "test-key")
        cfg = Config.from_env()
        assert cfg.prowlarr_url == "http://localhost:9696"
        assert cfg.prowlarr_api_key == "test-key"

    def test_missing_url_raises(self, monkeypatch):
        monkeypatch.delenv("PROWLARR_URL", raising=False)
        monkeypatch.delenv("PROWLARR_API_KEY", raising=False)
        with pytest.raises(ValueError, match="PROWLARR_URL"):
            Config.from_env()

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.setenv("PROWLARR_URL", "http://localhost:9696")
        monkeypatch.delenv("PROWLARR_API_KEY", raising=False)
        with pytest.raises(ValueError, match="PROWLARR_API_KEY"):
            Config.from_env()

    def test_defaults(self, monkeypatch):
        monkeypatch.setenv("PROWLARR_URL", "http://localhost:9696")
        monkeypatch.setenv("PROWLARR_API_KEY", "test-key")
        cfg = Config.from_env()
        assert cfg.discovery_interval == timedelta(hours=6)
        assert cfg.test_interval == timedelta(hours=2)
        assert cfg.prune_interval == timedelta(hours=1)
        assert cfg.prune_threshold == timedelta(hours=12)
        assert cfg.test_delay == timedelta(seconds=2)
        assert cfg.webhook_url is None
        assert cfg.webhook_events == ["added", "pruned", "digest"]
        assert cfg.digest_time == "08:00"
        assert cfg.log_level == "info"

    def test_custom_intervals(self, monkeypatch):
        monkeypatch.setenv("PROWLARR_URL", "http://localhost:9696")
        monkeypatch.setenv("PROWLARR_API_KEY", "test-key")
        monkeypatch.setenv("DISCOVERY_INTERVAL", "12h")
        monkeypatch.setenv("PRUNE_THRESHOLD", "6h")
        monkeypatch.setenv("WEBHOOK_URL", "https://discord.com/webhook/123")
        cfg = Config.from_env()
        assert cfg.discovery_interval == timedelta(hours=12)
        assert cfg.prune_threshold == timedelta(hours=6)
        assert cfg.webhook_url == "https://discord.com/webhook/123"

    def test_trailing_slash_stripped_from_url(self, monkeypatch):
        monkeypatch.setenv("PROWLARR_URL", "http://localhost:9696/")
        monkeypatch.setenv("PROWLARR_API_KEY", "test-key")
        cfg = Config.from_env()
        assert cfg.prowlarr_url == "http://localhost:9696"
