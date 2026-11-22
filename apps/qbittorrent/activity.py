"""Persist upload observations used to retain productive torrents."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

DAY = 86400
SAMPLE_INTERVAL = 300
MAX_INTERPOLATION_GAP = SAMPLE_INTERVAL * 3
MEANINGFUL_BYTES = 1024 * 1024
ACTIVITY_GRACE = 2 * 3600


@dataclass(frozen=True)
class Activity:
    """Daily upload per byte stored and the last meaningful upload time."""

    productivity: float
    last_activity: float


def uploaded_since(samples: list[tuple[float, int]], cutoff: float) -> float:
    """Estimate uploads in a window ending at the latest sample.

    Args:
        samples: Chronologically ordered timestamps and cumulative upload counters.
        cutoff: Start of the window as a Unix timestamp.

    Returns:
        Bytes uploaded in the window, counting uploads across a long gap at
        the first sample after the gap.
    """
    baseline = float(samples[0][1])
    for previous, current in pairwise(samples):
        if current[0] <= cutoff:
            baseline = float(current[1])
        elif previous[0] < cutoff and current[0] - previous[0] <= MAX_INTERPOLATION_GAP:
            fraction = (cutoff - previous[0]) / (current[0] - previous[0])
            baseline = previous[1] + fraction * (current[1] - previous[1])
            break
        else:
            break

    return max(samples[-1][1] - baseline, 0.0)


class ActivityHistory:
    """Persist cumulative upload counters across sampling gaps."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS torrents ("
                "hash TEXT PRIMARY KEY, since REAL, completion INTEGER, activity REAL);"
                "CREATE TABLE IF NOT EXISTS samples ("
                "hash TEXT, time REAL, uploaded INTEGER, PRIMARY KEY (hash, time));",
            )

    def observe(
        self,
        torrent_hash: str,
        *,
        uploaded: int,
        completion: int,
        size: int,
        now: float,
    ) -> Activity | None:
        """Record an upload counter and evaluate the seeding grace periods.

        Args:
            torrent_hash: Info hash of the torrent.
            uploaded: Cumulative bytes uploaded.
            completion: Completion time as a Unix timestamp.
            size: Torrent size in bytes.
            now: Observation time as a Unix timestamp.

        Returns:
            Activity score and last meaningful upload time when both grace
            periods have elapsed, or None while the torrent is protected.
        """
        if uploaded < 0 or completion <= 0 or completion > now or size <= 0:
            self.forget(torrent_hash)
            return None

        with closing(sqlite3.connect(self.path)) as connection, connection:
            state = connection.execute(
                "SELECT since, completion, activity FROM torrents WHERE hash = ?",
                (torrent_hash,),
            ).fetchone()
            samples = [
                (float(row[0]), int(row[1]))
                for row in connection.execute(
                    "SELECT time, uploaded FROM samples WHERE hash = ? ORDER BY time",
                    (torrent_hash,),
                )
            ]
            if (
                state is None
                or not samples
                or int(state[1]) != completion
                or now < samples[-1][0]
                or uploaded < samples[-1][1]
            ):
                connection.execute("DELETE FROM samples WHERE hash = ?", (torrent_hash,))
                connection.execute(
                    "INSERT OR REPLACE INTO torrents VALUES (?, ?, ?, ?)",
                    (torrent_hash, now, completion, now),
                )
                state = (now, completion, now)
                samples = []

            since, _, last_activity = state
            since = float(since)
            last_activity = float(last_activity)
            delta = uploaded - samples[-1][1] if samples else 0
            if samples and now == samples[-1][0]:
                samples[-1] = (now, uploaded)
            else:
                samples.append((now, uploaded))

            if delta > 0 and uploaded_since(samples, now - 3600) >= MEANINGFUL_BYTES:
                last_activity = now

            connection.execute(
                "INSERT OR REPLACE INTO samples VALUES (?, ?, ?)",
                (torrent_hash, now, uploaded),
            )
            connection.execute(
                "UPDATE torrents SET activity = ? WHERE hash = ?",
                (last_activity, torrent_hash),
            )
            connection.execute(
                "DELETE FROM samples WHERE hash = ? AND time < ?",
                (torrent_hash, now - 8 * DAY),
            )

        if now - max(since, completion) < DAY or now - last_activity < ACTIVITY_GRACE:
            return None

        scores = [
            uploaded_since(samples, now - window)
            / size
            / (min(now - since, window) / DAY)
            for window in (DAY, 7 * DAY)
        ]
        return Activity(max(scores), last_activity)

    def forget(self, torrent_hash: str) -> None:
        """Discard observations for an incomplete or removed torrent."""
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("DELETE FROM samples WHERE hash = ?", (torrent_hash,))
            connection.execute("DELETE FROM torrents WHERE hash = ?", (torrent_hash,))

    def retain(self, hashes: set[str]) -> None:
        """Bound storage to torrents still present in a complete client snapshot."""
        with closing(sqlite3.connect(self.path)) as connection:
            known = {
                str(row[0]) for row in connection.execute("SELECT hash FROM torrents")
            }

        for torrent_hash in known - hashes:
            self.forget(torrent_hash)
