import pytest
from unittest.mock import MagicMock
from docktarr.docker_manager import DockerManager, ContainerInfo


class FakeContainer:
    def __init__(self, name, status, image, env=None, devices=None):
        self.name = name
        self.status = status
        self.attrs = {
            "Config": {"Env": env or [], "Image": image},
            "HostConfig": {"Devices": devices or []},
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
async def test_get_container_missing_raises():
    import docker.errors

    fake_client = MagicMock()
    fake_client.containers.get.side_effect = docker.errors.NotFound("missing")
    dm = DockerManager(_client=fake_client)
    with pytest.raises(LookupError):
        await dm.get_container("DoesNotExist")
