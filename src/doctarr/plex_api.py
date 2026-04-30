from __future__ import annotations

import re
from dataclasses import dataclass, field

import httpx


_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')


@dataclass(frozen=True)
class PlexPreferences:
    token: str
    attrs: dict[str, str] = field(default_factory=dict)

    def get(self, key: str) -> str | None:
        return self.attrs.get(key)


def parse_preferences_xml(xml: str) -> PlexPreferences:
    """Parse Plex's Preferences.xml (flat attrs on <Preferences>). Tolerant to whitespace/newlines."""
    # Grab everything inside the first <Preferences ... /> or <Preferences ... >
    start = xml.find("<Preferences")
    if start < 0:
        raise ValueError("Preferences.xml missing <Preferences> tag")
    end = xml.find(">", start)
    if end < 0:
        raise ValueError("Preferences.xml malformed — no closing '>'")
    head = xml[start + len("<Preferences") : end]
    attrs = {m.group(1): m.group(2) for m in _ATTR_RE.finditer(head)}
    token = attrs.pop("PlexOnlineToken", "")
    return PlexPreferences(token=token, attrs=attrs)


class PlexClient:
    """Thin async Plex REST client. Base URL = http://host:32400."""

    def __init__(
        self, base_url: str, token: str, http: httpx.AsyncClient | None = None
    ):
        self._base = base_url.rstrip("/")
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=30.0)

    async def library_sections(self) -> list[dict]:
        r = await self._http.get(
            f"{self._base}/library/sections", params={"X-Plex-Token": self._token}
        )
        r.raise_for_status()
        # Plex returns XML by default; expect caller to parse or request JSON via Accept header.
        return _parse_sections(r.text)

    async def refresh_section(self, section_id: int) -> int:
        r = await self._http.get(
            f"{self._base}/library/sections/{section_id}/refresh",
            params={"X-Plex-Token": self._token},
        )
        return r.status_code

    async def active_sessions(self) -> int:
        r = await self._http.get(
            f"{self._base}/status/sessions", params={"X-Plex-Token": self._token}
        )
        r.raise_for_status()
        m = re.search(r'size="(\d+)"', r.text)
        return int(m.group(1)) if m else 0

    async def set_preference(self, key: str, value: str) -> int:
        r = await self._http.put(
            f"{self._base}/:/prefs",
            params={key: value, "X-Plex-Token": self._token},
        )
        return r.status_code

    async def close(self):
        await self._http.aclose()


def _parse_sections(xml: str) -> list[dict]:
    out = []
    for m in re.finditer(r"<Directory\b([^>]*)/?>", xml):
        attrs = {a.group(1): a.group(2) for a in _ATTR_RE.finditer(m.group(1))}
        out.append(attrs)
    return out
