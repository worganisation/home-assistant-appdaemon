"""Verify SLC failure classification, preserved readings, and notifications."""

# ruff: noqa: S101, PLR2004

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, call, patch

import httpx2
from slc_balance import SlcBalance
from slc_client import LoanSummary, SlcError, SlcTemporaryError


class SlcPollFailureTests(unittest.TestCase):
    """Exercise successful polling, repeated failures, and recovery."""

    def setUp(self) -> None:
        """Create a poller with isolated MQTT and Home Assistant transports."""
        self.app = object.__new__(SlcBalance)
        self.app.username = "test-user"
        self.app.password = "test-password"  # noqa: S105 - Synthetic test credential.
        self.app.secret_answer = "test-answer"  # noqa: S105 - Synthetic test credential.
        self.app._failure_notified = False  # noqa: SLF001
        self.app.notify_script = "script.notify_will"
        self.app.notification_id = "slc_balance_poll_failed"
        self.app.raw_sensor = "sensor.slc_last_poll"
        self.app.mqtt_base_topic = "appdaemon/slc"
        self.app.mqtt_qos = 0
        self.app.mqtt_client = MagicMock()
        self.app.set_state = MagicMock()
        self.app.call_service = MagicMock()
        self.app.error = MagicMock()
        self.app.log = MagicMock()

        self.summary = LoanSummary(
            balance=1000.0,
            interest_rate_pct=4.1,
            as_of_date=date(2026, 10, 10),
            current_year="2026-27",
            salary_repayments=-100.0,
            direct_repayments=None,
            interest_added=10.0,
        )

    def test_repeated_failures_preserve_values_and_recover(self) -> None:
        """Portal and HTTP errors leave all retained MQTT topics untouched."""
        with patch("slc_balance.fetch_loan_summary", return_value=self.summary):
            self.app.poll_slc()

        publications = self.app.mqtt_client.publish.call_args_list.copy()
        assert (
            call(
                "appdaemon/slc/balance/state",
                "1000.00",
                qos=0,
                retain=True,
            )
            in publications
        )
        assert (
            call(
                "appdaemon/slc/availability",
                "online",
                qos=0,
                retain=True,
            )
            in publications
        )

        for error in (
            SlcTemporaryError("Services unavailable - Student Loans Company"),
            httpx2.ConnectError("Connection failed"),
            SlcTemporaryError("Services unavailable - Student Loans Company"),
        ):
            with patch("slc_balance.fetch_loan_summary", side_effect=error):
                self.app.poll_slc()

            assert self.app.mqtt_client.publish.call_args_list == publications
            self.app.call_service.assert_not_called()
            diagnostic = self.app.set_state.call_args.kwargs
            assert diagnostic["state"] == "error"
            assert diagnostic["attributes"]["error"] == str(error)

        with patch("slc_balance.fetch_loan_summary", return_value=self.summary):
            self.app.poll_slc()

        assert self.app.mqtt_client.publish.call_count > len(publications)
        assert self.app.set_state.call_args.kwargs["state"] == "ok"
        self.app.call_service.assert_not_called()

    def test_failed_first_poll_does_not_replace_retained_values(self) -> None:
        """A failed initial poll preserves broker state from the prior runtime."""
        with patch(
            "slc_balance.fetch_loan_summary",
            side_effect=SlcTemporaryError("Services unavailable - Student Loans Company"),
        ):
            self.app.poll_slc()

        self.app.mqtt_client.publish.assert_not_called()
        self.app.call_service.assert_not_called()
        assert self.app.set_state.call_args.kwargs["state"] == "error"

    def test_actionable_failure_notifies_once_and_clears_on_recovery(self) -> None:
        """Bad credentials and changed pages alert without losing retained values."""
        for message in (
            "Credential login failed: Your email address, CRN or password is not correct",
            "Secret answer failed: Your secret answer is not correct",
            "Overview page missing balance element",
        ):
            with self.subTest(message=message):
                self.setUp()
                with patch("slc_balance.fetch_loan_summary", return_value=self.summary):
                    self.app.poll_slc()

                self.app.mqtt_client.publish.reset_mock()
                with patch(
                    "slc_balance.fetch_loan_summary",
                    side_effect=SlcError(message),
                ):
                    self.app.poll_slc()
                    self.app.poll_slc()

                self.app.mqtt_client.publish.assert_not_called()
                self.app.call_service.assert_called_once()
                notification = self.app.call_service.call_args.kwargs["variables"]
                assert message in notification["message"]

                with patch(
                    "slc_balance.fetch_loan_summary",
                    side_effect=SlcTemporaryError("Services unavailable"),
                ):
                    self.app.poll_slc()

                self.app.call_service.assert_called_once()
                with patch("slc_balance.fetch_loan_summary", return_value=self.summary):
                    self.app.poll_slc()

                assert self.app.call_service.call_count == 2
                assert (
                    "message" not in self.app.call_service.call_args.kwargs["variables"]
                )

    def test_non_transient_http_error_notifies(self) -> None:
        """An unexpected HTTP refusal is actionable rather than a silent outage."""
        response = httpx2.Response(
            403,
            request=httpx2.Request("GET", "https://logon.slc.co.uk"),
        )
        error = httpx2.HTTPStatusError(
            "Forbidden",
            request=response.request,
            response=response,
        )
        with patch("slc_balance.fetch_loan_summary", side_effect=error):
            self.app.poll_slc()

        self.app.call_service.assert_called_once()
        self.app.mqtt_client.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
