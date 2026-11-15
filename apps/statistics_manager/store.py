"""Atomic ownership metadata storage, recoverable from native registry provenance."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from .model import Record, record


class Store:
    """Persist successful mappings and the resolved selection-label identity."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.corrupt = False
        try:
            self.data = record(json.loads(path.read_text()))
            self.validate(self.data)
        except FileNotFoundError:
            self.data = {"version": 1, "helpers": {}}
        except (ValueError, TypeError):
            self.corrupt = True
            self.data = {"version": 1, "helpers": {}}

    @staticmethod
    def validate(data: Record) -> None:
        """Reject unsupported or malformed persisted ownership metadata."""
        if data.get("version") != 1:
            raise ValueError("Unsupported ownership file version")
        record(data.get("helpers"))

    def save(self, helpers: Record, label_id: str) -> None:
        """Replace the mapping only after successful native-state verification."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        data: Record = {"version": 1, "label_id": label_id, "helpers": helpers}
        with temporary.open("w") as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        self.data = data
        self.corrupt = False
