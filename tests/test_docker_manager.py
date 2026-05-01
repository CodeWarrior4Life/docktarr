from datetime import datetime, timezone

import pytest
from unittest.mock import MagicMock
from docktarr.docker_manager import DockerManager, ContainerInfo


class FakeContainer:
    def __init__(self, name, status, image, env=None, devices=None, started_at=None):
        self.name = name
        self.status = status
        state = {}
        if started_at is not None:
            state["StartedAt"] = started_at
        self.attrs = {
            "Config": {"Env": env or [], "Image": image},
            "HostConfig": {"Devices": devices or []},
            "State": state,
        }


@pytest.mark.asyncio
async def test_get_container_returns_info():
    fake_client = MagicMock()
    fake_client.containers.get.return_value = FakeContainer(
        name="Plex",
        status="running",
        image="lscr.io/linuxserver/plex:latest",
        env=["PUID=1026", "PGID=100"],
        devices=[{"PathOnHost": "/dev/dri"}],
    )
    dm = DockerManager(_client=fake_client)
    info = await dm.get_container("Plex")
    assert info.name == "Plex"
    assert info.status == "running"
    assert info.env["PUID"] == "1026"
    assert "/dev/dri" in info.device_paths


@pytest.mark.asyncio
async def test_get_container_parses_started_at():
    """ContainerInfo.started_at parses ISO 8601 from container State.StartedAt."""
    fake_client = MagicMock()
    fake_client.containers.get.return_value = FakeContainer(
        name="gluetun",
        status="running",
        image="qmcgaw/gluetun:latest",
        started_at="2026-04-30T21:20:00.123456789Z",
    )
    dm = DockerManager(_client=fake_client)
    info = await dm.get_container("gluetun")
    assert info.started_at is not None
    assert info.started_at.tzinfo is not None
    assert info.started_at == datetime(
        2026, 4, 30, 21, 20, 0, 123456, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_get_container_missing_started_at_returns_none():
    """If State.StartedAt is missing, started_at is None (not a crash)."""
    fake_client = MagicMock()
    fake_client.containers.get.return_value = FakeContainer(
        name="ancient",
        status="running",
        image="example:latest",
    )
    dm = DockerManager(_client=fake_client)
    info = await dm.get_container("ancient")
    assert info.started_at is None


@pytest.mark.asyncio
async def test_get_container_missing_raises():
    import docker.errors

    fake_client = MagicMock()
    fake_client.containers.get.side_effect = docker.errors.NotFound("missing")
    dm = DockerManager(_client=fake_client)
    with pytest.raises(LookupError):
        await dm.get_container("DoesNotExist")
