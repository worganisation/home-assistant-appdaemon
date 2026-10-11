"""Bounded stdio client for the pinned Codex app-server protocol."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import suppress
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

if TYPE_CHECKING:
    from pathlib import Path

OBJECT = TypeAdapter(dict[str, object])
REQUEST_TIMEOUT = 30


class CodexError(RuntimeError):
    """A sanitized app-server error safe to present to the operator."""


class AuthenticationError(CodexError):
    """The account requires another login."""


class Rpc:
    """Own one process, demultiplex replies and retain only login notifications."""

    def __init__(self, binary: Path, home: Path) -> None:
        self.binary = binary
        self.home = home
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task[None] | None = None
        self.pending: dict[int, asyncio.Future[dict[str, object]]] = {}
        self.login_events: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=8)
        self.sequence = 0

    async def start(self) -> None:
        """Start an isolated account process without inheriting service secrets."""
        if self.process and self.process.returncode is None:
            return

        await self.close()
        environment = {
            key: os.environ[key]
            for key in (
                "PATH",
                "HOME",
                "LANG",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "CODEX_CA_CERTIFICATE",
            )
            if key in os.environ
        }
        environment["CODEX_HOME"] = str(self.home)
        self.process = await asyncio.create_subprocess_exec(
            str(self.binary),
            "app-server",
            "--listen",
            "stdio://",
            "-c",
            'cli_auth_credentials_store="file"',
            "-c",
            "check_for_update_on_startup=false",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            cwd=self.home,
            limit=4 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self._read())
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "ha_codex_usage",
                        "title": "Home Assistant Codex usage",
                        "version": "1.0.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self._send({"method": "initialized"})
        except BaseException:
            await self.close()
            raise

    async def _send(self, message: dict[str, object]) -> None:
        if (
            not self.process
            or self.process.returncode is not None
            or not self.process.stdin
        ):
            raise CodexError("Codex process is unavailable")

        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Match replies by ID; close a hung process instead of stacking requests."""
        self.sequence += 1
        request_id = self.sequence
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        self.pending[request_id] = future
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await self._send({"id": request_id, "method": method, "params": params})
                return await future
        except TimeoutError:
            await self.close()
            raise CodexError("Codex request timed out") from None
        except (BrokenPipeError, ConnectionError):
            await self.close()
            raise CodexError("Codex process disconnected") from None
        finally:
            self.pending.pop(request_id, None)

    async def _read(self) -> None:
        process = self.process
        if not process or not process.stdout:
            return

        try:
            while line := await process.stdout.readline():
                message = OBJECT.validate_json(line)
                request_id = message.get("id")
                if isinstance(request_id, int) and (
                    future := self.pending.get(request_id)
                ):
                    if not future.done():
                        self._resolve(future, message)
                elif message.get("method") == "account/login/completed":
                    params = OBJECT.validate_python(message.get("params"))
                    if not self.login_events.full():
                        self.login_events.put_nowait(params)
        except (ValueError, OSError):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CodexError("Codex process disconnected"))

    @staticmethod
    def _resolve(
        future: asyncio.Future[dict[str, object]],
        message: dict[str, object],
    ) -> None:
        if "error" in message:
            error = OBJECT.validate_python(message["error"])
            description = str(error.get("message", "")).lower()
            auth = any(
                marker in description
                for marker in (
                    "401",
                    "unauthorized",
                    "not authenticated",
                    "not logged in",
                    "refresh token",
                    "refresh_token",
                    "authentication required",
                    "sign in",
                    "login required",
                )
            )
            future.set_exception(
                AuthenticationError("Account needs login")
                if auth
                else CodexError("Codex rejected the request"),
            )
        else:
            future.set_result(OBJECT.validate_python(message.get("result")))

    async def close(self) -> None:
        """Reap the child and reader on reload, cancellation or failure."""
        if self.process and self.process.returncode is None:
            with suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()

        if self.reader:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader

        self.reader = None
        self.process = None
