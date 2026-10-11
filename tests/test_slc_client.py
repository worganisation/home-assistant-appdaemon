"""Verify temporary outages are distinct from SLC login and layout failures."""

# ruff: noqa: PT027, S106 - unittest runner and synthetic test credentials.

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx2
import slc_client
from slc_client import SlcError, SlcTemporaryError, fetch_loan_summary


class SlcResponseTests(unittest.TestCase):
    """Exercise the production response hook through a mock HTTP transport."""

    def test_outages_at_every_login_stage_are_temporary(self) -> None:
        """Outages at landing, cookies, login, credentials, or secret answer stay silent."""
        login = (
            '<form action="/login">'
            '<input name="userId"><input name="_csrf" value="csrf">'
            '<input name="lt"><input name="execution"><input name="_eventId">'
            "</form>"
        )
        challenge_html = (
            '<form action="/secret"><input name="secretAnswer">'
            '<input name="_csrf" value="csrf"></form>'
        )
        for failing_stage in range(6):
            for status, body in (
                (200, "<title>Services unavailable - Student Loans Company</title>"),
                (503, "<title>Server error</title>"),
                (429, "<title>Too many requests</title>"),
                (408, "<title>Request timeout</title>"),
            ):
                with self.subTest(stage=failing_stage, status=status):
                    responses = [
                        httpx2.Response(200, text=html)
                        for html in ("", "", login, challenge_html, "", "")
                    ]
                    responses[failing_stage] = httpx2.Response(status, text=body)
                    with slc_client._client() as client:  # noqa: SLF001
                        client_with_transport = httpx2.Client(
                            transport=httpx2.MockTransport(
                                lambda _, pending=responses: pending.pop(0),
                            ),
                            event_hooks=client.event_hooks,
                        )

                    with (
                        patch("slc_client._client", return_value=client_with_transport),
                        self.assertRaises(SlcTemporaryError),
                    ):
                        fetch_loan_summary(
                            username="user",
                            password="test",
                            secret_answer="test",
                        )

    def test_unknown_login_page_is_actionable(self) -> None:
        """Missing login markup without an outage marker remains a layout failure."""
        self._assert_actionable(
            [
                "",
                "",
                "<title>New login layout</title>",
            ],
            "Did not reach SLC login form",
        )

    def test_rejected_password_is_actionable(self) -> None:
        """The portal's explicit rejected-credential message remains an error."""
        self._assert_actionable(
            [
                "",
                "",
                '<form><input name="userId"><input name="_csrf"><input name="lt">'
                '<input name="execution"><input name="_eventId"></form>',
                "Your email address, CRN or password is not correct",
            ],
            "Credential login failed",
        )

    def _assert_actionable(self, pages: list[str], message: str) -> None:
        with slc_client._client() as client:  # noqa: SLF001
            client_with_transport = httpx2.Client(
                transport=httpx2.MockTransport(
                    lambda _: httpx2.Response(200, text=pages.pop(0)),
                ),
                event_hooks=client.event_hooks,
            )

        with (
            patch("slc_client._client", return_value=client_with_transport),
            self.assertRaisesRegex(SlcError, message) as raised,
        ):
            fetch_loan_summary(username="user", password="test", secret_answer="test")

        assert not isinstance(raised.exception, SlcTemporaryError)  # noqa: S101


if __name__ == "__main__":
    unittest.main()
