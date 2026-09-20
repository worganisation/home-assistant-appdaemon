"""Normalize tracker data and calculate conservative local estimates."""

from __future__ import annotations

import html
import math
import re
from typing import Any
from urllib.parse import urlsplit

GIB = 1024**3
SEED_SECONDS = 72 * 3600
DAY = 86400
TRACKER_WORKING = 2
USE_GLOBAL_LIMIT = -2


def number(value: Any) -> float | None:
    """Return finite numbers, rejecting booleans and absent values."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def size_bytes(value: Any) -> float | None:
    """Parse documented formatted byte amounts without silently guessing units."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*([\d,.]+)\s*(B|[KMGT]i?B)\s*", value, re.IGNORECASE)
    if not match:
        return None
    amount = number(match[1])
    unit = match[2].upper()
    power = {"B": 0, "K": 1, "M": 2, "G": 3, "T": 4}[unit[0]]
    return None if amount is None else amount * (1024 if "I" in unit else 1000) ** power


def safe_text(value: Any, limit: int = 160) -> str:
    """Remove markup, URLs and credential-shaped strings from display text."""
    text = re.sub(r"<[^>]*>", "", html.unescape(str(value)))
    text = re.sub(r"https?://\S+|\b[a-fA-F0-9]{32,}\b", "[redacted]", text)
    text = re.sub(r"(?i)(mam_id|passkey|token|cookie)\s*[=:]\s*\S+", "[redacted]", text)
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"[\x00-\x1f\x7f]", " ", text)[:limit]


def is_mam_tracker(url: Any) -> bool:
    """Match DNS boundaries, never a substring in an authenticated URL."""
    try:
        host = urlsplit(str(url)).hostname or ""
    except ValueError:
        return False
    return host == "myanonamouse.net" or host.endswith(".myanonamouse.net")


def path_value(data: dict[str, Any], path: Any) -> Any:
    """Read explicitly configured response paths after their schema is verified."""
    if not isinstance(path, list) or not path:
        return None
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or not isinstance(key, str):
            return None
        current = current.get(key)
    return current


def count_value(value: Any) -> int | None:
    """Accept nonnegative integral counters without treating booleans as counts."""
    parsed = number(value)
    return (
        int(parsed)
        if parsed is not None and parsed >= 0 and parsed.is_integer()
        else None
    )


def mapped_count(data: dict[str, Any], path: Any) -> int | None:
    """Sum verified disjoint categories only when every component is available."""
    if isinstance(path, list) and path and all(isinstance(item, list) for item in path):
        counts = [count_value(path_value(data, item)) for item in path]
        return (
            sum(value for value in counts if value is not None)
            if all(value is not None for value in counts)
            else None
        )
    return count_value(path_value(data, path))


NOTICE_LABELS = {
    "pms": "{count} private-message notification{s}. Check your MAM inbox.",
    "aboutToDropClient": "{count} client{s} about to be dropped. Check MAM's client status.",
    "tickets": "{count} ticket notification{s}. Check your MAM tickets.",
    "waiting_tickets": "{count} waiting-ticket notification{s}. Check your MAM tickets.",
    "requests": "{count} request notification{s}. Check your MAM requests.",
    "topics": "{count} topic notification{s}. Check your MAM forum topics.",
}


def account_notices(value: Any) -> tuple[list[str], bool, dict[str, int]]:
    """Render allowlisted notification counters without exposing message contents."""
    if isinstance(value, list):
        return (
            [safe_text(item, 240) for item in value[:10] if isinstance(item, str)],
            all(isinstance(item, str) for item in value),
            {},
        )
    if not isinstance(value, dict):
        return [], False, {}
    counts = {key: count_value(value.get(key)) for key in NOTICE_LABELS}
    available = all(count is not None for count in counts.values())
    counters = {key: count for key, count in counts.items() if count is not None}
    messages = [
        NOTICE_LABELS[key].format(count=count, s="" if count == 1 else "s")
        for key, count in counters.items()
        if count
    ]
    if value.get("iCloudRelay") is True:
        messages.append("MAM reports iCloud Private Relay in use.")
    return messages, available, counters


