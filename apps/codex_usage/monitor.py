"""AppDaemon lifecycle, private onboarding and per-account polling."""

from __future__ import annotations

import asyncio
import fcntl
import re
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from uuid import uuid4

from aiohttp import web
from appdaemon.plugins.hass.hassapi import Hass
from pydantic import ValidationError

from .models import (
    PROFILE_PATTERN,
    AccountResponse,
    Configuration,
    Limits,
    Profile,
    Usage,
    quota_readings,
    token_readings,
)
from .protocol import AuthenticationError, CodexError, Rpc
from .publisher import Publisher

LOGIN_TTL = 15 * 60


@dataclass
class ActiveAccount:
    """A credential generation, process and serialization lock."""

    profile: Profile
    rpc: Rpc
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Login:
    """Temporary isolated login which cannot alter active credentials."""

    profile: str
    generation: str
    rpc: Rpc
    login_id: str = ""
    status: str = "pending"
    task: asyncio.Task[None] | None = None


class CodexUsageMonitor(Hass):
    """Monitor account usage without starting inference or exposing credentials."""

    settings: Configuration
    root: Path
    binary: Path
    publisher: Publisher
    accounts: dict[str, ActiveAccount]
    logins: dict[str, Login]
    runner: web.AppRunner | None = None
    owner_lock: IO[str] | None = None
    poll_tasks: set[asyncio.Task[None]]
    enrollment_lock: asyncio.Lock
    closing: bool = False

    async def initialize(self) -> None:
        """Validate configuration and acquire exclusive ownership before starting."""
        try:
            self.settings = Configuration.model_validate(self.args)
        except ValidationError:
            raise ValueError("Invalid Codex monitor configuration") from None
        self.root = Path(self.settings.storage_path)
        self.binary = Path(self.settings.binary_path)
        self.accounts = {}
        self.logins = {}
        self.poll_tasks = set()
        self.enrollment_lock = asyncio.Lock()
        self.closing = False
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        (self.root / "homes").mkdir(exist_ok=True, mode=0o700)
        (self.root / "profiles").mkdir(exist_ok=True, mode=0o700)

        self.owner_lock = (self.root / "monitor.lock").open("w")
        try:
            fcntl.flock(self.owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owner_lock.close()
            self.owner_lock = None
            raise RuntimeError(
                "Another Codex monitor owns this storage directory",
            ) from None

        self.publisher = Publisher(self.settings, self.root)
        try:
            self._load_profiles()

            await self._control_server()
            self.publisher.start()
            await self.run_every(
                self.poll,
                "immediate",
                self.settings.poll_interval_seconds,
            )  # pyright: ignore[reportGeneralTypeIssues] — AppDaemon's sync decorator returns an awaitable on its event loop.
            await self.run_every(self.freshness, "immediate", 30)  # pyright: ignore[reportGeneralTypeIssues] — AppDaemon's sync decorator returns an awaitable on its event loop.
        except BaseException:
            await self.terminate()
            raise

        self.log("Codex monitor ready with %s account(s)", len(self.accounts))

    def _load_profiles(self) -> None:
        for path in sorted((self.root / "profiles").glob("*.json")):
            profile = Profile.model_validate_json(path.read_text())
            if path.stem != profile.name:
                raise ValueError("Profile filename does not match its identity")
            self.accounts[profile.name] = ActiveAccount(
                profile,
                Rpc(self.binary, profile.home(self.root)),
            )
            self.publisher.add(profile)

    def _prepare_socket(self) -> Path:
        socket = Path(self.settings.control_socket)
        socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        socket.parent.chmod(0o700)
        if socket.exists():
            if not socket.is_socket():
                raise ValueError("Control socket path is not a socket")
            socket.unlink()
        return socket

    async def _control_server(self) -> None:
        socket = await asyncio.to_thread(self._prepare_socket)

        app = web.Application(client_max_size=4096)
        app.router.add_post("/login/{profile}", self.login_start)
        app.router.add_get("/login/{profile}", self.login_status)
        app.router.add_delete("/login/{profile}", self.login_cancel)
        app.router.add_get("/accounts", self.account_status)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=5)
        await self.runner.setup()
        await web.UnixSite(self.runner, str(socket)).start()
        await asyncio.to_thread(socket.chmod, 0o600)

    @staticmethod
    def _profile_name(request: web.Request) -> str:
        name = request.match_info["profile"]
        if not re.fullmatch(PROFILE_PATTERN, name):
            raise web.HTTPBadRequest(
                text="Use a lowercase profile name of 1-40 characters",
            )
        return name

    async def login_start(self, request: web.Request) -> web.Response:
        """Start device authorization in an empty credential generation."""
        name = self._profile_name(request)
        if self.closing:
            raise web.HTTPServiceUnavailable(text="Monitor is stopping")
        if name in self.logins and self.logins[name].status == "pending":
            raise web.HTTPConflict(text="A login is already pending for this profile")

        generation = uuid4().hex
        home = self.root / "homes" / generation
        home.mkdir(mode=0o700)
        login = Login(name, generation, Rpc(self.binary, home))
        self.logins[name] = login
        try:
            await login.rpc.start()
            result = await login.rpc.request(
                "account/login/start",
                {"type": "chatgptDeviceCode"},
            )
            login_id, url, code = (
                result.get(key) for key in ("loginId", "verificationUrl", "userCode")
            )
            self._validate_login_response(login_id, url, code)
            login.login_id = str(login_id)
            login.task = asyncio.create_task(self._finish_login(login))
            return web.json_response(
                {"verification_url": url, "user_code": code},
                headers={"Cache-Control": "no-store"},
            )
        except (CodexError, OSError, ValueError):
            login.status = "failed"
            await login.rpc.close()
            shutil.rmtree(home)
            raise web.HTTPBadGateway(
                text="Device login could not start; check Codex installation and account policy",
            ) from None
        except asyncio.CancelledError:
            login.status = "cancelled"
            await login.rpc.close()
            shutil.rmtree(home, ignore_errors=True)
            raise

    @staticmethod
    def _validate_login_response(login_id: object, url: object, code: object) -> None:
        if not all(isinstance(value, str) and value for value in (login_id, url, code)):
            raise CodexError("Invalid device login response")
        if url != "https://auth.openai.com/codex/device":
            raise CodexError("Unexpected device login URL")

    async def _finish_login(self, login: Login) -> None:
        try:
            async with asyncio.timeout(LOGIN_TTL):
                await self._await_login(login)
                profile = await self._login_identity(login)
                async with self.enrollment_lock:
                    await self._activate(login, profile)
        except asyncio.CancelledError:
            if login.status != "complete":
                login.status = "cancelled"
            raise
        except (TimeoutError, CodexError, OSError, ValueError):
            if login.status != "complete":
                login.status = "failed"
        finally:
            await login.rpc.close()
            active = self.accounts.get(login.profile)
            if not active or active.profile.generation != login.generation:
                shutil.rmtree(login.rpc.home, ignore_errors=True)

    @staticmethod
    async def _await_login(login: Login) -> None:
        while event := await login.rpc.login_events.get():
            if event.get("loginId") == login.login_id:
                if event.get("success") is not True:
                    raise AuthenticationError("Device login failed")
                return

    @staticmethod
    async def _login_identity(login: Login) -> Profile:
        account = AccountResponse.model_validate(
            await login.rpc.request("account/read", {}),
        ).account
        limits = Limits.model_validate(await login.rpc.request("account/rateLimits/read"))
        if not account or not account.email or not limits.account_id:
            raise AuthenticationError("Account identity could not be verified")

        return Profile(
            name=login.profile,
            generation=login.generation,
            email=account.email,
            account_id=limits.account_id,
        )

    async def _activate(self, login: Login, profile: Profile) -> None:
        old = self.accounts.get(profile.name)
        identity = (profile.email.casefold(), profile.account_id)
        if old and (old.profile.email.casefold(), old.profile.account_id) != identity:
            login.status = "identity_mismatch"
            return

        if any(
            active.profile.name != profile.name
            and (active.profile.email.casefold(), active.profile.account_id) == identity
            for active in self.accounts.values()
        ):
            login.status = "already_enrolled"
            return

        lock = old.lock if old else asyncio.Lock()
        async with lock:
            await login.rpc.close()
            auth = profile.home(self.root) / "auth.json"
            if not auth.is_file():
                raise AuthenticationError("Login credentials were not persisted")
            auth.chmod(0o600)
            if old:
                await old.rpc.close()

            self._save_profile(profile)
            self.accounts[profile.name] = ActiveAccount(
                profile,
                Rpc(self.binary, profile.home(self.root)),
            )
            login.status = "complete"
            self.publisher.add(profile)
            self._schedule_poll(self.accounts[profile.name])

    def _save_profile(self, profile: Profile) -> None:
        path = self.root / "profiles" / f"{profile.name}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(profile.model_dump_json())
        temporary.chmod(0o600)
        temporary.replace(path)

    async def login_status(self, request: web.Request) -> web.Response:
        """Return login outcome without repeating a one-time device code."""
        name = self._profile_name(request)
        login = self.logins.get(name)
        return web.json_response(
            {"status": login.status if login else "not_started"},
            headers={"Cache-Control": "no-store"},
        )

    async def login_cancel(self, request: web.Request) -> web.Response:
        """Cancel only the pending login; an active profile is untouched."""
        name = self._profile_name(request)
        if (login := self.logins.get(name)) and login.status == "pending" and login.task:
            login.task.cancel()
            with suppress(asyncio.CancelledError):
                await login.task
        return web.json_response({"status": login.status if login else "not_started"})

    async def account_status(self, _request: web.Request) -> web.Response:
        """Describe enrolled identities and sensor IDs, excluding credentials."""
        accounts: list[dict[str, object]] = []
        for name, active in self.accounts.items():
            state = self.publisher.accounts[name]
            accounts.append(
                {
                    "profile": name,
                    "email": active.profile.email,
                    "sensors": [
                        {
                            "entity_id": f"sensor.{Publisher.object_id(name, reading.key)}",
                            "name": reading.name,
                            "key": reading.key,
                            "group": reading.group,
                        }
                        for reading in state.readings.values()
                    ],
                },
            )
        return web.json_response(
            {"accounts": accounts},
            headers={"Cache-Control": "no-store"},
        )

    async def poll(self, _kwargs: dict[str, object]) -> None:
        """Poll accounts independently so one failed login cannot block another."""
        if self.closing:
            return

        for active in tuple(self.accounts.values()):
            self._schedule_poll(active)

    def _schedule_poll(self, active: ActiveAccount) -> None:
        task = asyncio.create_task(self._poll_account(active))
        self.poll_tasks.add(task)
        task.add_done_callback(self.poll_tasks.discard)

    async def _poll_account(self, active: ActiveAccount) -> None:
        if active.lock.locked() or self.closing:
            return

        async with active.lock:
            name = active.profile.name
            if self.accounts.get(name) is not active:
                return

            try:
                await active.rpc.start()
                account = AccountResponse.model_validate(
                    await active.rpc.request("account/read", {}),
                ).account
                if (
                    not account
                    or not account.email
                    or account.email.casefold() != active.profile.email.casefold()
                ):
                    raise AuthenticationError("Account identity changed")
            except (CodexError, OSError, ValueError) as error:
                self.publisher.failure(
                    name,
                    "quota",
                    authentication=isinstance(
                        error,
                        (AuthenticationError, ValidationError),
                    ),
                )
                return

            for group, method in (
                ("quota", "account/rateLimits/read"),
                ("tokens", "account/usage/read"),
            ):
                try:
                    result = await active.rpc.request(method)
                    if group == "quota":
                        limits = Limits.model_validate(result)
                        if limits.account_id != active.profile.account_id:
                            raise AuthenticationError("Account identity changed")
                        readings = quota_readings(limits)
                    else:
                        readings = token_readings(Usage.model_validate(result))
                    self.publisher.success(name, group, readings)
                except (CodexError, OSError, ValueError) as error:
                    auth = isinstance(error, AuthenticationError)
                    self.publisher.failure(name, group, authentication=auth)
                    if auth:
                        break

    async def freshness(self, _kwargs: dict[str, object]) -> None:
        """Expire cached data even when a server is failing."""
        self.publisher.refresh()

    async def terminate(self) -> None:
        """Stop control requests, pending logins, polls, child processes and MQTT."""
        self.closing = True
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        tasks = list(self.poll_tasks)
        tasks.extend(login.task for login in self.logins.values() if login.task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(active.rpc.close() for active in self.accounts.values()))
        await self.publisher.close()
        if self.owner_lock:
            await asyncio.to_thread(
                Path(self.settings.control_socket).unlink,
                missing_ok=True,
            )
            self.owner_lock.close()
            self.owner_lock = None
