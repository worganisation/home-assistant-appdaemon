"""Validated configuration, account identity and usage sensor values."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

if TYPE_CHECKING:
    from pathlib import Path

PROFILE_PATTERN = r"^[a-z][a-z0-9_-]{0,39}$"
STALE_SECONDS = 900


class Configuration(BaseModel):
    """AppDaemon arguments, validated together before opening resources."""

    model_config = ConfigDict(extra="ignore", strict=True)

    mqtt_host: Annotated[str, Field(min_length=1)]
    mqtt_port: Annotated[int, Field(ge=1, le=65535)] = 1883
    mqtt_username: Annotated[str, Field(min_length=1)]
    mqtt_password: SecretStr
    mqtt_discovery_prefix: str = "homeassistant"
    mqtt_base_topic: str = "appdaemon/codex_usage"
    poll_interval_seconds: Annotated[int, Field(ge=60, le=300)] = 300
    storage_path: str = "/data/codex"
    control_socket: str = "/share/codex-monitor/control.sock"
    binary_path: str = "/data/codex/runtime/bin/codex"


class Profile(BaseModel):
    """Identity bound to one private credential generation."""

    name: Annotated[str, Field(pattern=PROFILE_PATTERN)]
    generation: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    email: str
    account_id: str

    def home(self, root: Path) -> Path:
        """Locate credentials without interpreting user input as a path."""
        return root / "homes" / self.generation


class WireModel(BaseModel):
    """Accept additional server fields without storing them."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Account(WireModel):
    """Account identity returned by the app-server."""

    type: Literal["chatgpt"]
    email: str | None = None


class AccountResponse(WireModel):
    """Authenticated account details."""

    account: Account | None = None


class Window(WireModel):
    """A server-defined quota window."""

    used_percent: Annotated[int, Field(alias="usedPercent", strict=True)]
    duration: Annotated[int | None, Field(alias="windowDurationMins")] = None
    resets_at: Annotated[int | None, Field(alias="resetsAt")] = None


class Credits(WireModel):
    """Credit balance without inferred monetary value."""

    balance: str | None = None
    unlimited: bool = False


class Bucket(WireModel):
    """One metered bucket and its optional windows."""

    limit_id: Annotated[str | None, Field(alias="limitId")] = None
    limit_name: Annotated[str | None, Field(alias="limitName")] = None
    primary: Window | None = None
    secondary: Window | None = None
    credits: Credits | None = None


class Resets(WireModel):
    """Authoritative reset count; individual credit records are ignored."""

    available_count: Annotated[int, Field(alias="availableCount", ge=0)]


class Limits(WireModel):
    """Account quota snapshot."""

    account_id: Annotated[str | None, Field(alias="accountId")] = None
    rate_limits: Annotated[Bucket, Field(alias="rateLimits")]
    buckets: Annotated[dict[str, Bucket] | None, Field(alias="rateLimitsByLimitId")] = (
        None
    )
    resets: Annotated[Resets | None, Field(alias="rateLimitResetCredits")] = None


class UsageSummary(WireModel):
    """Optional lifetime token count."""

    lifetime_tokens: Annotated[int | None, Field(alias="lifetimeTokens", ge=0)] = None


class DailyUsage(WireModel):
    """A dated token activity bucket."""

    start_date: Annotated[date, Field(alias="startDate")]
    tokens: Annotated[int, Field(ge=0)]


class Usage(WireModel):
    """Token activity, independent of quota availability."""

    summary: UsageSummary
    daily: Annotated[list[DailyUsage] | None, Field(alias="dailyUsageBuckets")] = None


@dataclass(frozen=True)
class Reading:
    """A sensor value and its public MQTT metadata."""

    key: str
    name: str
    value: str | int | float | None
    group: str
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    attributes: dict[str, object] = field(default_factory=dict)


def timestamp(value: int | None) -> str | None:
    """Convert a provided reset timestamp to UTC."""
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def bucket_key(value: str) -> str:
    """Make collision-resistant MQTT identifiers for server bucket names."""
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")[:40]
    return f"{slug}_{hashlib.sha256(value.encode()).hexdigest()[:8]}"


def quota_readings(limits: Limits) -> list[Reading]:
    """Normalize all present windows without assuming a fixed duration."""
    readings = [
        Reading(
            "available_resets",
            "Available resets",
            limits.resets.available_count if limits.resets else None,
            "quota",
        ),
    ]
    buckets = limits.buckets
    if buckets is None:
        buckets = {limits.rate_limits.limit_id or "codex": limits.rate_limits}

    for limit_id, bucket in buckets.items():
        prefix = bucket_key(limit_id)
        label = bucket.limit_name or limit_id
        for slot in ("primary", "secondary"):
            window = getattr(bucket, slot)
            if not isinstance(window, Window):
                continue

            duration = window.duration
            title = f"{label} {slot}" if duration is None else f"{label} {duration} min"
            attrs: dict[str, object] = {
                "limit_id": limit_id,
                "window": slot,
                "window_duration_minutes": duration,
            }
            for metric, value in (
                ("used", window.used_percent),
                ("remaining", max(0, min(100, 100 - window.used_percent))),
            ):
                readings.append(
                    Reading(
                        f"{prefix}_{slot}_{metric}",
                        f"{title} {metric}",
                        value,
                        "quota",
                        "%",
                        state_class="measurement",
                        attributes=attrs,
                    ),
                )
            readings.append(
                Reading(
                    f"{prefix}_{slot}_reset",
                    f"{title} next reset",
                    timestamp(window.resets_at),
                    "quota",
                    device_class="timestamp",
                    attributes=attrs,
                ),
            )

        balance: float | None = None
        if (
            bucket.credits
            and not bucket.credits.unlimited
            and bucket.credits.balance is not None
        ):
            balance = float(bucket.credits.balance)
            if not math.isfinite(balance):
                raise ValueError("Invalid credit balance")

        readings.append(
            Reading(
                f"{prefix}_credits",
                f"{label} credit balance",
                balance,
                "quota",
                "credits",
                attributes={
                    "limit_id": limit_id,
                    "unlimited": bucket.credits.unlimited if bucket.credits else None,
                },
            ),
        )

    return readings


def token_readings(usage: Usage) -> list[Reading]:
    """Expose the latest reported day without assuming its timezone is local."""
    latest = max(usage.daily, key=lambda day: day.start_date) if usage.daily else None
    return [
        Reading(
            "lifetime_tokens",
            "Lifetime tokens",
            usage.summary.lifetime_tokens,
            "tokens",
            "tokens",
        ),
        Reading(
            "daily_tokens",
            "Latest reported daily tokens",
            latest.tokens if latest else None,
            "tokens",
            "tokens",
            attributes={"date": latest.start_date.isoformat() if latest else None},
        ),
        Reading(
            "daily_date",
            "Latest reported date",
            latest.start_date.isoformat() if latest else None,
            "tokens",
            device_class="date",
        ),
    ]
