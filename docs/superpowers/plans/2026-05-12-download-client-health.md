---
type: plan
project: Media Library
component: docktarr
title: Doctarr - Download Client Health Check Implementation Plan
status: ready
created: 2026-05-12
session: 114
spec: "[[Doctarr - Download Client Health Check]]"
target_version: 0.7.1
branch: feat/download-client-health
pvd_conformant: true
---

# Download Client Health Check — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `download_client_health` docktarr module that probes every ARR app's configured download-client every 5 min, statically lints for literal-IP host fields, performs a live `/downloadclient/test`, optionally rewrites stale literal IPs back to the VPN container's DNS alias, and re-probes immediately after a `vpn.restart_finished` event.

**Architecture:** New module follows the shape of `vpn_health.py` / `qbit_health.py` [V:file-line:C:/Dev/docktarr/src/docktarr/vpn_health.py:74] [V:file-line:C:/Dev/docktarr/src/docktarr/qbit_health.py:78]: single async `run_*` function, scheduler tick, takes `arr_clients` + `DockerManager` + `Notifier` + config + optional `HealthState`. Static lint runs first (cheap regex); failed-lint or DNS-host items proceed to live test. Auto-patch is opt-in via env var, gated on `getent hosts` from inside the ARR container via `DockerManager.exec_run` [V:file-line:C:/Dev/docktarr/src/docktarr/docker_manager.py:94]. Snapshot lives on `HealthState.dc_health` (codebase convention [V:file-line:C:/Dev/docktarr/src/docktarr/http_health.py:12]); surfaced via `/health/download_clients`. Spec's "StateStore slot" wording corrected to HealthState — established pattern [R: spec adapted to codebase].

**Tech stack:** Python 3.13, httpx, docker, APScheduler, aiohttp, pytest-asyncio. No new deps. [U]

**Baseline:** docktarr 0.7.0 on `main`, 206 tests passing [V:cmd-exec:baseline-tests]. Target: 0.7.1 on `feat/download-client-health`, 214 tests. [U]

**MAM safety:** Configuration-only writes; no torrent state mutation; compliant with all 7 MAM Hard Rules [R: spec section "Followups from S111"].

---

## Verify-blocks

### baseline-tests
```bash
cd C:/Dev/docktarr && python -m pytest --collect-only -q 2>&1 | tail -1
# Expected: '206 tests collected'
```

---

## File Structure [U]

- **Create** `C:/Dev/docktarr/src/docktarr/download_client_health.py`
- **Create** `C:/Dev/docktarr/tests/test_download_client_health.py`
- **Modify** `C:/Dev/docktarr/src/docktarr/arrclient.py` — add `get_download_clients` / `test_download_client` / `put_download_client_host`
- **Modify** `C:/Dev/docktarr/tests/test_arrclient.py`
- **Modify** `C:/Dev/docktarr/src/docktarr/docker_manager.py` — add `ip_addresses` field on `ContainerInfo`
- **Modify** `C:/Dev/docktarr/tests/test_docker_manager.py`
- **Modify** `C:/Dev/docktarr/src/docktarr/http_health.py` — `dc_health` slot + route
- **Modify** `C:/Dev/docktarr/src/docktarr/notifier.py` — `dc_health.*` templates
- **Modify** `C:/Dev/docktarr/src/docktarr/vpn_health.py` — emit `vpn.restart_finished`
- **Modify** `C:/Dev/docktarr/tests/test_vpn_health.py`
- **Modify** `C:/Dev/docktarr/src/docktarr/main.py` — scheduler wiring
- **Modify** `C:/Dev/docktarr/tests/test_integration_startup.py`
- **Modify** `C:/Dev/docktarr/src/docktarr/config.py` — default webhook events
- **Modify** `C:/Dev/docktarr/pyproject.toml`, `CHANGELOG.md`, `README.md`

---

## Task 0: Baseline + branch

- [ ] **Step 1: Verify clean baseline** [U]

```bash
cd C:/Dev/docktarr && git status
```
Expected: `On branch main`, clean.

- [ ] **Step 2: Verify 206 tests collect** [U]

```bash
cd C:/Dev/docktarr && python -m pytest --collect-only -q 2>&1 | tail -1
```
Expected: `206 tests collected`.

- [ ] **Step 3: Create branch** [U]

```bash
cd C:/Dev/docktarr && git checkout -b feat/download-client-health
```

- [ ] **Step 4: Empty marker commit** [U]

```bash
cd C:/Dev/docktarr && git commit --allow-empty -m "chore: branch marker for feat/download-client-health"
```

---

## Task 1: ArrClient downloadclient API methods

Servarr-family apps expose `GET /api/{v}/downloadclient`, `POST /api/{v}/downloadclient/test`, `PUT /api/{v}/downloadclient/{id}` [U]. `ArrClient._api_version()` returns `"v3"` for Sonarr/Radarr and `"v1"` for Readarr/Bookshelf [V:file-line:C:/Dev/docktarr/src/docktarr/arrclient.py:36]; endpoint paths are identical across versions [U].

**Files:** modify `src/docktarr/arrclient.py`, `tests/test_arrclient.py`. [U]

- [ ] **Step 1: Failing test for `get_download_clients`** [U]

Append to `tests/test_arrclient.py`: [U]

