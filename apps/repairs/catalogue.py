"""Pure repair lifecycle, bounded context and SQLite analysis history."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Generator

MAX_NOTE_LENGTH = 255
MAX_ATTEMPTS = 3

PROMPT_VERSION = "1"
FIELDS = (
    "title",
    "explanation",
    "impact",
    "evidence",
    "steps",
    "uncertainties",
    "involvement",
)
ENTITY_PATTERN = re.compile(r"\b([a-z_]+\.[a-z0-9_]+)\b")
LIMITS = {
    "title": 120,
    "explanation": 1200,
    "impact": 600,
    "evidence": 1600,
    "steps": 2000,
    "uncertainties": 800,
    "involvement": 400,
}
KITCHEN_KEY = "spook:lovelace_unknown_entity_references_dashboard-mobile"
KITCHEN_NOTE = (
    "Re-pair the original kitchen radiator; never substitute the small radiator. "
    "2026-09-08 investigation: Z2M device leave on 2026-08-21, no rejoin found. "
    "Historical evidence: verify current condition."
)


def now() -> str:
    """Return an unambiguous timestamp."""
    return datetime.now(UTC).isoformat()


def encode(value: object) -> str:
    """Serialize canonical JSON for comparison and persistence."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def clean_text(value: str, limit: int = 8000) -> str:
    """Redact common credentials and addresses, then bound untrusted text."""
    value = re.sub(r"https?://\S+", "[URL omitted]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[email omitted]", value)
    value = re.sub(
        r"(?i)(token|password|secret|api[_ -]?key)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        value,
    )
    return value if len(value) <= limit else value[:limit] + " [truncated]"


def metadata(issue: dict[str, Any]) -> dict[str, Any]:
    """Copy only the public repair fields needed by the model and UI."""
    raw_placeholders = issue.get("translation_placeholders") or {}
    placeholders = {}
    remaining = 16000
    for key, value in sorted(raw_placeholders.items()):
        if remaining <= 0:
            break
        text = clean_text(str(value), min(remaining, 8000))
        placeholders[clean_text(str(key), 120)] = text
        remaining -= len(text)
    return {
        name: clean_text(str(issue.get(name) or ""), 8000)
        for name in (
            "domain",
            "issue_id",
            "issue_domain",
            "severity",
            "translation_key",
            "breaks_in_ha_version",
        )
    } | {
        "translation_placeholders": placeholders,
        "placeholders_omitted": len(raw_placeholders) - len(placeholders),
        "source_fingerprint": fingerprint(raw_placeholders),
    }


def fingerprint(value: object) -> str:
    """Identify the exact model input without volatile timestamps."""
    return hashlib.sha256(encode(value).encode()).hexdigest()


def validate_analysis(value: object) -> dict[str, str]:
    """Accept complete, bounded structured output only."""
    if not isinstance(value, dict):
        raise TypeError("AI response must be an object")
    result = {}
    for field in FIELDS:
        text = value.get(field)
        if not isinstance(text, str) or not text.strip() or len(text) > LIMITS[field]:
            raise ValueError(f"Invalid AI field: {field}")
        result[field] = clean_text(text.strip(), LIMITS[field])
    return result


