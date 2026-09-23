"""Exercise monitor failure boundaries without network or Home Assistant writes."""

# ruff: noqa: S101, PLR2004

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from threading import RLock
from unittest.mock import MagicMock, patch

from mam.clients import MamClient, PollError, retry_delay, save_private
from mam.mam_monitor import MamMonitor
from mam.model import (
    GIB,
    account_snapshot,
    assess,
    is_mam_tracker,
    number,
    reconcile_torrents,
    safe_text,
    size_bytes,
    torrent_snapshot,
    unsatisfied_limit,
)


def account() -> dict:
    """Return a synthetic verified account, not a fixture of MAM's wire schema."""
    return {
        "credit": 5,
        "ratio": 2,
        "coverage": "complete",
        "class": "User",
        "unsatisfied": 1,
        "connectable": True,
        "notices": [],
    }


def torrent(**changes) -> dict:
    """Build a local idle seeder with unrestricted retention."""
    raw = {
        "name": "Example book",
        "state": "stalledUP",
        "seeding_time": 3600,
        "added_on": 1000,
        "ratio_limit": -1,
        "seeding_time_limit": -1,
        "inactive_seeding_time_limit": -1,
    }
    raw.update(changes)
    return raw


def snapshot(raw=None, files=None, previous=None) -> dict:
    """Normalize a synthetic complete torrent."""
    return torrent_snapshot(
        raw or torrent(),
        files if files is not None else [{"size": GIB, "priority": 1, "progress": 1}],
        [{"url": "https://t.myanonamouse.net/announce", "status": 2}],
        {},
        previous or {},
        2000,
    )


class MamModelTests(unittest.TestCase):
    """Cover account limits and all-file obligations independently of transport."""

    def test_formatted_sizes(self) -> None:
        """Preserve explicit binary versus decimal units and reject bad values."""
        assert size_bytes("1,024.50 MiB") == 1024.5 * 1024**2
        assert size_bytes("5 GB") == 5_000_000_000
        assert size_bytes("5 GiB") == 5 * GIB
        for value in (None, "unknown", "NaN GiB", 123, "-1 GiB"):
            assert size_bytes(value) is None
        for value in (True, "inf", "NaN", None):
            assert number(value) is None

    def test_unknown_optional_fields_and_zero_download_ratio(self) -> None:
        """Infinity is not a JSON number or evidence of zero ratio."""
        result = account_snapshot(
            {
                "uid": "123",
                "classname": "User",
                "uploaded": "5 GiB",
                "downloaded": "0 B",
                "ratio": "∞",
            },
            {},
        )
        assert result["ratio"] is None
        assert result["unsatisfied"] is None
        assert result["coverage"] == "partial"
        assert result["credit"] == 5

    def test_verified_paths_only(self) -> None:
        """Optional adapters reject invalid types instead of inventing zeroes."""
        raw = {
            "uid": "123",
            "uploaded": "5 GiB",
            "downloaded": "1 GiB",
            "custom": {"count": 2, "connected": False},
        }
        result = account_snapshot(
            raw,
            {"unsatisfied": ["custom", "count"], "connectable": ["custom", "connected"]},
        )
        assert result["unsatisfied"] == 2
        assert result["connectable"] is False
        raw["custom"]["count"] = True
        assert (
            account_snapshot(raw, {"unsatisfied": ["custom", "count"]})["unsatisfied"]
            is None
        )

    def test_class_limit_precedence(self) -> None:
        """Unverified account age never promotes a new User to 50 slots."""
        assert unsatisfied_limit({"class": "User"})[0] == 20
        assert unsatisfied_limit({"class": "User"}, 50)[0] == 50
        assert unsatisfied_limit({"class": "User", "limit": 19}, 50)[0] == 19
        assert unsatisfied_limit({"class": "Staff"})[0] is None

    def test_tracker_domain_boundaries(self) -> None:
        """Authenticated tracker URLs are matched without publishing them."""
        assert is_mam_tracker("https://t.myanonamouse.net/a/secret")
        assert not is_mam_tracker("https://myanonamouse.net.evil.test/announce")
        assert not is_mam_tracker("https://other.test/myanonamouse.net")

    def test_idle_seeding_is_normal(self) -> None:
        """No upload traffic is necessary to accumulate client seed time."""
        result = snapshot()
        assert result["issues"] == []
        assert result["hours_remaining"] == 71

    def test_skipped_cover_invalidates_completion(self) -> None:
        """A 100 percent selected-file progress is insufficient."""
        files = [
            {"size": GIB, "priority": 1, "progress": 1},
            {"size": 1024, "priority": 0, "progress": 0},
        ]
        result = snapshot(torrent(progress=1), files)
        assert not result["complete"]
        assert result["seed_hours"] is None
        assert "skipped_files" in result["issues"]
        assert result["remaining_gib"] > 0

    def test_retention_and_counter_reset(self) -> None:
        """Finite limits and resetting local counters remain visible risks."""
        previous = snapshot(torrent(seeding_time=7200))
        result = snapshot(torrent(ratio_limit=1), previous=previous)
        assert "counter_reset" in result["issues"]
        assert "retention_limit" in result["issues"]
        assert snapshot(previous=result)["counter_reset"]

    def test_disappearance_preserves_obligation(self) -> None:
        """A removed client entry must not remove an unresolved obligation."""
        result = reconcile_torrents({}, {"hash": snapshot()})
        assert result["hash"]["missing"]
        assert result["hash"]["issues"] == ["missing_torrent"]
        assert (
            reconcile_torrents({}, {"hash": snapshot(torrent(seeding_time=72 * 3600))})
            == {}
        )

    def test_budget_and_reserve(self) -> None:
        """Outstanding bytes and reserve both consume estimated headroom."""
        pending = snapshot(files=[{"size": 4 * GIB, "priority": 1, "progress": 0}])
        values, issues = assess(
            account(),
            {"hash": pending},
            mam_fresh=True,
            qbt_fresh=True,
            reserve=2,
            limit_override=0,
        )
        assert values["budget"] == -1
        assert issues["budget"][0] == "warning"
        values, issues = assess(
            account(),
            {"hash": pending},
            mam_fresh=True,
            qbt_fresh=True,
            reserve=0.5,
            limit_override=0,
        )
        assert values["budget"] == 0.5
        assert "budget" not in issues

    def test_stale_sources_never_clear(self) -> None:
        """Cached healthy values cannot clear missing source coverage."""
        values, issues = assess(
            account(),
            {},
            mam_fresh=False,
            qbt_fresh=False,
            reserve=2,
            limit_override=0,
        )
        assert values["status"] == "attention"
        assert "budget" not in values
        assert set(issues) == {"mam_stale", "qbt_stale"}

    def test_redaction(self) -> None:
        """Do not retain authenticated links or markup in display strings."""
        value = safe_text('<a href="https://site.test/token">Title</a> cookie=secret')
        assert "secret" not in value
        assert "https" not in value
        assert "<a" not in value


