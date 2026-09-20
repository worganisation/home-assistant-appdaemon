"""Privately provision the monitor's dedicated MAM API session; no API calls."""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path


def main() -> None:
    """Write an owner-only session file from an interactive hidden prompt."""
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "/data/mam")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    token = getpass.getpass("Dedicated MAM API session (hidden): ").strip()
    if not token or any(character.isspace() for character in token):
        raise SystemExit("Session must be nonempty and contain no whitespace")
    temporary = directory / "session.new"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump({"mam_id": token}, output)
    temporary.chmod(0o600)
    temporary.replace(directory / "session.json")
    print("Session saved privately. The monitor uses it on its next scheduled poll.")  # noqa: T201


if __name__ == "__main__":
    main()
