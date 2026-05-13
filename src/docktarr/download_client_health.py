"""Download-client health probe for Docktarr.

For every configured ARR app (Sonarr/Radarr/Bookshelf/Readarr/...) walks its
/api/{v}/downloadclient config, lints the host field for literal IPs, and
runs a live /downloadclient/test to confirm reachability. Optional auto_patch
rewrites a stale literal IP back to the VPN container's DNS alias when
getent hosts resolves it from inside the ARR container.

See spec: 02_Projects/Media Library/Specifications/Doctarr - Download Client Health Check.md
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from docktarr.arrclient import ArrClient
from docktarr.docker_manager import DockerManager
from docktarr.notifier import Notifier

if TYPE_CHECKING:
    from docktarr.http_health import HealthState

log = logging.getLogger(__name__)

_HOSTNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
_LITERAL_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


@dataclass(frozen=True)
class DownloadClientHealthConfig:
    vpn_container: str = "gluetun"
    auto_patch: bool = False


def _classify_host(host: str) -> str:
    if _LITERAL_IP_RE.match(host):
        return "literal_ip"
    if _HOSTNAME_RE.match(host):
        return "dns"
    return "unknown"


def _extract_fields(client_cfg: dict) -> dict[str, Any]:
    return {f["name"]: f.get("value") for f in client_cfg.get("fields", []) or []}


async def run_download_client_health(
    arr_clients: dict[str, ArrClient],
    docker_manager: DockerManager | None,
    notifier: Notifier,
    config: DownloadClientHealthConfig,
    *,
    health_state: "HealthState | None" = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []

    current_vpn_ip: str | None = None
    if docker_manager is not None and config.vpn_container:
        try:
            vpn_info = await docker_manager.get_container(config.vpn_container)
            current_vpn_ip = vpn_info.primary_ip
        except LookupError:
            log.warning(
                "download_client_health: vpn_container %r not found",
                config.vpn_container,
            )

    for app_name, client in arr_clients.items():
        try:
            dc_list = await client.get_download_clients()
        except Exception as exc:
            log.warning(
                "download_client_health: %s: failed to list download-clients: %s",
                app_name,
                exc,
            )
            results.append(
                {
                    "app": app_name,
                    "client_id": None,
                    "name": None,
                    "host": None,
                    "port": None,
                    "status": "error",
                    "literal_ip": False,
                    "error": str(exc),
                }
            )
            continue

        for cfg in dc_list:
            if not cfg.get("enable", True):
                continue
            fields = _extract_fields(cfg)
            host = str(fields.get("host", "") or "")
            port = fields.get("port")
            classification = _classify_host(host)
            literal = classification == "literal_ip"

            result: dict[str, Any] = {
                "app": app_name,
                "client_id": cfg.get("id"),
                "name": cfg.get("name"),
                "host": host,
                "port": port,
                "status": "ok",
                "literal_ip": literal,
            }

            if literal:
                await notifier.emit(
                    "dc_health.literal_ip",
                    {
                        "app": app_name,
                        "client_id": cfg.get("id"),
                        "host": host,
                        "suggested_alias": (
                            config.vpn_container
                            if current_vpn_ip and host == current_vpn_ip
                            else None
                        ),
                    },
                )
                result["status"] = "literal_ip"

            ok, status, body = await client.test_download_client(cfg)
            if not ok:
                await notifier.emit(
                    "dc_health.unreachable",
                    {
                        "app": app_name,
                        "client_id": cfg.get("id"),
                        "host": host,
                        "port": port,
                        "test_response": body,
                    },
                )
                if result["status"] == "ok":
                    result["status"] = "unreachable"
                elif result["status"] == "literal_ip":
                    result["status"] = "literal_ip+unreachable"
            result["test_status"] = status
            result["test_body"] = body if not ok else None

            patched_to_dns = False
            if (
                config.auto_patch
                and literal
                and current_vpn_ip is not None
                and host == current_vpn_ip
                and docker_manager is not None
            ):
                try:
                    rc, _out = await docker_manager.exec_run(
                        client.container_name,
                        ["getent", "hosts", config.vpn_container],
                    )
                except Exception as exc:
                    log.warning(
                        "download_client_health: %s: getent verify failed: %s",
                        app_name,
                        exc,
                    )
                    rc = 1
                if rc == 0:
                    patched = await client.put_download_client_host(
                        client_id=cfg["id"],
                        new_host=config.vpn_container,
                    )
                    if patched:
                        await notifier.emit(
                            "dc_health.auto_patched",
                            {
                                "app": app_name,
                                "client_id": cfg.get("id"),
                                "host_before": host,
                                "host_after": config.vpn_container,
                            },
                        )
                        result["status"] = "auto_patched"
                        patched_to_dns = True
                else:
                    log.warning(
                        "download_client_health: %s: getent %s failed (rc=%s) — "
                        "skipping auto_patch",
                        app_name,
                        config.vpn_container,
                        rc,
                    )
            result["auto_patched"] = patched_to_dns

            results.append(result)

    report = {"ts": now.isoformat(), "results": results}
    if health_state is not None:
        health_state.record_dc_health(report)
    return report