```python
import httpx
import pytest

from docktarr.arrclient import ArrClient
from docktarr.config import ArrAppConfig


def _client_with_handler(handler):
    app = ArrAppConfig(url="http://sonarr:8989", api_key="abc", name="Sonarr")
    c = ArrClient(app)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


@pytest.mark.asyncio
async def test_get_download_clients_returns_list():
    payload = [
        {"id": 1, "name": "qBittorrent", "enable": True,
         "fields": [{"name": "host", "value": "gluetun"},
                    {"name": "port", "value": 8082}]},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v3/downloadclient"
        assert req.headers["X-Api-Key"] == "abc"
        return httpx.Response(200, json=payload)

    c = _client_with_handler(handler)
    result = await c.get_download_clients()
    assert result == payload
```

- [ ] **Step 2: Run; verify FAIL** [U]

```bash
cd C:/Dev/docktarr && python -m pytest tests/test_arrclient.py::test_get_download_clients_returns_list -v
```
Expected: `AttributeError: 'ArrClient' object has no attribute 'get_download_clients'`.

- [ ] **Step 3: Implement `get_download_clients`** [U]

Add after `get_queue` in `src/docktarr/arrclient.py`: [U]

```python
    async def get_download_clients(self) -> list[dict]:
        v = self._api_version()
        resp = await self._client.get(
            f"{self._url}/api/{v}/downloadclient",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()
```

- [ ] **Step 4: Run; verify PASS** [U]

- [ ] **Step 5: Failing tests for `test_download_client`** [U]

```python
@pytest.mark.asyncio
async def test_test_download_client_pass():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v3/downloadclient/test"
        assert req.method == "POST"
        return httpx.Response(200, json={})

    c = _client_with_handler(handler)
    ok, status, body = await c.test_download_client({"name": "qBittorrent", "fields": []})
    assert ok is True
    assert status == 200
    assert body == "{}"


@pytest.mark.asyncio
async def test_test_download_client_fail_returns_body():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json=[{"propertyName": "host", "errorMessage": "Unable to connect"}],
        )

    c = _client_with_handler(handler)
    ok, status, body = await c.test_download_client({"name": "x"})
    assert ok is False
    assert status == 400
    assert "Unable to connect" in body
```

- [ ] **Step 6: Run; verify FAIL** [U]

- [ ] **Step 7: Implement `test_download_client`** [U]

```python
    async def test_download_client(
        self, client_config: dict
    ) -> tuple[bool, int, str]:
        v = self._api_version()
        try:
            resp = await self._client.post(
                f"{self._url}/api/{v}/downloadclient/test",
                json=client_config,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            return False, 0, str(exc)
        return resp.status_code == 200, resp.status_code, resp.text
```

- [ ] **Step 8: Run; verify PASS** [U]

- [ ] **Step 9: Failing test for `put_download_client_host`** [U]

```python
@pytest.mark.asyncio
async def test_put_download_client_host_rewrites_host_field():
    current = {
        "id": 1, "name": "qBittorrent", "enable": True,
        "fields": [{"name": "host", "value": "172.29.0.2"},
                   {"name": "port", "value": 8082}],
    }
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=current)
        captured["body"] = req.read()
        return httpx.Response(202)

    c = _client_with_handler(handler)
    ok = await c.put_download_client_host(client_id=1, new_host="gluetun")
    assert ok is True
    import json
    body = json.loads(captured["body"])
    fields = {f["name"]: f["value"] for f in body["fields"]}
    assert fields["host"] == "gluetun"
    assert fields["port"] == 8082
```

- [ ] **Step 10: Run; verify FAIL** [U]

- [ ] **Step 11: Implement `put_download_client_host`** [U]

```python
    async def put_download_client_host(
        self, client_id: int, new_host: str
    ) -> bool:
        v = self._api_version()
        get = await self._client.get(
            f"{self._url}/api/{v}/downloadclient/{client_id}",
            headers=self._headers(),
        )
        if get.status_code != 200:
            return False
        body = get.json()
        for f in body.get("fields", []):
            if f.get("name") == "host":
                f["value"] = new_host
        put = await self._client.put(
            f"{self._url}/api/{v}/downloadclient/{client_id}",
            json=body,
            headers=self._headers(),
        )
        return put.status_code < 400
```

- [ ] **Step 12: Run; verify PASS** [U]

- [ ] **Step 13: Commit** [U]

```bash
cd C:/Dev/docktarr && git add src/docktarr/arrclient.py tests/test_arrclient.py
git commit -m "feat(arrclient): add downloadclient GET/test/PUT methods"
```

---

## Task 2: ContainerInfo.ip_addresses

Docker exposes container IPs at `c.attrs["NetworkSettings"]["Networks"][<network>]["IPAddress"]` [U]. Extend `ContainerInfo` [V:file-line:C:/Dev/docktarr/src/docktarr/docker_manager.py:39] with `ip_addresses: dict[str,str]` + a `primary_ip` property. [U]

**Files:** modify `src/docktarr/docker_manager.py`, `tests/test_docker_manager.py`. [U]

- [ ] **Step 1: Failing test** [U]

Add to `tests/test_docker_manager.py`: [U]