class Catalogue:
    """Persist lifecycle and revision history; never use HA Recorder as storage."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS repairs (
                    key TEXT PRIMARY KEY, metadata TEXT NOT NULL, status TEXT NOT NULL,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, resolved_at TEXT,
                    occurrence INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '',
                    input TEXT NOT NULL DEFAULT '{}', fingerprint TEXT NOT NULL DEFAULT '',
                    analysis TEXT, analysis_fingerprint TEXT, generated_at TEXT,
                    generation_status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS analyses (
                    id INTEGER PRIMARY KEY, repair_key TEXT NOT NULL,
                    occurrence INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    input TEXT NOT NULL, output TEXT NOT NULL, generated_at TEXT NOT NULL
                );
                PRAGMA user_version = 1;
            """)
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        """Open a short-lived connection; callers commit transactionally."""
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def records(self) -> list[dict[str, Any]]:
        """Return detached records in a stable order."""
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM repairs ORDER BY key")]

    def reconcile(self, issues: list[dict[str, Any]]) -> None:
        """Apply only a successful full snapshot, preserving notes and analyses."""
        timestamp = now()
        keys = set()
        with self.connect() as db:
            for issue in issues:
                key = f"{issue['domain']}:{issue['issue_id']}"
                keys.add(key)
                status = "dismissed" if issue.get("ignored") else "active"
                note = KITCHEN_NOTE if key == KITCHEN_KEY else ""
                db.execute(
                    """
                    INSERT INTO repairs(key, metadata, status, first_seen, last_seen, note)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        metadata=excluded.metadata, status=excluded.status,
                        last_seen=excluded.last_seen, resolved_at=NULL,
                        occurrence=repairs.occurrence + (repairs.status='resolved')
                """,
                    (key, encode(metadata(issue)), status, timestamp, timestamp, note),
                )
            for row in db.execute(
                "SELECT key FROM repairs WHERE status != 'resolved'",
            ).fetchall():
                if row["key"] not in keys:
                    db.execute(
                        "UPDATE repairs SET status='resolved', resolved_at=? WHERE key=?",
                        (timestamp, row["key"]),
                    )

    def set_input(self, key: str, context: dict[str, Any], ai_identity: str) -> None:
        """Invalidate analysis only when meaningful inputs change."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM repairs WHERE key=?", (key,)).fetchone()
            if row is None:
                return
            value = {
                "repair": json.loads(row["metadata"]),
                "context": context,
                "user_note": clean_text(row["note"]),
                "prompt_version": PROMPT_VERSION,
                "ai_identity": ai_identity,
                "occurrence": row["occurrence"],
            }
            digest = fingerprint(value)
            if digest != row["fingerprint"]:
                db.execute(
                    """UPDATE repairs SET input=?, fingerprint=?, attempts=0,
                           retry_at=0, generation_status='pending' WHERE key=?""",
                    (encode(value), digest, key),
                )

    def save_note(self, key: str, note: str) -> None:
        """Keep user-authored instructions separate from model output."""
        if len(note) > MAX_NOTE_LENGTH:
            raise ValueError("Notes must be at most 255 characters")
        with self.connect() as db:
            db.execute("UPDATE repairs SET note=? WHERE key=?", (note, key))

    def request_analysis(self, key: str) -> None:
        """Allow an explicit retry even when the fingerprint is unchanged."""
        with self.connect() as db:
            db.execute(
                """UPDATE repairs SET generation_status='pending', attempts=0,
                       retry_at=0 WHERE key=? AND status='active'""",
                (key,),
            )

    def failed(self, key: str, digest: str, clock: float) -> None:
        """Bound retries to the initial attempt plus one and five minutes."""
        with self.connect() as db:
            row = db.execute("SELECT * FROM repairs WHERE key=?", (key,)).fetchone()
            if row is None or row["fingerprint"] != digest:
                return
            attempts = row["attempts"] + 1
            delay = 60 if attempts == 1 else 300
            db.execute(
                """UPDATE repairs SET attempts=?, retry_at=?, generation_status=?
                       WHERE key=?""",
                (
                    attempts,
                    clock + delay,
                    "failed" if attempts >= MAX_ATTEMPTS else "retrying",
                    key,
                ),
            )

    def complete(self, key: str, digest: str, analysis: dict[str, str]) -> bool:
        """Commit only if the repair is still active and input is current."""
        analysis = validate_analysis(analysis)
        with self.connect() as db:
            row = db.execute("SELECT * FROM repairs WHERE key=?", (key,)).fetchone()
            if row is None or row["status"] != "active" or row["fingerprint"] != digest:
                return False
            timestamp = now()
            db.execute(
                """INSERT INTO analyses(repair_key, occurrence, fingerprint, input,
                       output, generated_at) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    key,
                    row["occurrence"],
                    digest,
                    row["input"],
                    encode(analysis),
                    timestamp,
                ),
            )
            db.execute(
                """UPDATE repairs SET analysis=?, analysis_fingerprint=?, generated_at=?,
                       generation_status='ready', attempts=0, retry_at=0 WHERE key=?""",
                (encode(analysis), digest, timestamp, key),
            )
        return True

    def projection(self, status: str) -> list[dict[str, Any]]:
        """Build a bounded UI copy while retaining full local history."""
        rows = [row for row in self.records() if row["status"] == status]
        if status == "resolved":
            rows.sort(key=lambda row: row["resolved_at"] or "", reverse=True)
            rows = rows[:20]
        else:
            severity = {"critical": 0, "error": 1, "warning": 2}
            rows.sort(
                key=lambda row: (
                    severity.get(json.loads(row["metadata"])["severity"], 3),
                    row["first_seen"],
                ),
            )
        return [
            {
                "key": row["key"],
                "repair": json.loads(row["metadata"]),
                "analysis": json.loads(row["analysis"]) if row["analysis"] else None,
                "note": row["note"],
                "status": row["generation_status"],
                "stale": bool(
                    row["analysis"] and row["analysis_fingerprint"] != row["fingerprint"],
                ),
                "generated_at": row["generated_at"],
                "resolved_at": row["resolved_at"],
                "first_seen": row["first_seen"],
                "occurrence": row["occurrence"],
            }
            for row in rows
        ]
