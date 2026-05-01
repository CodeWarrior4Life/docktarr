from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import docker
import docker.errors

log = logging.getLogger("docktarr.docker")


def _parse_started_at(raw: str | None) -> datetime | None:
    """Parse Docker's ``State.StartedAt`` ISO 8601 string to a tz-aware datetime.

    Docker emits values like ``2026-04-30T21:20:00.123456789Z`` with up to
    nanosecond precision; Python's stdlib only handles microseconds, so we
    truncate the fractional part to 6 digits and normalize ``Z`` to ``+00:00``.
    Returns None if the field is missing or the zero-value sentinel
    ``0001-01-01T00:00:00Z`` (containers that have never started).
    """
    if not raw or raw.startswith("0001-01-01"):
        return None
    s = raw.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        frac = frac[:6].ljust(6, "0")
        s = f"{head}.{frac}"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    status: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    device_paths: list[str] = field(default_factory=list)
    exit_code: int | None = None
    started_at: datetime | None = None


class DockerManager:
    def __init__(self, _client=None):
        self._client = _client or docker.from_env()

    async def get_container(self, name: str) -> ContainerInfo:
        def _get():
            return self._client.containers.get(name)

        try:
            c = await asyncio.to_thread(_get)
        except docker.errors.NotFound as e:
            raise LookupError(f"container {name!r} not found") from e

        env_pairs = c.attrs["Config"].get("Env") or []
        env = dict(p.split("=", 1) for p in env_pairs if "=" in p)
        devices = c.attrs.get("HostConfig", {}).get("Devices") or []
        device_paths = [d.get("PathOnHost") for d in devices if d.get("PathOnHost")]
        state = c.attrs.get("State", {})
        raw_exit = state.get("ExitCode")
        exit_code = int(raw_exit) if raw_exit is not None else None
        started_at = _parse_started_at(state.get("StartedAt"))

        return ContainerInfo(
            name=c.name,
            status=c.status,
            image=c.attrs["Config"].get("Image", ""),
            env=env,
            device_paths=device_paths,
            exit_code=exit_code,
            started_at=started_at,
        )

    async def restart(self, name: str) -> None:
        def _restart():
            self._client.containers.get(name).restart()

        await asyncio.to_thread(_restart)

    async def stop(self, name: str) -> None:
        def _stop():
            self._client.containers.get(name).stop()

        await asyncio.to_thread(_stop)