```python
@pytest.mark.asyncio
async def test_get_container_populates_ip_addresses():
    from unittest.mock import MagicMock
    from docktarr.docker_manager import DockerManager

    fake = MagicMock()
    fake.name = "gluetun"
    fake.status = "running"
    fake.attrs = {
        "Config": {"Env": [], "Image": "qmcgaw/gluetun:latest"},
        "HostConfig": {"Devices": []},
        "State": {"ExitCode": 0, "StartedAt": "2026-05-09T18:15:00Z"},
        "NetworkSettings": {
            "Networks": {
                "arr_default": {"IPAddress": "172.29.0.7"},
                "bridge": {"IPAddress": ""},
            }
        },
    }
    client = MagicMock()
    client.containers.get.return_value = fake

    dm = DockerManager(_client=client)
    info = await dm.get_container("gluetun")
    assert info.ip_addresses == {"arr_default": "172.29.0.7"}
    assert info.primary_ip == "172.29.0.7"
```

- [ ] **Step 2: Run; verify FAIL** [U]

- [ ] **Step 3: Implement** [U]

Extend `ContainerInfo`: [U]

```python
@dataclass(frozen=True)
class ContainerInfo:
    name: str
    status: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    device_paths: list[str] = field(default_factory=list)
    exit_code: int | None = None
    started_at: datetime | None = None
    ip_addresses: dict[str, str] = field(default_factory=dict)

    @property
    def primary_ip(self) -> str | None:
        for ip in self.ip_addresses.values():
            if ip:
                return ip
        return None
```

In `get_container` after `started_at = _parse_started_at(...)`: [U]

```python
        networks = (c.attrs.get("NetworkSettings", {}) or {}).get("Networks", {}) or {}
        ip_addresses = {
            net: (data or {}).get("IPAddress", "")
            for net, data in networks.items()
            if (data or {}).get("IPAddress")
        }
```

Pass `ip_addresses=ip_addresses` to the `ContainerInfo(...)` constructor. [U]

- [ ] **Step 4: Run; verify PASS + no regressions** [U]

```bash
cd C:/Dev/docktarr && python -m pytest tests/test_docker_manager.py -v
```

- [ ] **Step 5: Commit** [U]

```bash
cd C:/Dev/docktarr && git add src/docktarr/docker_manager.py tests/test_docker_manager.py
git commit -m "feat(docker_manager): expose container ip_addresses + primary_ip"
```

---

## Task 3: HealthState dc_health slot + /health/download_clients

Mirror existing slots [V:file-line:C:/Dev/docktarr/src/docktarr/http_health.py:35]. Add `/health/download_clients` route alongside the others [V:file-line:C:/Dev/docktarr/src/docktarr/http_health.py:74]. [U]

**Files:** modify `src/docktarr/http_health.py`, `tests/test_http_health.py`. [U]

- [ ] **Step 1: Read `tests/test_http_health.py`** to mirror its style (port allocation, async pattern). [U]

- [ ] **Step 2: Failing test** [U]

Add to `tests/test_http_health.py` (adapt port-allocation style from existing tests): [U]

```python
@pytest.mark.asyncio
async def test_dc_health_endpoint_returns_recorded_report():
    import aiohttp
    from docktarr.http_health import HealthServer, HealthState

    state = HealthState()
    sample = {
        "ts": "2026-05-12T23:34:00Z",
        "results": [{"app": "Sonarr", "client_id": 1, "name": "qBittorrent",
                     "host": "gluetun", "port": 8082, "status": "ok",
                     "literal_ip": False}],
    }
    state.record_dc_health(sample)
    assert state.dc_health == sample
    assert state.snapshot()["download_clients"] == sample

    server = HealthServer(state=state, host="127.0.0.1", port=18891)
    await server.start()
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:18891/health/download_clients") as r:
                assert r.status == 200
                assert await r.json() == sample
    finally:
        await server.stop()
```

If port 18891 conflicts in CI, mirror whatever port-picker is already used in this file. [U]

- [ ] **Step 3: Run; verify FAIL** [U]

Expected: `AttributeError: 'HealthState' object has no attribute 'record_dc_health'`.

- [ ] **Step 4: Implement** [U]

In `src/docktarr/http_health.py` add field on `HealthState`: [U]

```python
    dc_health: dict | None = None
```

And method: [U]

```python
    def record_dc_health(self, report: dict) -> None:
        self.dc_health = report
```

Extend `snapshot()`: [U]

```python
            "download_clients": self.dc_health,
```

In `HealthServer.start`, add route: [U]

```python
        app.router.add_get("/health/download_clients", self._dc_health)
```

And handler: [U]

```python
    async def _dc_health(self, req):
        return web.json_response(self._state.dc_health)
```

- [ ] **Step 5: Run; verify PASS** [U]

- [ ] **Step 6: Commit** [U]

```bash
cd C:/Dev/docktarr && git add src/docktarr/http_health.py tests/test_http_health.py
git commit -m "feat(http_health): /health/download_clients + dc_health slot"
```

---

## Task 4: download_client_health module — scenarios 1 + 2

Static-lint regexes — hostname `^[a-zA-Z][a-zA-Z0-9_-]*$`, literal IP `^\d+\.\d+\.\d+\.\d+$` [R: spec "Per ARR app, per enabled download client" section]. [U]

**Files:** create `src/docktarr/download_client_health.py` + `tests/test_download_client_health.py`. [U]

- [ ] **Step 1: Create module file** [U]

