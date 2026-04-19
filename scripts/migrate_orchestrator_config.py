#!/usr/bin/env python
"""Deterministic orchestrator.yaml -> (doctarr.yaml, .env) translator.

Secrets (passwords, API keys, usernames) are extracted to .env; structural
config goes to doctarr.yaml.  Run after upgrading to doctarr 0.4.0.

Usage:
    python scripts/migrate_orchestrator_config.py <orchestrator.yaml> <output_dir>

Output:
    <output_dir>/doctarr.yaml  -- structural config (no secrets)
    <output_dir>/.env          -- secrets + connection env vars

Design notes:
    - *_env fields (e.g. password_env: QBT_PASSWORD) are forwarded as-is:
      the env-var *name* is preserved so existing secrets don't need to move.
    - Inline secrets (password: literal) are extracted to .env directly.
    - services block is normalised to APPNAME_URL + APPNAME_API_KEY env vars.
    - Unknown top-level keys are passed through to doctarr.yaml under an
      'unknown_sections' key so nothing is silently dropped.
    - Idempotent: running twice on the same input produces identical output.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Secret field classifiers
# ---------------------------------------------------------------------------

# Inline secret fields: (section, key) -> env-var name
_INLINE_SECRETS: dict[tuple[str, str], str] = {
    ("qbittorrent", "username"): "QBITTORRENT_USERNAME",
    ("qbittorrent", "password"): "QBITTORRENT_PASSWORD",
}

# *_env pointer fields: value is an env-var name, not the secret itself.
# We forward the pointer so the existing env var doesn't need renaming.
_ENV_POINTER_SUFFIXES = ("_env",)

# Known structural (non-secret) top-level sections processed explicitly.
_KNOWN_SECTIONS = {"orchestrator", "qbittorrent", "services", "disk", "vpn", "telegram"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def migrate(
    orchestrator_yaml: "Path | str",
    output_dir: "Path | str",
) -> tuple[Path, Path]:
    """Translate *orchestrator_yaml* to doctarr.yaml + .env in *output_dir*.

    Returns ``(doctarr_yaml_path, env_file_path)``.  Safe to call repeatedly;
    last call wins (files are overwritten, not appended).
    """
    src = Path(orchestrator_yaml)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    raw: dict[str, Any] = yaml.safe_load(src.read_text()) or {}

    env_lines: list[str] = []
    yaml_payload: dict[str, Any] = {}

    # -- orchestrator meta -------------------------------------------------
    if orch := raw.get("orchestrator"):
        yaml_payload["orchestrator"] = dict(orch)

    # -- qbittorrent -------------------------------------------------------
    if qbt := raw.get("qbittorrent"):
        _process_qbittorrent(qbt, env_lines, yaml_payload)

    # -- services (sonarr, radarr, prowlarr, …) ----------------------------
    if services := raw.get("services"):
        _process_services(services, env_lines, yaml_payload)

    # -- disk --------------------------------------------------------------
    if disk := raw.get("disk"):
        _process_disk(disk, env_lines, yaml_payload)

    # -- vpn ---------------------------------------------------------------
    if vpn := raw.get("vpn"):
        yaml_payload.setdefault("orchestrator", {})["vpn"] = dict(vpn)

    # -- telegram ----------------------------------------------------------
    if telegram := raw.get("telegram"):
        _process_telegram(telegram, env_lines, yaml_payload)

    # -- unknown sections → passthrough ------------------------------------
    unknown = {k: v for k, v in raw.items() if k not in _KNOWN_SECTIONS}
    if unknown:
        yaml_payload["unknown_sections"] = unknown

    # -- write outputs -----------------------------------------------------
    yaml_out = out / "doctarr.yaml"
    env_out = out / ".env"

    if yaml_payload:
        yaml_out.write_text(yaml.safe_dump(yaml_payload, sort_keys=False))
    else:
        yaml_out.write_text("# no orchestrator sections to migrate\n")

    env_out.write_text("\n".join(env_lines) + ("\n" if env_lines else ""))

    return yaml_out, env_out


# ---------------------------------------------------------------------------
# Section processors
# ---------------------------------------------------------------------------


def _process_qbittorrent(qbt: dict, env_lines: list[str], yaml_payload: dict) -> None:
    host = qbt.get("host", "")
    port = qbt.get("port", "")
    if host:
        url = f"http://{host}:{port}" if port else f"http://{host}"
        env_lines.append(f"QBITTORRENT_URL={url}")

    if "username" in qbt:
        env_lines.append(f"QBITTORRENT_USERNAME={qbt['username']}")

    # Inline password → env
    if "password" in qbt:
        env_lines.append(f"QBITTORRENT_PASSWORD={qbt['password']}")

    # password_env pointer → forward the env-var name
    if "password_env" in qbt:
        env_lines.append(f"QBITTORRENT_PASSWORD_ENV={qbt['password_env']}")

    # Structural (non-secret) fields survive in yaml
    _SECRET_KEYS = {"username", "password", "password_env", "host", "port"}
    structural = {k: v for k, v in qbt.items() if k not in _SECRET_KEYS}
    if structural:
        yaml_payload.setdefault("orchestrator", {})["qbittorrent"] = structural


def _process_services(services: dict, env_lines: list[str], yaml_payload: dict) -> None:
    """Normalise services block → APPNAME_URL + APPNAME_API_KEY env vars."""
    services_yaml: dict = {}
    for name, cfg in services.items():
        if not isinstance(cfg, dict):
            continue
        upper = name.upper()
        host = cfg.get("host", "")
        port = cfg.get("port", "")
        if host:
            url = f"http://{host}:{port}" if port else f"http://{host}"
            env_lines.append(f"{upper}_URL={url}")

        # api_key_env: forward the env-var name so the actual key stays put
        if api_key_env := cfg.get("api_key_env"):
            env_lines.append(f"{upper}_API_KEY_ENV={api_key_env}")
            # Also emit the resolved name so doctarr Config.from_env picks it up
            env_lines.append(f"# doctarr reads: {api_key_env}")

        # Structural fields (api_version, etc.) survive in yaml
        structural = {
            k: v for k, v in cfg.items() if k not in ("host", "port", "api_key_env")
        }
        if structural:
            services_yaml[name] = structural

    if services_yaml:
        yaml_payload["services"] = services_yaml


def _process_disk(disk: dict, env_lines: list[str], yaml_payload: dict) -> None:
    # Legacy shape: paths list
    if paths := disk.get("paths"):
        env_lines.append(f"DISK_HEALTH_PATHS={','.join(paths)}")

    # Real shape: single monitor_path
    if monitor_path := disk.get("monitor_path"):
        env_lines.append(f"DISK_HEALTH_PATHS={monitor_path}")

    # Thresholds are structural
    structural = {k: v for k, v in disk.items() if k not in ("paths", "monitor_path")}
    if structural:
        yaml_payload.setdefault("orchestrator", {})["disk"] = structural


def _process_telegram(telegram: dict, env_lines: list[str], yaml_payload: dict) -> None:
    # bot_token_env / chat_id_env are pointers; forward them
    if bot_env := telegram.get("bot_token_env"):
        env_lines.append(f"TELEGRAM_BOT_TOKEN_ENV={bot_env}")
    if chat_env := telegram.get("chat_id_env"):
        env_lines.append(f"TELEGRAM_CHAT_ID_ENV={chat_env}")
    # No structural content to put in yaml; section fully expressed via env


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    src, out_dir = argv[1], argv[2]
    yaml_out, env_out = migrate(src, out_dir)
    print(f"Wrote {yaml_out}")
    print(f"Wrote {env_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
