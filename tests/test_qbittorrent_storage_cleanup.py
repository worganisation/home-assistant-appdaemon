"""Regression coverage for cleanup notifications after threshold changes."""

# Keep unittest assertions so these tests need no additional test-runner dependency.
# ruff: noqa: PT009

from __future__ import annotations

from unittest import TestCase, main
from unittest.mock import patch

from apps.qbittorrent.storage_cleanup import (
    ACTION_PREFIX,
    QbittorrentStorageCleanup,
    TorrentCandidate,
)

STORAGE_ENTITY = "sensor.storage_usage"
THRESHOLD_ENTITY = "input_number.cleanup_threshold"
TORRENT_HASH = "a" * 40


class ThresholdNotificationTests(TestCase):
    """Exercise registered callbacks without contacting Home Assistant or qBittorrent."""

    def setUp(self) -> None:
        """Initialize the app with mocked service, state, scheduling, and API boundaries."""
        self.app = object.__new__(QbittorrentStorageCleanup)
        self.app.args = {
            "storage_entity": STORAGE_ENTITY,
            "threshold_entity": THRESHOLD_ENTITY,
            "threshold": 99.9,
            "reset_below": 98.0,
            "qbittorrent_url": "http://qbittorrent.invalid",
            "qbittorrent_username": "",
            "qbittorrent_password": "",
        }
        self.states: dict[str, str | None] = {
            STORAGE_ENTITY: "95",
            THRESHOLD_ENTITY: "99.9",
        }
        self.enterContext(
            patch.object(self.app, "get_state", side_effect=self.states.get),
        )
        self.services = self.enterContext(patch.object(self.app, "call_service"))
        self.scheduler = self.enterContext(patch.object(self.app, "run_in"))
        state_listeners = self.enterContext(patch.object(self.app, "listen_state"))
        event_listener = self.enterContext(patch.object(self.app, "listen_event"))
        self.enterContext(patch.object(self.app, "log"))
        client_type = self.enterContext(
            patch("apps.qbittorrent.storage_cleanup.QbittorrentWebApi", autospec=True),
        )
        self.client = client_type.return_value
        self.client.restart_errored.return_value = []
        self.client.ranked_seeders.return_value = [
            TorrentCandidate(
                hash=TORRENT_HASH,
                name="Completed torrent",
                size=1024,
                ratio=4.0,
                ratio_limit=5.0,
                upload_speed=0,
                seeding_seconds=3600,
                time_limit_seconds=None,
                closeness=0.8,
                deletion_score=0.8,
                closest_limit="ratio",
            ),
        ]
        self.app.initialize()
        self.callbacks = {
            call.args[1]: call.args[0] for call in state_listeners.call_args_list
        }
        self.notification_action = event_listener.call_args.args[0]

    def change_state(self, entity: str, value: str | None) -> None:
        """Deliver a state change through the listener registered during initialization."""
        old = self.states[entity]
        self.states[entity] = value
        self.callbacks[entity](entity, "state", old, value)

    def test_changed_threshold_offers_cleanup_at_or_below_usage(self) -> None:
        """Each changed threshold that is met sends the actionable notification."""
        for threshold in ("95", "90", "85", "92"):
            with self.subTest(threshold=threshold):
                self.services.reset_mock()
                self.change_state(THRESHOLD_ENTITY, threshold)
                self.services.assert_called_once()
                self.assertEqual(self.services.call_args.args, ("script/turn_on",))
                self.assertEqual(
                    self.services.call_args.kwargs["entity_id"],
                    "script.notify_will",
                )
                variables = self.services.call_args.kwargs["variables"]
                self.assertEqual(variables["title"], "Delete qBittorrent torrent?")
                self.assertEqual(
                    variables["actions"],
                    [
                        {
                            "action": f"{ACTION_PREFIX}{TORRENT_HASH}",
                            "title": "Delete torrent",
                        },
                    ],
                )
        self.client.delete_with_files.assert_not_called()

    def test_already_exceeded_previous_threshold_without_active_prompt(self) -> None:
        """A previous limit below consumption does not suppress the new prompt."""
        self.states[THRESHOLD_ENTITY] = "90"
        self.change_state(THRESHOLD_ENTITY, "85")
        self.services.assert_called_once()

    def test_active_cleanup_in_reset_band_does_not_suppress_threshold_change(
        self,
    ) -> None:
        """A latched cleanup cycle can be prompted again by changing its limit."""
        self.states[STORAGE_ENTITY] = "100"
        self.scheduler.call_args.args[0]({})
        self.change_state(STORAGE_ENTITY, "98.5")
        self.services.reset_mock()
        self.change_state(THRESHOLD_ENTITY, "98")
        self.services.assert_called_once()

    def test_unchanged_numeric_threshold_and_storage_updates_do_not_repeat_prompt(
        self,
    ) -> None:
        """Normal sensor updates and numeric formatting changes stay deduplicated."""
        self.change_state(THRESHOLD_ENTITY, "90")
        self.services.reset_mock()
        self.change_state(THRESHOLD_ENTITY, "90.0")
        self.change_state(STORAGE_ENTITY, "94")
        self.change_state(STORAGE_ENTITY, "96")
        self.services.assert_not_called()

    def test_threshold_above_usage_does_not_offer_cleanup(self) -> None:
        """A changed limit that has not been reached does not prompt."""
        self.change_state(THRESHOLD_ENTITY, "96")
        self.services.assert_not_called()
        self.client.ranked_seeders.assert_not_called()

    def test_raising_threshold_clears_prompt_below_reset_level(self) -> None:
        """Raising the limit keeps the existing recovery behavior."""
        self.change_state(THRESHOLD_ENTITY, "90")
        self.services.reset_mock()
        self.change_state(THRESHOLD_ENTITY, "99.9")
        self.services.assert_called_once()
        self.assertTrue(self.services.call_args.kwargs["variables"]["clear_notification"])
        self.client.restart_errored.assert_called_once()

    def test_unavailable_states_do_not_offer_cleanup(self) -> None:
        """Both the new threshold and current storage usage must be numeric."""
        for entity in (STORAGE_ENTITY, THRESHOLD_ENTITY):
            for invalid in (None, "unknown", "unavailable"):
                with self.subTest(entity=entity, value=invalid):
                    self.states.update({STORAGE_ENTITY: "95", THRESHOLD_ENTITY: "99.9"})
                    if entity == STORAGE_ENTITY:
                        self.states[entity] = invalid
                        self.change_state(THRESHOLD_ENTITY, "90")
                    else:
                        self.change_state(THRESHOLD_ENTITY, invalid)
                    self.services.assert_not_called()
        self.client.ranked_seeders.assert_not_called()

    def test_threshold_change_waits_for_post_delete_storage_refresh(self) -> None:
        """A pending deletion check honors the new threshold after its existing delay."""
        self.change_state(THRESHOLD_ENTITY, "90")
        self.notification_action(
            "mobile_app_notification_action",
            {"action": f"{ACTION_PREFIX}{TORRENT_HASH}"},
        )
        self.client.delete_with_files.assert_called_once_with(TORRENT_HASH)
        self.services.reset_mock()
        self.change_state(STORAGE_ENTITY, "87")
        self.change_state(THRESHOLD_ENTITY, "85")
        self.services.assert_not_called()
        self.assertEqual(self.scheduler.call_args.args[1], 90)
        self.scheduler.call_args.args[0]({})
        self.services.assert_called_once()
        self.assertEqual(
            self.services.call_args.kwargs["variables"]["title"],
            "Delete qBittorrent torrent?",
        )


if __name__ == "__main__":
    main()