Write `src/docktarr/download_client_health.py`: [U]

```python
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
                app_name, exc,
            )
            results.append({
                "app": app_name, "client_id": None, "name": None,
                "host": None, "port": None, "status": "error",
                "literal_ip": False, "error": str(exc),
            })
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

            results.append(result)

    report = {"ts": now.isoformat(), "results": results}
    if health_state is not None:
        health_state.record_dc_health(report)
    return report
```

- [ ] **Step 2: Create test file with scenarios 1, 2** [U]

Write `tests/test_download_client_health.py`: [U]

```python
"""Tests for download_client_health — 8 spec scenarios."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from docktarr.docker_manager import ContainerInfo
from docktarr.download_client_health import (
    DownloadClientHealthConfig,
    _classify_host,
    run_download_client_health,
)
from docktarr.notifier import Notifier


def _make_notifier() -> tuple[Notifier, list[dict]]:
    events: list[dict] = []

    class Capturing(Notifier):
        async def emit(self, event, payload):
            events.append({"event": event, "payload": payload})

    transport = httpx.MockTransport(lambda r: httpx.Response(204))
    n = Capturing(httpx.AsyncClient(transport=transport),
                  webhook_url=None, enabled_events=[])
    return n, events


def _make_arr_client(*, name: str = "Sonarr", host: str = "gluetun",
                     port: int = 8082, client_id: int = 1,
                     test_result: tuple[bool, int, str] = (True, 200, "{}")):
    c = MagicMock()
    c.name = name
    c.container_name = name.lower()
    c.get_download_clients = AsyncMock(return_value=[
        {"id": client_id, "name": "qBittorrent", "enable": True,
         "fields": [{"name": "host", "value": host},
                    {"name": "port", "value": port}]},
    ])
    c.test_download_client = AsyncMock(return_value=test_result)
    c.put_download_client_host = AsyncMock(return_value=True)
    return c


def _make_docker(*, vpn_ip: str | None = "172.29.0.7",
                 vpn_container_name: str = "gluetun") -> MagicMock:
    dm = MagicMock()
    if vpn_ip is None:
        dm.get_container = AsyncMock(side_effect=LookupError("not found"))
    else:
        info = ContainerInfo(
            name=vpn_container_name, status="running",
            image="qmcgaw/gluetun:latest",
            ip_addresses={"arr_default": vpn_ip},
        )
        dm.get_container = AsyncMock(return_value=info)
    dm.exec_run = AsyncMock(return_value=(0, ""))
    return dm


_DEFAULT_CFG = DownloadClientHealthConfig(vpn_container="gluetun", auto_patch=False)


def test_classify_host_recognizes_dns_literal_ip_unknown():
    assert _classify_host("gluetun") == "dns"
    assert _classify_host("172.29.0.7") == "literal_ip"
    assert _classify_host("gluetun.local") == "unknown"
    assert _classify_host("") == "unknown"


@pytest.mark.asyncio
async def test_scenario_1_all_dns_no_events():
    sonarr = _make_arr_client(name="Sonarr", host="gluetun")
    radarr = _make_arr_client(name="Radarr", host="gluetun")
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    report = await run_download_client_health(
        {"Sonarr": sonarr, "Radarr": radarr},
        docker_manager=dm, notifier=notifier, config=_DEFAULT_CFG,
    )

    assert events == []
    assert all(r["status"] == "ok" for r in report["results"])
    assert all(r["literal_ip"] is False for r in report["results"])


@pytest.mark.asyncio
async def test_scenario_2_literal_ip_current_emits_literal_ip_with_alias():
    sonarr = _make_arr_client(name="Sonarr", host="172.29.0.7")
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr}, docker_manager=dm, notifier=notifier, config=_DEFAULT_CFG,
    )

    assert len(events) == 1
    assert events[0]["event"] == "dc_health.literal_ip"
    assert events[0]["payload"]["host"] == "172.29.0.7"
    assert events[0]["payload"]["suggested_alias"] == "gluetun"
```

- [ ] **Step 3: Run; verify PASS** [U]

```bash
cd C:/Dev/docktarr && python -m pytest tests/test_download_client_health.py -v
```
Expected: 3 passed.

- [ ] **Step 4: Commit** [U]

```bash
cd C:/Dev/docktarr && git add src/docktarr/download_client_health.py tests/test_download_client_health.py
git commit -m "feat(download_client_health): module skeleton + static IP lint (scenarios 1, 2)"
```

---

## Task 5: Live test probe + reachability events (scenarios 3, 6, 7)

After lint, call `client.test_download_client(cfg)` per enabled DC. On failure emit `dc_health.unreachable` with `{app, client_id, host, port, test_response}` [R: spec "Emitted events"]. A literal-IP host can also be unreachable (scenario 3) — both events fire. [U]

- [ ] **Step 1: Failing tests** [U]

Append to `tests/test_download_client_health.py`: [U]

