"""Bounded read-only clients and private durable monitor state."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from http import HTTPStatus
from http.cookiejar import MozillaCookieJar
from typing import TYPE_CHECKING, Any

from requests import Response, Session

if TYPE_CHECKING:
    from pathlib import Path

from mam.model import is_mam_tracker, number, torrent_snapshot

MAM_INTERVAL = 1800
QBT_INTERVAL = 300


class PollError(Exception):
    """Carry only a safe error category, never an HTTP body or credential URL."""

    def __init__(self, category: str, *, retry_after: float = 0) -> None:
        super().__init__(category)
        self.category = category
        self.retry_after = retry_after


def save_private(path: Path, data: dict[str, Any]) -> None:
    """Atomically replace durable state with owner-only permissions."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def load_private(path: Path) -> dict[str, Any]:
    """Load durable state; corrupted state fails closed rather than losing history."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise TypeError("Invalid monitor state")
    return data


def retry_delay(value: str | None, now: float) -> float:
    """Support both Retry-After seconds and HTTP dates."""
    delay = number(value)
    if delay is not None:
        return max(0, delay)
    try:
        return max(0, parsedate_to_datetime(value or "").timestamp() - now)
    except (ValueError, TypeError, OverflowError):
        return 0


def check_response(response: Response) -> None:
    """Translate transport failures without exposing request data."""
    if response.status_code in {401, 403}:
        raise PollError("authentication_failed")
    if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        raise PollError(
            "rate_limited",
            retry_after=retry_delay(response.headers.get("Retry-After"), time.time()),
        )
    if response.status_code != HTTPStatus.OK:
        raise PollError("http_error")


class MamClient:
    """Use only the documented user-data endpoint with an isolated cookie jar."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.credential_path = directory / "session.json"
        self.cookie_path = directory / "cookies.txt"

    def credential(self) -> tuple[str, str]:
        """Read a privately provisioned API session without logging it."""
        if not self.credential_path.exists():
            raise PollError("session_not_configured")
        if self.credential_path.stat().st_mode & 0o077:
            raise PollError("session_permissions")
        data = load_private(self.credential_path)
        token = data.get("mam_id")
        if (
            not isinstance(token, str)
            or not token.strip()
            or any(c.isspace() for c in token)
        ):
            raise PollError("invalid_session_file")
        return token, sha256(token.encode()).hexdigest()

    def fetch(self, token: str, *, reset_cookies: bool) -> dict[str, Any]:
        """Persist server cookie rotation, including cookies on failed responses."""
        jar = MozillaCookieJar(str(self.cookie_path))
        if self.cookie_path.exists() and not reset_cookies:
            jar.load(ignore_discard=True, ignore_expires=False)
        with Session() as session:
            session.trust_env = False
            if reset_cookies or not list(jar):
                session.cookies.set(
                    "mam_id",
                    token,
                    domain="www.myanonamouse.net",
                    path="/",
                    secure=True,
                )
                for cookie in session.cookies:
                    jar.set_cookie(cookie)
            session.cookies.update(jar)
            response = session.get(
                "https://www.myanonamouse.net/jsonLoad.php",
                params={"clientStats": "", "notif": "", "snatch_summary": ""},
                timeout=20,
                allow_redirects=False,
            )
            jar.clear()
            for cookie in session.cookies:
                jar.set_cookie(cookie)
            temporary = self.cookie_path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
            temporary.chmod(0o600)
            jar.save(str(temporary), ignore_discard=True, ignore_expires=True)
            temporary.replace(self.cookie_path)
            check_response(response)
            payload = response.json()
        if not isinstance(payload, dict):
            raise PollError("invalid_payload")
        return payload


class QbtClient:
    """Read all tracker membership and file completion using a bounded snapshot."""

    def __init__(self, url: str, username: str, password: str) -> None:
        self.url = url.rstrip("/")
        self.username = username
        self.password = password

    def fetch(self, previous: dict[str, Any], now: float) -> dict[str, Any]:
        """Return a complete snapshot or fail without declaring torrents missing."""
        deadline = time.monotonic() + 120
        with Session() as session:
            session.trust_env = False
            login = session.post(
                f"{self.url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                headers={"Referer": self.url},
                timeout=15,
                allow_redirects=False,
            )
            check_response(login)
            if login.text.strip() != "Ok.":
                raise PollError("authentication_failed")

            def get(path: str, **params: str) -> Any:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PollError("snapshot_timeout")
                response = session.get(
                    f"{self.url}/api/v2/{path}",
                    params=params,
                    timeout=min(15, remaining),
                    allow_redirects=False,
                )
                check_response(response)
                return response.json()

            preferences = get("app/preferences")
            torrents = get("torrents/info")
            if not isinstance(preferences, dict) or not isinstance(torrents, list):
                raise PollError("invalid_payload")
            result = {}
            for torrent in torrents:
                torrent_hash = torrent["hash"]
                trackers = get("torrents/trackers", hash=torrent_hash)
                if not isinstance(trackers, list):
                    raise PollError("invalid_payload")
                if not any(is_mam_tracker(t.get("url")) for t in trackers):
                    continue
                files = get("torrents/files", hash=torrent_hash)
                if (
                    not isinstance(files, list)
                    or not files
                    or any(
                        not isinstance(f, dict)
                        or number(f.get("size")) is None
                        or number(f.get("progress")) is None
                        or "priority" not in f
                        or not 0 <= float(f["progress"]) <= 1
                        or float(f["size"]) < 0
                        for f in files
                    )
                ):
                    raise PollError("incomplete_file_metadata")
                result[torrent_hash] = torrent_snapshot(
                    torrent,
                    files,
                    trackers,
                    preferences,
                    previous.get(torrent_hash, {}),
                    now,
                )
            return result


def timestamp(value: Any) -> str | None:
    """Format known Unix times for Home Assistant timestamp sensors."""
    return datetime.fromtimestamp(value, UTC).isoformat() if value else None
