from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

import docker
import docker.errors

log = logging.getLogger("doctarr.docker")


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    status: str
    image: str
    env: dict[str, str] = field(default_factory=dict)
    device_paths: list[str] = field(default_factory=list)
    exit_code: int | None = None


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

        return ContainerInfo(
            name=c.name,
            status=c.status,
            image=c.attrs["Config"].get("Image", ""),
            env=env,
            device_paths=device_paths,
            exit_code=exit_code,
        )

    async def restart(self, name: str) -> None:
        def _restart():
            self._client.containers.get(name).restart()

        await asyncio.to_thread(_restart)

    async def stop(self, name: str) -> None:
        def _stop():
            self._client.containers.get(name).stop()

        await asyncio.to_thread(_stop)
