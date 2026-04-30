import os
import pytest
from docktarr.ssh_client import SSHRef, SSHClient, resolve_ssh_ref


def test_resolve_ssh_ref_reads_env(monkeypatch):
    monkeypatch.setenv("MEGACITY_SUDO_USER", "SuperUser")
    monkeypatch.setenv("MEGACITY_SUDO_PASSWORD", "secret123")
    ref = resolve_ssh_ref("megacity_sudo", host="megacity")
    assert ref.username == "SuperUser"
    assert ref.password == "secret123"
    assert ref.host == "megacity"


def test_resolve_ssh_ref_defaults_username(monkeypatch):
    monkeypatch.delenv("FOO_SUDO_USER", raising=False)
    monkeypatch.setenv("FOO_SUDO_PASSWORD", "x")
    ref = resolve_ssh_ref("foo_sudo", host="foo")
    assert ref.username == "SuperUser"  # default


def test_resolve_ssh_ref_raises_when_password_missing(monkeypatch):
    monkeypatch.delenv("BAR_SUDO_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="BAR_SUDO_PASSWORD"):
        resolve_ssh_ref("bar_sudo", host="bar")


@pytest.mark.asyncio
async def test_ssh_client_run_with_fake():
    from docktarr.ssh_client import _FakeSSHConnection

    client = SSHClient(
        ref=SSHRef(host="zion", username="x", password="y"),
        _connection_factory=_FakeSSHConnection.factory({"uptime": "12:00 up 1 day"}),
    )
    result = await client.run("uptime")
    assert result.stdout.strip() == "12:00 up 1 day"
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_ssh_client_sudo_wraps_command():
    from docktarr.ssh_client import _FakeSSHConnection

    captured = []
    client = SSHClient(
        ref=SSHRef(host="zion", username="x", password="y"),
        _connection_factory=_FakeSSHConnection.factory({"*": "ok"}, capture=captured),
    )
    await client.run("chown 1026 /x", sudo=True)
    assert any("sudo -S" in c and "chown 1026" in c for c in captured)