```python
@pytest.mark.asyncio
async def test_scenario_3_stale_literal_ip_emits_both_literal_and_unreachable():
    sonarr = _make_arr_client(
        name="Sonarr", host="172.29.0.2",
        test_result=(False, 400, '[{"errorMessage":"Unable to connect"}]'),
    )
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr}, docker_manager=dm, notifier=notifier, config=_DEFAULT_CFG,
    )

    event_names = [e["event"] for e in events]
    assert "dc_health.literal_ip" in event_names
    assert "dc_health.unreachable" in event_names
    unreachable = next(e for e in events if e["event"] == "dc_health.unreachable")
    assert unreachable["payload"]["host"] == "172.29.0.2"
    assert "Unable to connect" in unreachable["payload"]["test_response"]


@pytest.mark.asyncio
async def test_scenario_6_qbit_down_emits_unreachable_dns_host():
    sonarr = _make_arr_client(
        name="Sonarr", host="gluetun",
        test_result=(False, 400,
                     '[{"errorMessage":"Unable to connect to qBittorrent"}]'),
    )
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr}, docker_manager=dm, notifier=notifier, config=_DEFAULT_CFG,
    )

    event_names = [e["event"] for e in events]
    assert "dc_health.unreachable" in event_names
    assert "dc_health.literal_ip" not in event_names


@pytest.mark.asyncio
async def test_scenario_7_wrong_creds_emits_unreachable_with_auth_body():
    sonarr = _make_arr_client(
        name="Sonarr", host="gluetun",
        test_result=(False, 200,
                     '[{"errorMessage":"Failed to authenticate"}]'),
    )
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr}, docker_manager=dm, notifier=notifier, config=_DEFAULT_CFG,
    )

    unreachable = [e for e in events if e["event"] == "dc_health.unreachable"]
    assert len(unreachable) == 1
    assert "authenticate" in unreachable[0]["payload"]["test_response"].lower()
```

- [ ] **Step 2: Run; verify all three FAIL** [U]

- [ ] **Step 3: Implement live test** [U]

In `download_client_health.py`, inside the per-`cfg` loop, replace the trailing `results.append(result)` with: [U]

```python
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

            results.append(result)
```

- [ ] **Step 4: Run; verify PASS** [U]

- [ ] **Step 5: Commit** [U]

```bash
cd C:/Dev/docktarr && git add src/docktarr/download_client_health.py tests/test_download_client_health.py
git commit -m "feat(download_client_health): live test probe + unreachable (scenarios 3, 6, 7)"
```

---

## Task 6: Auto-patch with DNS verify (scenarios 4, 5)

When `auto_patch=True` AND host is a literal IP matching current `vpn_container.primary_ip` AND `docker_manager.exec_run(arr_container, ["getent","hosts",vpn_container])` exits 0, rewrite the DC host. Emit `dc_health.auto_patched`. `getent` failure → no rewrite [R: spec "Auto-heal behavior (gated)"]. [U]

- [ ] **Step 1: Failing tests** [U]

```python
_AUTOPATCH_CFG = DownloadClientHealthConfig(vpn_container="gluetun", auto_patch=True)


@pytest.mark.asyncio
async def test_scenario_4_auto_patch_happy_path_rewrites_host():
    sonarr = _make_arr_client(name="Sonarr", host="172.29.0.7",
                              test_result=(False, 400, "Unable to connect"))
    dm = _make_docker(vpn_ip="172.29.0.7")
    dm.exec_run = AsyncMock(return_value=(0, "172.29.0.7 gluetun"))
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr}, docker_manager=dm, notifier=notifier, config=_AUTOPATCH_CFG,
    )

    sonarr.put_download_client_host.assert_awaited_once_with(client_id=1, new_host="gluetun")
    patched = [e for e in events if e["event"] == "dc_health.auto_patched"]
    assert len(patched) == 1
    assert patched[0]["payload"]["host_before"] == "172.29.0.7"
    assert patched[0]["payload"]["host_after"] == "gluetun"


@pytest.mark.asyncio
async def test_scenario_5_auto_patch_dns_broken_does_not_patch():
    sonarr = _make_arr_client(name="Sonarr", host="172.29.0.7",
                              test_result=(False, 400, "Unable to connect"))
    dm = _make_docker(vpn_ip="172.29.0.7")
    dm.exec_run = AsyncMock(return_value=(2, ""))
    notifier, events = _make_notifier()

    await run_download_client_health(
        {"Sonarr": sonarr}, docker_manager=dm, notifier=notifier, config=_AUTOPATCH_CFG,
    )

    sonarr.put_download_client_host.assert_not_awaited()
    assert not any(e["event"] == "dc_health.auto_patched" for e in events)
    assert any(e["event"] == "dc_health.unreachable" for e in events)
```

- [ ] **Step 2: Run; verify FAIL** [U]

- [ ] **Step 3: Implement auto-patch** [U]

After the `result["test_body"] = ...` line and BEFORE `results.append(result)`, insert: [U]

```python
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
                        app_name, exc,
                    )
                    rc = 1
                if rc == 0:
                    patched = await client.put_download_client_host(
                        client_id=cfg["id"], new_host=config.vpn_container,
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
                        app_name, config.vpn_container, rc,
                    )
            result["auto_patched"] = patched_to_dns
```

- [ ] **Step 4: Run; verify PASS** [U]

- [ ] **Step 5: Commit** [U]

```bash
cd C:/Dev/docktarr && git add src/docktarr/download_client_health.py tests/test_download_client_health.py
git commit -m "feat(download_client_health): auto_patch with DNS verify (scenarios 4, 5)"
```

---

## Task 7: vpn.restart_finished emission

