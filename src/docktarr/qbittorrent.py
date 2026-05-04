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
        # qBit only issues a Set-Cookie on a NEW session. When this client has
        # already established a session via the httpx cookie jar, qBit returns
        # 200 Ok without Set-Cookie. resp.cookies is empty in that case, but
        # self._client.cookies still holds the live SID. Fall through.
        sid = resp.cookies.get("SID") or self._client.cookies.get("SID") or self._sid
        if not sid:
            raise RuntimeError("qBittorrent login failed: no SID cookie")
        self._sid = sid
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

    async def set_download_limit(self, bytes_per_sec: int) -> None:
        """Set the global download speed limit in bytes/sec. ``0`` = unlimited."""
        resp = await self._client.post(
            f"{self._base_url}/api/v2/transfer/setDownloadLimit",
            data={"limit": str(int(bytes_per_sec))},
            cookies=self._cookies(),
        )
        if resp.status_code == 403:
            await self.login()
            resp = await self._client.post(
                f"{self._base_url}/api/v2/transfer/setDownloadLimit",
                data={"limit": str(int(bytes_per_sec))},
                cookies=self._cookies(),
            )
        resp.raise_for_status()

    async def get_download_limit(self) -> int:
        """Return the current global download limit in bytes/sec. ``0`` = unlimited."""
        resp = await self._client.get(
            f"{self._base_url}/api/v2/transfer/downloadLimit",
            cookies=self._cookies(),
        )
        if resp.status_code == 403:
            await self.login()
            resp = await self._client.get(
                f"{self._base_url}/api/v2/transfer/downloadLimit",
                cookies=self._cookies(),
            )
        resp.raise_for_status()
        return int(resp.text.strip() or "0")

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
