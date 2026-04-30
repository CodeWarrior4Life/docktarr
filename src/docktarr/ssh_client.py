from __future__ import annotations

import asyncio
import logging
import os
import shlex
from dataclasses import dataclass
from typing import Callable, Awaitable

import asyncssh

log = logging.getLogger("doctarr.ssh")


@dataclass(frozen=True)
class SSHRef:
    host: str
    username: str
    password: str | None = None
    key_path: str | None = None
    port: int = 22


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_code: int


def resolve_ssh_ref(ssh_ref_name: str, *, host: str) -> SSHRef:
    """Resolve a YAML ssh_ref like 'megacity_sudo' to credentials from env."""
    prefix = ssh_ref_name.upper()
    user = os.environ.get(f"{prefix}_USER", "SuperUser").strip()
    password = os.environ.get(f"{prefix}_PASSWORD", "").strip()
    key_path = os.environ.get(f"{prefix}_KEY_PATH", "").strip() or None
    if not password and not key_path:
        raise ValueError(
            f"SSH ref {ssh_ref_name!r} requires {prefix}_PASSWORD or "
            f"{prefix}_KEY_PATH environment variable."
        )
    return SSHRef(
        host=host,
        username=user,
        password=password or None,
        key_path=key_path,
    )


class _RealSSHConnection:
    @staticmethod
    def factory():
        async def _connect(ref: SSHRef):
            kwargs: dict = {
                "host": ref.host,
                "username": ref.username,
                "port": ref.port,
                "known_hosts": None,  # TOFU; production should mount known_hosts
            }
            if ref.password:
                kwargs["password"] = ref.password
            if ref.key_path:
                kwargs["client_keys"] = [ref.key_path]
            return await asyncssh.connect(**kwargs)

        return _connect


class _FakeSSHConnection:
    """Test double; returns canned responses keyed by command prefix."""

    def __init__(self, responses: dict[str, str], capture: list[str] | None = None):
        self._responses = responses
        self._capture = capture if capture is not None else []

    @classmethod
    def factory(cls, responses: dict[str, str], capture: list[str] | None = None):
        inst = cls(responses, capture)

        async def _connect(ref: SSHRef):
            return inst

        return _connect

    async def run(self, cmd: str, input: str | None = None):
        self._capture.append(cmd)
        # Exact match first, then wildcard, then first-word key
        for key, val in self._responses.items():
            if key == cmd or key == "*" or cmd.startswith(key):
                return _FakeProcess(stdout=val, stderr="", exit_status=0)
        return _FakeProcess(stdout="", stderr="no match", exit_status=127)

    async def wait_closed(self):
        return None

    def close(self):
        pass


@dataclass
class _FakeProcess:
    stdout: str
    stderr: str
    exit_status: int


class SSHClient:
    def __init__(
        self,
        ref: SSHRef,
        _connection_factory: Callable[[SSHRef], Awaitable] | None = None,
    ):
        self._ref = ref
        self._connect = _connection_factory or _RealSSHConnection.factory()
        self._conn = None
        self._lock = asyncio.Lock()

    async def _ensure_connection(self):
        if self._conn is None:
            self._conn = await self._connect(self._ref)
        return self._conn

    async def run(
        self,
        cmd: str,
        *,
        sudo: bool = False,
        timeout: float = 60.0,
    ) -> CommandResult:
        async with self._lock:
            conn = await self._ensure_connection()
            if sudo and self._ref.password:
                wrapped = (
                    f"echo {shlex.quote(self._ref.password)} | sudo -S "
                    f"sh -c {shlex.quote(cmd)}"
                )
            elif sudo:
                wrapped = f"sudo -n sh -c {shlex.quote(cmd)}"
            else:
                wrapped = cmd
            try:
                proc = await asyncio.wait_for(conn.run(wrapped), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("SSH command timeout on %s: %s", self._ref.host, cmd)
                return CommandResult(stdout="", stderr="timeout", exit_code=124)

            return CommandResult(
                stdout=str(proc.stdout) if proc.stdout else "",
                stderr=str(proc.stderr) if proc.stderr else "",
                exit_code=int(proc.exit_status if proc.exit_status is not None else -1),
            )

    async def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
                await self._conn.wait_closed()
            except Exception:
                pass
            self._conn = None