def account_snapshot(data: dict[str, Any], paths: dict[str, Any]) -> dict[str, Any]:
    """Normalize documented fields; optional unmapped fields stay unknown."""
    uploaded = count_value(data.get("uploaded_bytes"))
    downloaded = count_value(data.get("downloaded_bytes"))
    if uploaded is None:
        uploaded = size_bytes(data.get("uploaded"))
    if downloaded is None:
        downloaded = size_bytes(data.get("downloaded"))
    if not data.get("uid") or uploaded is None or downloaded is None:
        raise ValueError("Unrecognized account response")
    result: dict[str, Any] = {
        "class": safe_text(data.get("classname", "unknown")),
        "uploaded": uploaded / GIB,
        "downloaded": downloaded / GIB,
        "ratio": number(data.get("ratio")),
        "bonus": number(data.get("seedbonus")),
        "credit": (uploaded - downloaded) / GIB,
    }
    for key in (
        "unsatisfied",
        "limit",
        "satisfied",
        "seeding",
        "leeching",
        "wedges",
        "hnr",
        "inactive_unsatisfied",
    ):
        result[key] = mapped_count(data, paths.get(key))
    connected = path_value(data, paths.get("connectable"))
    if isinstance(connected, str):
        connected = {"yes": True, "no": False}.get(connected.strip().lower())
    result["connectable"] = connected if isinstance(connected, bool) else None
    notices, available, counters = account_notices(path_value(data, paths.get("notices")))
    result["notices"] = notices
    result["notices_available"] = available
    result["notification_counts"] = counters
    result["notification_count"] = (
        sum(counters.values()) if available and counters else None
    )
    result["coverage"] = (
        "complete"
        if all(
            result[key] is not None
            for key in (
                "unsatisfied",
                "limit",
                "satisfied",
                "seeding",
                "leeching",
                "connectable",
                "ratio",
                "bonus",
            )
        )
        and result["notices_available"]
        else "partial"
    )
    return result


def unsatisfied_limit(
    account: dict[str, Any],
    override: int = 0,
) -> tuple[int | None, str]:
    """Prefer reported limits; never infer account age from the User class."""
    if account.get("limit") is not None:
        return account["limit"], "MAM"
    if override > 0:
        return override, "configured override"
    limits = {
        "user": 20,
        "power user": 100,
        "vip": 150,
        "elite vip": 200,
        "elite vip+": 200,
    }
    value = limits.get(str(account.get("class", "")).lower())
    return value, "conservative class fallback" if value else "unknown class"


def torrent_snapshot(  # noqa: C901 - independent torrent safety checks
    torrent: dict[str, Any],
    files: list[dict[str, Any]],
    trackers: list[dict[str, Any]],
    preferences: dict[str, Any],
    previous: dict[str, Any],
    now: float,
) -> dict[str, Any]:
    """Inspect every file; client seed time is never tracker-confirmed satisfaction."""
    complete = bool(files) and all(number(f.get("progress")) == 1 for f in files)
    skipped = sum(f.get("priority") == 0 for f in files)
    size = sum(float(f["size"]) for f in files)
    remaining = sum(float(f["size"]) * (1 - float(f["progress"])) for f in files)
    seeding = number(torrent.get("seeding_time"))
    reset = bool(previous.get("counter_reset")) or (
        seeding is not None
        and previous.get("seed_seconds") is not None
        and seeding < previous["seed_seconds"]
    )
    seed_seconds = seeding if complete and not skipped else None
    seed_remaining = (
        max(0, SEED_SECONDS - seed_seconds) if seed_seconds is not None else None
    )
    state = str(torrent.get("state", "unknown"))
    mam_trackers = [t for t in trackers if is_mam_tracker(t.get("url"))]
    tracker_ok = any(t.get("status") == TRACKER_WORKING for t in mam_trackers)
    limits: dict[str, float | None] = {}
    for name, global_flag, global_value in (
        ("ratio_limit", "max_ratio_enabled", "max_ratio"),
        ("seeding_time_limit", "max_seeding_time_enabled", "max_seeding_time"),
        (
            "inactive_seeding_time_limit",
            "max_inactive_seeding_time_enabled",
            "max_inactive_seeding_time",
        ),
    ):
        value = number(torrent.get(name))
        if value == USE_GLOBAL_LIMIT:
            value = (
                number(preferences.get(global_value))
                if preferences.get(global_flag)
                else -1
            )
        if (
            number(torrent.get(name)) == USE_GLOBAL_LIMIT
            and global_flag not in preferences
        ):
            value = None
        limits[name] = value
    issues = []
    if skipped:
        issues.append("skipped_files")
    if state in {"missingFiles", "error"}:
        issues.append("missing_files")
    if any(v is not None and v >= 0 for v in limits.values()):
        issues.append("retention_limit")
    if any(v is None for v in limits.values()):
        issues.append("retention_unknown")
    if not tracker_ok:
        issues.append("tracker_error")
    if complete and state not in {"uploading", "stalledUP", "forcedUP"}:
        issues.append("not_seeding")
    if reset:
        issues.append("counter_reset")
    added = number(torrent.get("added_on"))
    # Local added_on is only a conservative deadline proxy, not MAM's snatch date.
    deadline = added + 30 * DAY if added and added > 0 else None
    if deadline and seed_remaining and deadline - now - seed_remaining < DAY:
        issues.append("deadline_estimate")
    return {
        "name": safe_text(torrent.get("name", "Unnamed torrent")),
        "state": state,
        "size_gib": size / GIB,
        "remaining_gib": remaining / GIB,
        "complete": complete,
        "skipped_files": skipped,
        "file_count": len(files),
        "seed_seconds": seed_seconds,
        "seed_hours": None if seed_seconds is None else round(seed_seconds / 3600, 2),
        "hours_remaining": None
        if seed_remaining is None
        else round(seed_remaining / 3600, 2),
        "counter_reset": reset,
        "tracker_ok": tracker_ok,
        "limits": limits,
        "ratio": number(torrent.get("ratio")),
        "downloaded_gib": (number(torrent.get("downloaded")) or 0) / GIB,
        "uploaded_gib": (number(torrent.get("uploaded")) or 0) / GIB,
        "upload_speed": number(torrent.get("upspeed")),
        "download_speed": number(torrent.get("dlspeed")),
        "deadline_estimate": deadline,
        "last_seen": now,
        "issues": issues,
        "missing": False,
    }