class MamRuntimeTests(unittest.TestCase):
    """Validate persistent rate limits, alerts and MQTT restart behavior."""

    def make_app(self) -> MamMonitor:
        """Construct the runtime with mocked Home Assistant dependencies."""
        app = object.__new__(MamMonitor)
        app.state = {
            "started": 1,
            "alerts": {},
            "account": {},
            "torrents": {},
            "mam": {
                "last_success": 0,
                "next_poll": 9000,
                "error": "not_polled",
                "failures": 0,
            },
            "qbt": {
                "last_success": 0,
                "next_poll": 9000,
                "error": "not_polled",
                "failures": 0,
            },
        }
        app.lock = RLock()
        app.persist = MagicMock()
        app.call_service = MagicMock()
        app.log = MagicMock()
        app.args = {}
        return app

    def test_dedup_escalation_daily_and_recovery(self) -> None:
        """Repeated ticks and restored alert state do not spam the phone."""
        app = self.make_app()
        issues = {"budget": ("warning", "Low budget")}
        app.notify_issues(issues, 100, mam_fresh=True, qbt_fresh=True)
        app.notify_issues(issues, 200, mam_fresh=True, qbt_fresh=True)
        assert app.call_service.call_count == 1
        restored = self.make_app()
        restored.state["alerts"] = json.loads(json.dumps(app.state["alerts"]))
        restored.notify_issues(issues, 300, mam_fresh=True, qbt_fresh=True)
        restored.call_service.assert_not_called()
        issues["budget"] = ("critical", "No credit")
        restored.notify_issues(issues, 400, mam_fresh=True, qbt_fresh=True)
        restored.notify_issues(issues, 500, mam_fresh=True, qbt_fresh=True)
        assert restored.call_service.call_count == 1
        restored.notify_issues({}, 600, mam_fresh=False, qbt_fresh=True)
        assert "budget" in restored.state["alerts"]
        restored.notify_issues({}, 700, mam_fresh=True, qbt_fresh=True)
        assert restored.call_service.call_count == 2

    def test_transient_delay_and_stale_timing(self) -> None:
        """Stale thresholds do not accidentally acquire a second grace period."""
        app = self.make_app()
        issues = {"torrent:hash:tracker_error": ("warning", "Tracker error")}
        app.notify_issues(issues, 100, mam_fresh=True, qbt_fresh=True)
        app.call_service.assert_not_called()
        app.notify_issues(issues, 1000, mam_fresh=True, qbt_fresh=True)
        assert app.call_service.call_count == 1
        app.notify_issues(
            {"mam_stale": ("warning", "Stale")},
            5500,
            mam_fresh=False,
            qbt_fresh=False,
        )
        assert app.call_service.call_count == 2

    def test_durable_cooldown(self) -> None:
        """Reloading a reserved schedule cannot produce an immediate API request."""
        app = self.make_app()
        app.poll_source = MagicMock()
        app.render = MagicMock()
        with patch("mam.mam_monitor.time.time", return_value=100):
            app.tick()
        app.poll_source.assert_not_called()

    def test_reserve_waits_for_confirmation_and_preserves_zero(self) -> None:
        """A failed HA helper write does not silently change the default to zero."""
        app = self.make_app()
        app.reserve_entity = "input_number.mam_download_reserve_gib"
        app.get_state = MagicMock(return_value="0")
        assert app.reserve() == 2
        assert not app.state.get("reserve_initialized")
        app.get_state.return_value = "2"
        assert app.reserve() == 2
        assert app.state["reserve_initialized"]
        app.get_state.return_value = "0"
        assert app.reserve() == 0
        app.get_state.return_value = "unavailable"
        assert app.reserve() == 0

    def test_reconnect_discovery_and_liveness(self) -> None:
        """MQTT discovery carries stable IDs, retained state and source availability."""
        app = self.make_app()
        app.base_topic = "appdaemon/mam"
        app.mqtt_client = MagicMock()
        app.render = MagicMock()
        app.mqtt_connected(None, None, None, 0, None)
        configs = [
            json.loads(call.args[1])
            for call in app.mqtt_client.publish.call_args_list
            if call.args[0].endswith("/config")
        ]
        budget = next(
            config
            for config in configs
            if config["default_entity_id"] == "sensor.mam_budget"
        )
        assert len(budget["availability"]) == 3
        assert budget["expire_after"] == 180
        assert budget["state_class"] == "measurement"
        assert all(
            call.kwargs["retain"] for call in app.mqtt_client.publish.call_args_list
        )
        app.render.assert_called_once()

    def test_authentication_failure_blocks_same_session(self) -> None:
        """A rejected credential is not retried after cooldown or app reload."""
        app = self.make_app()
        app.mam = MagicMock()
        app.mam.credential.return_value = ("test-session", "fingerprint")
        app.state["mam"].update(
            {"auth_blocked": True, "credential_fingerprint": "fingerprint"},
        )
        with self.assertRaises(PollError):  # noqa: PT027
            app.poll_source("mam", 100)
        app.mam.fetch.assert_not_called()

    def test_request_failure_reserves_cooldown_before_attempt(self) -> None:
        """No network request precedes the durable cooldown reservation."""
        app = self.make_app()
        app.state["mam"]["next_poll"] = 0
        app.render = MagicMock()

        def fail(source, now):
            assert app.state[source]["next_poll"] == now + 1800
            app.persist.assert_called()
            raise PollError("rate_limited", retry_after=10000)

        app.poll_source = MagicMock(side_effect=fail)
        with patch("mam.mam_monitor.time.time", return_value=100):
            app.tick()
        assert app.state["mam"]["next_poll"] == 10100
        assert app.state["mam"]["last_success"] == 0

    def test_retry_after(self) -> None:
        """Both legal HTTP Retry-After formats are respected."""
        assert retry_delay("3600", 0) == 3600
        assert retry_delay("Thu, 01 Jan 1970 01:00:00 GMT", 0) == 3600
        assert retry_delay("bad", 0) == 0

    def test_private_storage_and_missing_session(self) -> None:
        """Missing configured secrets fail safely and runtime storage stays private."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            client = MamClient(path)
            with self.assertRaises(PollError):  # noqa: PT027 - stdlib unittest, no pytest dependency
                client.credential()
            save_private(path / "monitor.json", {"next_poll": 123})
            assert (path / "monitor.json").stat().st_mode & 0o077 == 0
            client = MamClient(path, "test-session")
            assert client.credential()[0] == "test-session"
            assert not (path / "session.json").exists()

    def test_cookie_rotation(self) -> None:
        """A response cookie is saved for the next scheduled request."""
        from requests import Session  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as directory:
            client = MamClient(Path(directory))
            session = Session()
            response = MagicMock(status_code=200)
            response.json.return_value = {"uid": "123"}

            def response_with_rotation(*_args, **_kwargs):
                session.cookies.set(
                    "mam_id",
                    "rotated",
                    domain="www.myanonamouse.net",
                    path="/",
                    secure=True,
                )
                return response

            session.get = MagicMock(side_effect=response_with_rotation)
            with patch("mam.clients.Session", return_value=session):
                client.fetch("test-session", reset_cookies=True)
            assert "rotated" in client.cookie_path.read_text()
            assert client.cookie_path.stat().st_mode & 0o077 == 0


if __name__ == "__main__":
    unittest.main()
