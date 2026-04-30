from pathlib import Path
import sys
import pytest

# Add scripts/ to sys.path for import
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


def test_migrate_emits_yaml_plus_env(tmp_path):
    orch_yaml = tmp_path / "orchestrator.yaml"
    orch_yaml.write_text("""
orchestrator:
  check_interval: 300
qbittorrent:
  host: gluetun
  port: 8080
  username: admin
  password: xxx
vpn:
  container: gluetun
  healthcheck_url: http://gluetun:8888/v1/openvpn/status
disk:
  paths:
    - /data
  warning_pct: 90
""")
    from migrate_orchestrator_config import migrate

    out_yaml, out_env = migrate(orch_yaml, tmp_path)
    assert Path(out_yaml).exists()
    assert Path(out_env).exists()
    yaml_text = Path(out_yaml).read_text()
    # The YAML should surface docktarr sections; orchestrator sections may be passed through as-is
    # under an 'orchestrator' key or mapped to per-job sections. Either is acceptable if consistent.
    assert (
        "qbittorrent" in yaml_text.lower()
        or "qbit" in yaml_text.lower()
        or "orchestrator" in yaml_text.lower()
    )
    env_text = Path(out_env).read_text()
    assert "QBITTORRENT_USERNAME=admin" in env_text
    assert "QBITTORRENT_PASSWORD=xxx" in env_text


def test_migrate_handles_missing_sections(tmp_path):
    orch_yaml = tmp_path / "orchestrator.yaml"
    orch_yaml.write_text("orchestrator:\n  check_interval: 60\n")
    from migrate_orchestrator_config import migrate

    out_yaml, out_env = migrate(orch_yaml, tmp_path)
    # Should not crash; outputs exist
    assert Path(out_yaml).exists()
    assert Path(out_env).exists()


def test_migrate_sanitizes_secrets_to_env(tmp_path):
    """Secrets (passwords, API keys) should land in .env, not in YAML."""
    orch_yaml = tmp_path / "orchestrator.yaml"
    orch_yaml.write_text("""
qbittorrent:
  username: admin
  password: supersecret123
""")
    from migrate_orchestrator_config import migrate

    out_yaml, out_env = migrate(orch_yaml, tmp_path)
    yaml_text = Path(out_yaml).read_text()
    env_text = Path(out_env).read_text()
    assert "supersecret123" not in yaml_text
    assert "supersecret123" in env_text


def test_migrate_real_shape_services(tmp_path):
    """Test with the real orchestrator config shape: services block, *_env references."""
    orch_yaml = tmp_path / "orchestrator.yaml"
    orch_yaml.write_text("""
orchestrator:
  check_interval: 120
  disk_check_interval: 3600
  daily_digest_hour: 8
  max_auto_fix_attempts: 3
  alert_cooldown: 1800

telegram:
  bot_token_env: TELEGRAM_BOT_TOKEN
  chat_id_env: TELEGRAM_CHAT_ID

qbittorrent:
  host: gluetun
  port: 8082
  username: theblacktruth
  password_env: QBT_PASSWORD
  settings:
    max_connec: 500
    up_limit: 104857600

services:
  sonarr:
    host: 10.0.0.16
    port: 8989
    api_version: v3
    api_key_env: SONARR_API_KEY
  radarr:
    host: 10.0.0.16
    port: 7878
    api_version: v3
    api_key_env: RADARR_API_KEY

disk:
  monitor_path: /monitor/zion
  warn_percent: 90
  critical_percent: 95

vpn:
  min_speed_kbps: 500
  speed_check_window: 300
""")
    from migrate_orchestrator_config import migrate

    out_yaml, out_env = migrate(orch_yaml, tmp_path)
    yaml_text = Path(out_yaml).read_text()
    env_text = Path(out_env).read_text()

    # Secrets must NOT be in yaml
    assert "theblacktruth" not in yaml_text

    # username → env
    assert "QBITTORRENT_USERNAME=theblacktruth" in env_text
    # password_env pointer → env (the env-var name is forwarded as-is)
    assert "QBT_PASSWORD" in env_text

    # Services rendered as env vars
    assert "SONARR_URL=http://10.0.0.16:8989" in env_text
    assert "RADARR_URL=http://10.0.0.16:7878" in env_text
    # api_key_env pointers forwarded
    assert "SONARR_API_KEY" in env_text

    # Structural config survives in yaml
    assert "check_interval" in yaml_text
    assert "warn_percent" in yaml_text or "disk" in yaml_text


def test_migrate_unknown_fields_preserved(tmp_path):
    """Unknown top-level keys should not be silently dropped."""
    orch_yaml = tmp_path / "orchestrator.yaml"
    orch_yaml.write_text("""
orchestrator:
  check_interval: 60
some_future_section:
  foo: bar
  baz: 42
""")
    from migrate_orchestrator_config import migrate

    out_yaml, out_env = migrate(orch_yaml, tmp_path)
    yaml_text = Path(out_yaml).read_text()
    # Unknown sections preserved in yaml (under unknown_sections or passthrough)
    assert "some_future_section" in yaml_text or "foo" in yaml_text


def test_migrate_idempotent(tmp_path):
    """Running migrate twice on same input produces identical output."""
    orch_yaml = tmp_path / "orchestrator.yaml"
    orch_yaml.write_text("""
qbittorrent:
  host: gluetun
  port: 8080
  username: admin
  password: secret
disk:
  monitor_path: /data
  warn_percent: 85
""")
    from migrate_orchestrator_config import migrate

    out_yaml1, out_env1 = migrate(orch_yaml, tmp_path)
    text1 = (Path(out_yaml1).read_text(), Path(out_env1).read_text())

    out_yaml2, out_env2 = migrate(orch_yaml, tmp_path)
    text2 = (Path(out_yaml2).read_text(), Path(out_env2).read_text())

    assert text1 == text2
