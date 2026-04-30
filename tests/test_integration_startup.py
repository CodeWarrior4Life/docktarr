"""Integration smoke test: verify full scheduler wire-up without hitting the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from docktarr.http_health import HealthState


@pytest.mark.asyncio
async def test_scheduler_starts_with_all_modules_enabled(tmp_path, monkeypatch):
    """All v0.4 modules must register their jobs when given valid YAML config."""
    yaml_path = tmp_path / "docktarr.yaml"
    yaml_path.write_text(
        """
hw_capability:
  enabled: true
  schedule: "0 3 * * *"
  hosts:
    - name: zion
      ssh_ref: zion_sudo
media_container_audit:
  enabled: true
  schedule: "0 */4 * * *"
  containers:
    - name: Plex
      host: zion
      kind: plex
      expected_devices: ["/dev/dri"]
      required_prefs: {HardwareAcceleratedCodecs: "1"}
permission_health:
  enabled: true
  schedule: "0 3 * * *"
  paths:
    - name: "Plex Movies"
      path: "/data/Video/Movies"
      expected_uid: 1026
      expected_gid: 100
      auto_fix: false
      max_drift_pct: 5.0
  fix_host: megacity
  fix_credential_ref: megacity_sudo
"""
    )

    monkeypatch.setenv("PROWLARR_URL", "http://prowlarr.invalid:9696")
    monkeypatch.setenv("PROWLARR_API_KEY", "x")
    monkeypatch.setenv("ZION_SUDO_PASSWORD", "p")
    monkeypatch.setenv("MEGACITY_SUDO_PASSWORD", "p")
    monkeypatch.setenv("DOCTARR_SKIP_NETWORK_INIT", "1")

    from docktarr.main import _build_scheduler_for_test

    result = await _build_scheduler_for_test(yaml_path)
    scheduler, health_state = result[0], result[1]

    try:
        job_ids = {j.id for j in scheduler.get_jobs()}

        # Core indexer jobs (v0.1)
        assert "discovery" in job_ids
        assert "tester" in job_ids
        assert "pruner" in job_ids

        # v0.4 modules
        assert "hw_capability" in job_ids
        assert "media_container_audit" in job_ids
        assert "permissions_health" in job_ids

        # health_state is the correct type
        assert isinstance(health_state, HealthState)

    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


@pytest.mark.asyncio
async def test_scheduler_minimal_config(tmp_path, monkeypatch):
    """Minimal env (no YAML modules) still registers core jobs."""
    monkeypatch.setenv("PROWLARR_URL", "http://prowlarr.invalid:9696")
    monkeypatch.setenv("PROWLARR_API_KEY", "x")
    monkeypatch.setenv("DOCTARR_SKIP_NETWORK_INIT", "1")

    # Point at an empty yaml path (file doesn't exist — load_yaml_config returns defaults)
    yaml_path = tmp_path / "empty.yaml"

    from docktarr.main import _build_scheduler_for_test

    result = await _build_scheduler_for_test(yaml_path)
    scheduler, health_state = result[0], result[1]

    try:
        job_ids = {j.id for j in scheduler.get_jobs()}
        assert "discovery" in job_ids
        assert "tester" in job_ids
        assert "pruner" in job_ids
        assert isinstance(health_state, HealthState)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
