from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

log = logging.getLogger("doctarr.health")


@dataclass
class HealthState:
    hw: dict[str, list[dict]] = field(default_factory=dict)
    audit: list[dict] = field(default_factory=list)
    permissions: list[dict] = field(default_factory=list)

    def record_hw(self, by_host: dict[str, list[dict]]) -> None:
        self.hw = by_host

    def record_audit_findings(self, findings: list[dict]) -> None:
        self.audit = findings

    def record_permission_findings(self, findings: list[dict]) -> None:
        self.permissions = [
            {**f, "drift_pct": (f["drift"] / f["total"] * 100.0) if f["total"] else 0.0}
            for f in findings
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "hw_capability": self.hw,
            "media_container_audit": self.audit,
            "permissions": self.permissions,
        }


class HealthServer:
    def __init__(self, state: HealthState, host: str = "0.0.0.0", port: int = 8080):
        self._state = state
        self._host = host
        self._port = port
        self._runner: web.AppRunner | None = None

    def snapshot(self) -> dict:
        return self._state.snapshot()

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/health/hw_capability", self._hw)
        app.router.add_get("/health/audit", self._audit)
        app.router.add_get("/health/permissions", self._perms)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()
        self._runner = runner
        log.info("health server on http://%s:%d/health", self._host, self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _health(self, req):
        return web.json_response(self._state.snapshot())

    async def _hw(self, req):
        return web.json_response(self._state.hw)

    async def _audit(self, req):
        return web.json_response(self._state.audit)

    async def _perms(self, req):
        return web.json_response(self._state.permissions)