`vpn_health` currently emits `vpn.restarted` after restart [V:file-line:C:/Dev/docktarr/src/docktarr/vpn_health.py:110]. Add a follow-up `vpn.restart_finished` so subscribers can react. [U]

- [ ] **Step 1: Failing test** [U]

Add to `tests/test_vpn_health.py`: [U]

```python
@pytest.mark.asyncio
async def test_container_exited_also_emits_restart_finished():
    http = _make_http_client()
    dm = _make_docker(_make_container(status="exited", exit_code=1))
    notifier, events = _make_notifier()

    await run_vpn_health(http, dm, notifier, _DEFAULT_CONFIG)

    event_names = [e["event"] for e in events]
    assert "vpn.restarted" in event_names
    assert "vpn.restart_finished" in event_names
    finished = next(e for e in events if e["event"] == "vpn.restart_finished")
    assert finished["payload"]["container_name"] == "gluetun"
```

- [ ] **Step 2: Run; verify FAIL** [U]

- [ ] **Step 3: Implement** [U]

In `src/docktarr/vpn_health.py`, after the existing `await notifier.emit("vpn.restarted", {...})` call inside the `if info.status != "running":` branch, insert ABOVE the existing `return`: [U]

```python
        await notifier.emit(
            "vpn.restart_finished",
            {"container_name": config.container_name},
        )
```

- [ ] **Step 4: Run; verify PASS** [U]

- [ ] **Step 5: Commit** [U]

```bash
cd C:/Dev/docktarr && git add src/docktarr/vpn_health.py tests/test_vpn_health.py
git commit -m "feat(vpn_health): emit vpn.restart_finished after restart"
```

---

## Task 8: Scenario 8 — new-member discovery

Contract test: any ARR app in `arr_clients` is iterated and probed [U].

- [ ] **Step 1: Add test** [U]

Append to `tests/test_download_client_health.py`: [U]

```python
@pytest.mark.asyncio
async def test_scenario_8_new_member_discovered_and_probed():
    sonarr = _make_arr_client(name="Sonarr", host="gluetun")
    sonarr_anime = _make_arr_client(name="Sonarr-Anime", host="gluetun")
    dm = _make_docker(vpn_ip="172.29.0.7")
    notifier, events = _make_notifier()

    report = await run_download_client_health(
        {"Sonarr": sonarr, "Sonarr-Anime": sonarr_anime},
        docker_manager=dm, notifier=notifier, config=_DEFAULT_CFG,
    )

    app_names = {r["app"] for r in report["results"]}
    assert app_names == {"Sonarr", "Sonarr-Anime"}
    sonarr.get_download_clients.assert_awaited_once()
    sonarr_anime.get_download_clients.assert_awaited_once()
    sonarr.test_download_client.assert_awaited_once()
    sonarr_anime.test_download_client.assert_awaited_once()
```

- [ ] **Step 2: Run; expect PASS** [U]

- [ ] **Step 3: Commit** [U]

```bash
cd C:/Dev/docktarr && git add tests/test_download_client_health.py
git commit -m "test(download_client_health): scenario 8 new-member discovery"
```

---

## Task 9: Notifier templates + default webhook_events

Notifier dispatches by template lookup [V:file-line:C:/Dev/docktarr/src/docktarr/notifier.py:102]. Default webhook events live in `Config.from_env` [V:file-line:C:/Dev/docktarr/src/docktarr/config.py:99]. [U]

- [ ] **Step 1: Failing test** [U]

Add to `tests/test_notifier.py`: [U]

```python
def test_dc_health_templates_present():
    from docktarr.notifier import _TEMPLATES
    for event in (
        "dc_health.literal_ip",
        "dc_health.unreachable",
        "dc_health.auto_patched",
        "dc_health.skipped_no_credentials",
    ):
        assert event in _TEMPLATES
```

- [ ] **Step 2: Run; verify FAIL** [U]

- [ ] **Step 3: Add templates** [U]

In `_TEMPLATES` dict in `src/docktarr/notifier.py`: [U]

```python
    "dc_health.literal_ip": (
        "**[Docktarr]** {app} download-client {client_id} uses literal IP "
        "**{host}** (suggested alias: {suggested_alias})"
    ),
    "dc_health.unreachable": (
        "**[Docktarr]** {app} download-client UNREACHABLE: host={host} "
        "port={port} — {test_response}"
    ),
    "dc_health.auto_patched": (
        "**[Docktarr]** {app} download-client {client_id} auto-patched: "
        "host {host_before} -> {host_after}"
    ),
    "dc_health.skipped_no_credentials": (
        "**[Docktarr]** {app} download-client probe skipped: no API credentials"
    ),
```

- [ ] **Step 4: Extend default `WEBHOOK_EVENTS`** [U]

In `src/docktarr/config.py` `Config.from_env`, append `,dc_health.literal_ip,dc_health.unreachable,dc_health.auto_patched` to the `WEBHOOK_EVENTS` default string. [U]

- [ ] **Step 5: Run; verify PASS** [U]

```bash
cd C:/Dev/docktarr && python -m pytest tests/test_notifier.py tests/test_config.py -v
```

- [ ] **Step 6: Commit** [U]

```bash
cd C:/Dev/docktarr && git add src/docktarr/notifier.py src/docktarr/config.py tests/test_notifier.py
git commit -m "feat(notifier): dc_health.* templates + default webhook events"
```

