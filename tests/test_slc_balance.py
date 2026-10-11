"""Verify SLC poll failures preserve readings without notifying Home Assistant."""

# ruff: noqa: S101

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, call, patch

import httpx2
from slc_balance import SlcBalance
from slc_client import LoanSummary, SlcError


class SlcPollFailureTests(unittest.TestCase):
    """Exercise successful polling, repeated failures, and recovery."""

    def setUp(self) -> None:
        """Create a poller with isolated MQTT and Home Assistant transports."""
        self.app = object.__new__(SlcBalance)
        self.app.username = "test-user"
        self.app.password = "test-password"  # noqa: S105 - Synthetic test credential.
        self.app.secret_answer = "test-answer"  # noqa: S105 - Synthetic test credential.
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
            SlcError("Services unavailable - Student Loans Company"),
            httpx2.HTTPError("Connection failed"),
            SlcError("Services unavailable - Student Loans Company"),
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
            side_effect=SlcError("Services unavailable - Student Loans Company"),
        ):
            self.app.poll_slc()

        self.app.mqtt_client.publish.assert_not_called()
        self.app.call_service.assert_not_called()
        assert self.app.set_state.call_args.kwargs["state"] == "error"


if __name__ == "__main__":
    unittest.main()
