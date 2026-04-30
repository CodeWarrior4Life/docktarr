from pathlib import Path
import textwrap
import pytest
from docktarr.yaml_config import load_yaml_config, YamlConfig


def test_returns_empty_when_file_missing(tmp_path):
    cfg = load_yaml_config(tmp_path / "missing.yaml")
    assert cfg == YamlConfig()


def test_parses_hw_capability_section(tmp_path):
    p = tmp_path / "docktarr.yaml"
    p.write_text(
        textwrap.dedent("""
        hw_capability:
          enabled: true
          schedule: "0 3 * * *"
          hosts:
            - name: zion
              ssh_ref: zion_sudo
    """)
    )
    cfg = load_yaml_config(p)
    assert cfg.hw_capability is not None
    assert cfg.hw_capability.enabled is True
    assert cfg.hw_capability.schedule == "0 3 * * *"
    assert len(cfg.hw_capability.hosts) == 1
    assert cfg.hw_capability.hosts[0].name == "zion"
    assert cfg.hw_capability.hosts[0].ssh_ref == "zion_sudo"


def test_parses_media_container_audit_section(tmp_path):
    p = tmp_path / "docktarr.yaml"
    p.write_text(
        textwrap.dedent("""
        media_container_audit:
          enabled: true
          schedule: "0 */4 * * *"
          containers:
            - name: Plex
              host: zion
              kind: plex
              expected_devices: ["/dev/dri"]
              auto_remediate: false
              pref_file: "/config/Library/Application Support/Plex Media Server/Preferences.xml"
              required_prefs:
                HardwareAcceleratedCodecs: "1"
    """)
    )
    cfg = load_yaml_config(p)
    assert cfg.media_container_audit is not None
    c = cfg.media_container_audit.containers[0]
    assert c.name == "Plex"
    assert c.kind == "plex"
    assert c.expected_devices == ["/dev/dri"]
    assert c.required_prefs == {"HardwareAcceleratedCodecs": "1"}


def test_parses_permission_health_section(tmp_path):
    p = tmp_path / "docktarr.yaml"
    p.write_text(
        textwrap.dedent("""
        permission_health:
          enabled: true
          schedule: "0 3 * * *"
          paths:
            - name: "Plex Movies"
              path: "/data/Video/Movies"
              expected_uid: 1026
              expected_gid: 100
              expected_mode_min: "0644"
              auto_fix: false
              max_drift_pct: 5.0
          fix_host: "megacity"
          fix_path_translation:
            "/data/Video": "/share/CACHEDEV1_DATA/Media/Video"
          fix_credential_ref: "megacity_sudo"
    """)
    )
    cfg = load_yaml_config(p)
    ph = cfg.permission_health
    assert ph.paths[0].expected_uid == 1026
    assert ph.paths[0].auto_fix is False
    assert ph.fix_path_translation["/data/Video"] == "/share/CACHEDEV1_DATA/Media/Video"


def test_raises_on_invalid_yaml(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("this: [: not valid")
    with pytest.raises(ValueError):
        load_yaml_config(p)