---

## Task 10: main.py scheduler wiring + vpn.restart_finished bridge

Wire `run_download_client_health` next to `arr_services` [V:file-line:C:/Dev/docktarr/src/docktarr/main.py:349]. Env vars: `DC_HEALTH_ENABLED` (default true), `DC_HEALTH_INTERVAL` (default 5m), `DC_HEALTH_VPN_CONTAINER` (default gluetun), `DC_HEALTH_AUTO_PATCH` (default false) [U]. Hook `vpn.restart_finished` → out-of-band tick via a local notifier-wrap [U].

- [ ] **Step 1: Read existing `_build_scheduler_for_test`** in `src/docktarr/main.py` to identify the precise insertion point [U] — must come AFTER `arr_clients` is populated but BEFORE `vpn_health` block (the vpn wrapper will reference `_dc_health_job`). [U]

- [ ] **Step 2: Insert download_client_health block** (place before the existing `--- vpn_health ---` block): [U]

```python
    # --- download_client_health (S114) ---
    dc_health_enabled = os.environ.get(
        "DC_HEALTH_ENABLED", "true"
    ).strip().lower() not in ("0", "false", "no")
    _dc_health_job = None
    if dc_health_enabled and arr_clients:
        from docktarr.download_client_health import (
            DownloadClientHealthConfig,
            run_download_client_health,
        )

        if docker_mgr is None:
            docker_mgr = DockerManager()

        dc_vpn_container = os.environ.get("DC_HEALTH_VPN_CONTAINER", "gluetun").strip()
        dc_auto_patch = os.environ.get(
            "DC_HEALTH_AUTO_PATCH", "false"
        ).strip().lower() not in ("0", "false", "no", "")
        dc_health_cfg = DownloadClientHealthConfig(
            vpn_container=dc_vpn_container,
            auto_patch=dc_auto_patch,
        )

        async def _dc_health_job():
            await run_download_client_health(
                arr_clients,
                docker_mgr,
                notifier,
                dc_health_cfg,
                health_state=health_state,
            )

        dc_health_interval = os.environ.get("DC_HEALTH_INTERVAL", "5m")
        scheduler.add_job(
            _dc_health_job,
            "interval",
            seconds=parse_duration(dc_health_interval).total_seconds(),
            id="download_client_health",
            next_run_time=datetime.now(timezone.utc),
        )
        log.info(
            "download_client_health enabled (vpn=%s, auto_patch=%s, interval=%s, apps=%s)",
            dc_vpn_container, dc_auto_patch, dc_health_interval, list(arr_clients.keys()),
        )
```

- [ ] **Step 3: Inside the existing `vpn_health` block**, replace the `_vpn_health_job` definition with the observer-wrapped version (only when `_dc_health_job` is defined): [U]

```python
        if _dc_health_job is not None:
            _orig_emit = notifier.emit
            _dc_pending = {"flag": False}

            async def _emit_with_observer(event, payload):
                await _orig_emit(event, payload)
                if event == "vpn.restart_finished":
                    _dc_pending["flag"] = True

            notifier.emit = _emit_with_observer  # type: ignore[assignment]

            async def _vpn_health_job():
                await run_vpn_health(vpn_http, docker_mgr, notifier, vpn_health_cfg)
                if _dc_pending["flag"]:
                    _dc_pending["flag"] = False
                    try:
                        await _dc_health_job()
                    except Exception as exc:
                        log.warning("dc_health post-vpn probe failed: %s", exc)
        else:
            async def _vpn_health_job():
                await run_vpn_health(vpn_http, docker_mgr, notifier, vpn_health_cfg)
```

(The branch keeps the legacy single-job behavior when dc_health is disabled, and adds the observer wrap only when both modules are enabled.) [U]

- [ ] **Step 4: Integration test** [U]

Add to `tests/test_integration_startup.py` (mirror existing env-stub patterns in that file — read first): [U]

```python
@pytest.mark.asyncio
async def test_download_client_health_job_registered(monkeypatch):
    monkeypatch.setenv("DOCKTARR_SKIP_NETWORK_INIT", "1")
    monkeypatch.setenv("PROWLARR_URL", "http://prowlarr:9696")
    monkeypatch.setenv("PROWLARR_API_KEY", "x")
    monkeypatch.setenv("QBITTORRENT_URL", "http://qbit:8082")
    monkeypatch.setenv("QBITTORRENT_USERNAME", "u")
    monkeypatch.setenv("QBITTORRENT_PASSWORD", "p")
    monkeypatch.setenv("SONARR_URL", "http://sonarr:8989")
    monkeypatch.setenv("SONARR_API_KEY", "k")

    from docktarr.main import _build_scheduler_for_test
    result = await _build_scheduler_for_test()
    scheduler = result[0]
    ids = {j.id for j in scheduler.get_jobs()}
    assert "download_client_health" in ids
```

- [ ] **Step 5: Run; expect PASS** [U]

```bash
cd C:/Dev/docktarr && python -m pytest tests/test_integration_startup.py -v
```

- [ ] **Step 6: Full suite green** [U]

```bash
cd C:/Dev/docktarr && python -m pytest -q 2>&1 | tail -3
```
Expected: ≥ 214 passed, 0 failed.

- [ ] **Step 7: Commit** [U]