def reconcile_torrents(
    current: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    """Retain disappeared torrents whose local seeding obligation is unresolved."""
    result = dict(current)
    for key, torrent in previous.items():
        if key not in result and (
            torrent.get("hours_remaining") != 0 or torrent.get("counter_reset")
        ):
            result[key] = {
                **torrent,
                "missing": True,
                "issues": ["missing_torrent"],
                "state": "missing",
            }
    return result


def assess(  # noqa: C901, PLR0912 - independent account and torrent checks
    account: dict[str, Any],
    torrents: dict[str, Any],
    *,
    mam_fresh: bool,
    qbt_fresh: bool,
    reserve: float,
    limit_override: int,
) -> tuple[dict[str, Any], dict[str, tuple[str, str]]]:
    """Return sensor values and stable issue IDs with severity and safe messages."""
    values = dict(account) if mam_fresh else {}
    issues: dict[str, tuple[str, str]] = {}
    if not mam_fresh:
        issues["mam_stale"] = ("warning", "MAM account data is unavailable or stale.")
    if not qbt_fresh:
        issues["qbt_stale"] = ("warning", "qBittorrent data is unavailable or stale.")
    limit, source = unsatisfied_limit(account, limit_override)
    values.update(
        {
            "limit": limit if mam_fresh else None,
            "limit_source": source,
            "reserve": reserve,
        },
    )
    if mam_fresh:
        if account.get("coverage") != "complete":
            issues["account_partial"] = (
                "warning",
                "Some MAM account fields are missing or unrecognized; coverage is partial.",
            )
        if account.get("ratio") is not None and account["ratio"] < 1:
            issues["ratio"] = ("critical", "MAM account ratio is below 1.0.")
        if account.get("connectable") is False:
            issues["connectable"] = (
                "warning",
                "MAM reports not connectable; this does not establish seed-time credit.",
            )
        if account.get("notices"):
            issues["site_notice"] = (
                "warning",
                "MAM: "
                + " ".join(
                    safe_text(message, 240) for message in account["notices"][:10]
                ),
            )
        if (account.get("hnr") or 0) > 0:
            issues["tracker_hnr"] = (
                "critical",
                f"MAM reports {account['hnr']} hit-and-run torrent(s). Review them on MAM.",
            )
        if (account.get("inactive_unsatisfied") or 0) > 0:
            issues["tracker_inactive_unsatisfied"] = (
                "warning",
                f"MAM reports {account['inactive_unsatisfied']} inactive unsatisfied torrent(s). "
                "Resume seeding them.",
            )
        count = account.get("unsatisfied")
        if count is not None and limit is not None:
            values["headroom"] = limit - count
            if count >= limit * 0.8:
                issues["slots"] = (
                    "critical" if count >= limit else "warning",
                    f"MAM unsatisfied torrents: {count} of {limit}.",
                )
    if qbt_fresh:
        values["torrent_count"] = len(torrents)
        values["outstanding"] = sum(t["remaining_gib"] for t in torrents.values())
        values["local_unsatisfied"] = sum(
            t.get("hours_remaining") != 0 for t in torrents.values()
        )
        values["attention_count"] = sum(bool(t["issues"]) for t in torrents.values())
        for key, torrent in torrents.items():
            for issue in torrent["issues"]:
                severity = (
                    "critical"
                    if issue
                    in {
                        "skipped_files",
                        "missing_files",
                        "missing_torrent",
                        "retention_limit",
                    }
                    else "warning"
                )
                issues[f"torrent:{key}:{issue}"] = (
                    severity,
                    f"{torrent['name']}: {issue.replace('_', ' ')}.",
                )
        if mam_fresh:
            values["projected_credit"] = account["credit"] - values["outstanding"]
            values["budget"] = values["projected_credit"] - reserve
            if values["budget"] < 0:
                issues["budget"] = (
                    "critical" if values["projected_credit"] <= 0 else "warning",
                    "Estimated credit after outstanding downloads is below your configured reserve.",
                )
    values["status"] = (
        "critical"
        if any(s == "critical" for s, _ in issues.values())
        else "attention"
        if issues
        else "no_detected_issues"
    )
    return values, issues
