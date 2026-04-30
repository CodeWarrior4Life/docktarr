"""qBittorrent Web API client for stall detection."""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class QBitClient:
    def __init__(self, base_url: str, username: str, password: str):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._client = httpx.AsyncClient(timeout=15.0)
        self._sid: str | None = None

    async def login(self) -> None:
        resp = await self._client.post(
            f"{self._base_url}/api/v2/auth/login",
            data={"username": self._username, "password": self._password},
        )
        resp.raise_for_status()
        self._sid = resp.cookies.get("SID")
        if not self._sid:
            raise RuntimeError("qBittorrent login failed: no SID cookie")
        log.debug("qBit login OK")

    def _cookies(self) -> dict[str, str]:
        return {"SID": self._sid} if self._sid else {}

    async def get_torrents(self) -> list[dict]:
        """Return all torrents with full info."""
        resp = await self._client.get(
            f"{self._base_url}/api/v2/torrents/info",
            cookies=self._cookies(),
        )
        if resp.status_code == 403:
            await self.login()
            resp = await self._client.get(
                f"{self._base_url}/api/v2/torrents/info",
                cookies=self._cookies(),
            )
        resp.raise_for_status()
        return resp.json()

    async def delete_torrent(self, hash: str, delete_files: bool = True) -> None:
        """Delete a torrent by hash."""
        resp = await self._client.post(
            f"{self._base_url}/api/v2/torrents/delete",
            data={
                "hashes": hash,
                "deleteFiles": str(delete_files).lower(),
            },
            cookies=self._cookies(),
        )
        resp.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