```bash
cd C:/Dev/docktarr && git add src/docktarr/main.py tests/test_integration_startup.py
git commit -m "feat(main): wire download_client_health + vpn.restart_finished hook"
```

---

## Task 11: Docs, version, push

- [ ] **Step 1: Version bump** [U]

In `pyproject.toml`: `version = "0.7.0"` → `version = "0.7.1"`.

- [ ] **Step 2: CHANGELOG** [U]

Prepend to `CHANGELOG.md` above the `## 0.7.0` block: [U]

```markdown
## 0.7.1 — 2026-05-12

### Added
- **`download_client_health` module — proactive download-client probe.** Walks
  every ARR app's `/api/{v}/downloadclient` config every 5 minutes, statically
  lints `host` for literal IPs, and runs a live `/downloadclient/test` to
  confirm reachability. Optional `DC_HEALTH_AUTO_PATCH` (default off) rewrites
  stale literal IPs back to the VPN container's DNS alias after verifying
  `getent hosts <name>` resolves inside the ARR app's own container. Hooks
  `vpn.restart_finished` to run an immediate out-of-band probe so a gluetun
  restart that rotates the bridge IP can't dry the river for 3 days (S113
  incident — `arr_known_issues.md` Pattern 12). New
  `/health/download_clients` endpoint returns the latest report. MAM-safe:
  configuration-only writes, no torrent-state mutation.
- New env vars: `DC_HEALTH_ENABLED`, `DC_HEALTH_INTERVAL`,
  `DC_HEALTH_VPN_CONTAINER`, `DC_HEALTH_AUTO_PATCH`.
- `ContainerInfo.ip_addresses` + `primary_ip` for downstream IP attribution.
- `vpn.restart_finished` event from `vpn_health` after a successful gluetun
  restart.

### Notes
- 8 new test scenarios per spec (`Doctarr - Download Client Health Check.md`).
- Test count 206 → 214.
```

- [ ] **Step 3: README module entry** — append after existing module catalog (skip if no such section) [U]

```markdown
### download_client_health
Probes every ARR app's `/downloadclient` config every 5 min. Static lint flags
literal-IP hosts (brittle to gluetun bridge IP churn); live `/downloadclient/test`
confirms reachability end-to-end. Optional `DC_HEALTH_AUTO_PATCH` rewrites stale
literal IPs back to the VPN container's DNS alias after `getent` verifies
resolution. Listens for `vpn.restart_finished` to probe immediately after a VPN
cycle. Report at `GET /health/download_clients`.
```

- [ ] **Step 4: Final suite** [U]

```bash
cd C:/Dev/docktarr && python -m pytest -q 2>&1 | tail -3
```
Expected: ≥ 214 passed, 0 failed.

- [ ] **Step 5: Commit** [U]

```bash
cd C:/Dev/docktarr && git add pyproject.toml CHANGELOG.md README.md
git commit -m "chore: bump 0.7.1 + CHANGELOG + README for download_client_health"
```

- [ ] **Step 6: Push branch** [U]

```bash
cd C:/Dev/docktarr && git push -u origin feat/download-client-health
```

- [ ] **Step 7: Open PR** [U]

```bash
gh pr create --repo CodeWarrior4Life/docktarr \
  --title "feat: v0.7.1 — download_client_health (S114)" \
  --body "Implements download_client_health module per spec. Static-IP lint + live /downloadclient/test probe + optional auto_patch (DNS-verify gated) + vpn.restart_finished hook + /health/download_clients endpoint. Driven by S113 incident. MAM-safe (config-only writes). 214/214 tests."
```

- [ ] **Step 8: Append PR URL to `D:/Vaults/Mainframe/02_Projects/Media Library/active-work.md` SESSION LOG** [U]

---

## Self-Review

**Spec coverage:** [U]

| Spec section | Task |
|---|---|
| Static-lint host classification | T4 (`_classify_host`) |
| Live `/downloadclient/test` | T1 (method), T5 (caller) |
| `dc_health.ok/literal_ip/unreachable/auto_patched/skipped_no_credentials` events | T4–T6, T9 templates |
| Auto-heal with `getent` gate | T6 |
| YAML config | Adapted to env vars (codebase convention) via T10 |
| StateStore slot `dc_health_report` | Corrected to `HealthState.dc_health` (codebase convention) — T3 |
| `/health/download_clients` endpoint | T3 |
| 8 test scenarios | T4 (1, 2), T5 (3, 6, 7), T6 (4, 5), T8 (8) |
| Integration with `arr_services` | T10 reuses arr_clients dict |
| Integration with `qbit_health` | Orthogonal — no shared state |
| Post-restart immediate probe | T7 emission, T10 wiring |
| MAM compliance | Documented; no torrent-state writes |

**Placeholders:** none. All code blocks complete; all commands explicit. [U]

**Type consistency:** `DownloadClientHealthConfig(vpn_container, auto_patch)` consistent T4 ↔ T6 ↔ T10. `ArrClient.put_download_client_host(client_id, new_host)` consistent T1 ↔ T6. `HealthState.record_dc_health(report)` consistent T3 ↔ T4. [U]

**Race deferred:** if `vpn.restart_finished` fires while `download_client_health` is mid-tick, double-emit possible. Harmless in this domain. Mark for v0.8.x if it surfaces [U].
