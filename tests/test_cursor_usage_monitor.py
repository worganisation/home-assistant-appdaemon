"""Tests for defensive Cursor usage payload handling."""

# ruff: noqa: PLR2004, S101

from __future__ import annotations

import unittest

from apps.cursor.usage_monitor import SENSORS, CursorUsageMonitor


class CursorUsagePayloadTests(unittest.TestCase):
    """Cover empty API aggregations and initial error-state publishing."""

    def test_missing_model_aggregations_are_empty_usage(self) -> None:
        """A new billing period without model events is valid, not an error."""
        models, total_cents = CursorUsageMonitor._normalize_models(None)  # noqa: SLF001

        assert models == {}
        assert total_cents == 0.0

    def test_model_aggregations_are_normalized(self) -> None:
        """Populated aggregation responses retain their model attributes."""
        models, total_cents = CursorUsageMonitor._normalize_models(  # noqa: SLF001
            [
                {
                    "modelIntent": "composer",
                    "inputTokens": "10",
                    "outputTokens": "5",
                    "cacheReadTokens": "20",
                    "totalCents": 12.345,
                },
            ],
        )

        assert total_cents == 12.345
        assert models == {
            "composer": {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 20,
                "cache_write_tokens": 0,
                "total_cents": 12.35,
            },
        }

    def test_unknown_state_contains_every_discovery_template_key(self) -> None:
        """An initial failed poll cannot publish missing MQTT template fields."""
        state = CursorUsageMonitor._unknown_state()  # noqa: SLF001

        for sensor in SENSORS:
            assert sensor.key in state
            if sensor.attributes_key is not None:
                assert state[sensor.attributes_key] == {}


if __name__ == "__main__":
    unittest.main()
