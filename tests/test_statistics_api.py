"""Validate the native multi-step flow contract and option preservation."""

# ruff: noqa: PT009, PT027 — Tests use the standard-library unittest runner.

from __future__ import annotations

import unittest
from typing import Literal

from statistics_manager.api import HomeAssistant
from statistics_manager.model import Policy, Record


class FlowAPI(HomeAssistant):
    """Serve captured-shape flow responses and retain outbound requests."""

    def __init__(self, replies: list[Record]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, Record]] = []

    async def rest(
        self,
        method: Literal["get", "post", "delete"],
        path: str,
        **data: object,
    ) -> Record:
        """Return the next native-shaped response."""
        self.calls.append((method, path, data))
        return self.replies.pop(0)


def form(step: str, **extra: object) -> Record:
    """Build a Home Assistant flow form response."""
    return {
        "type": "form",
        "step_id": step,
        "flow_id": "test-flow",
        "errors": {},
        **extra,
    }


def options(**overrides: object) -> Record:
    """Expose current values using HA's suggested-value schema metadata."""
    values = Policy().options("sensor.test") | overrides
    return form(
        "init",
        data_schema=[
            {"name": key, "description": {"suggested_value": value}}
            for key, value in values.items()
        ],
    )


class FlowTests(unittest.IsolatedAsyncioTestCase):
    """Exercise creation, no-op options and source updates through the native flow."""

    async def test_three_step_creation(self) -> None:
        """Submit each stage independently and retain the returned config-entry ID."""
        api = FlowAPI(
            [
                form("user"),
                form("state_characteristic"),
                form("options"),
                {"type": "create_entry", "result": {"entry_id": "entry"}},
            ],
        )
        entry_id = await api.create(Policy(), "stable-source", "sensor.test")
        self.assertEqual(entry_id, "entry")
        self.assertEqual(
            api.calls[1][2],
            {"name": Policy().marker("stable-source"), "entity_id": "sensor.test"},
        )
        self.assertEqual(api.calls[2][2], {"state_characteristic": "change"})
        self.assertEqual(api.calls[3][2], Policy().options("sensor.test"))

    async def test_healthy_options_are_read_without_reload(self) -> None:
        """Abort an unchanged options flow, accepting equivalent duration units."""
        api = FlowAPI(
            [options(max_age={"days": 3, "hours": 0}), {"message": "Flow aborted"}],
        )
        self.assertFalse(await api.configure(Policy(), "entry", "sensor.test"))
        self.assertEqual(api.calls[-1][0], "delete")

    async def test_source_rename_updates_in_place(self) -> None:
        """Keep the config entry and submit the current source plus complete policy."""
        api = FlowAPI([options(), {"type": "create_entry"}])
        self.assertTrue(await api.configure(Policy(), "entry", "sensor.renamed"))
        self.assertEqual(api.calls[-1][2], Policy().options("sensor.renamed"))

    async def test_sample_count_cap_is_replaced_by_time_window(self) -> None:
        """Clear a pre-existing count cap with the native omission semantics."""
        api = FlowAPI([options(sampling_size=20), {"type": "create_entry"}])
        self.assertTrue(await api.configure(Policy(), "entry", "sensor.test"))
        self.assertEqual(api.calls[-1][2], Policy().options("sensor.test"))

    async def test_validation_error_is_not_a_success(self) -> None:
        """Reject validation failures before claiming a helper was created."""
        api = FlowAPI([form("user"), form("user", errors={"base": "invalid_source"})])
        with self.assertRaises(ValueError):
            await api.create(Policy(), "stable-source", "sensor.test")
