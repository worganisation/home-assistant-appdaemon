"""Operate the private Codex onboarding socket through an existing SSH identity."""

# ruff: noqa: T201 — This command presents login instructions and results in the terminal.

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time

from dashboard import sections

SOCKET = "/share/codex-monitor/control.sock"


def request(
    target: str,
    hostname: str | None,
    method: str,
    path: str,
) -> dict[str, object]:
    """Use SSH transport without exposing a TCP API or changing host keys."""
    command = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "ConnectionAttempts=1",
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
    ]
    if hostname:
        command.extend(["-o", f"Hostname={hostname}"])
    remote = shlex.join(
        [
            "curl",
            "--silent",
            "--show-error",
            "--fail-with-body",
            "--max-time",
            "40",
            "--unix-socket",
            SOCKET,
            "--request",
            method,
            f"http://localhost{path}",
        ],
    )
    command.extend([target, remote])
    result = subprocess.run(  # noqa: S603 — SSH options are fixed and remote arguments are shell-quoted.
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=50,
    )
    if result.returncode:
        raise RuntimeError(
            "Cannot contact the Codex monitor. Check SSH access, deployment and AppDaemon logs.",
        )

    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise TypeError("Invalid monitor response")
    return value


def main() -> None:
    """Display a device login code locally and wait for the verified account."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["login", "status", "dashboard"])
    parser.add_argument("profile", nargs="?")
    parser.add_argument("--ssh-target", default="hass")
    parser.add_argument(
        "--hostname",
        help="Override only the SSH address, preserving its identity",
    )
    args = parser.parse_args()
    if args.ssh_target.startswith("-"):
        parser.error("SSH target must be a configured host")
    if args.action in {"status", "dashboard"}:
        payload = request(args.ssh_target, args.hostname, "GET", "/accounts")
        print(
            json.dumps(
                sections(payload) if args.action == "dashboard" else payload,
                indent=2,
            ),
        )
        return
    if not args.profile or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", args.profile):
        parser.error("Login requires a lowercase profile name of 1-40 characters")

    path = f"/login/{args.profile}"
    started = False
    complete = False
    try:
        response = request(args.ssh_target, args.hostname, "POST", path)
        started = True
        print(
            f"Open {response['verification_url']}\nEnter code: {response['user_code']}",
            flush=True,
        )
        print(
            "Sign into the account intended for this profile. Waiting for authorization…",
            flush=True,
        )
        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            time.sleep(2)
            status = request(args.ssh_target, args.hostname, "GET", path).get("status")
            if status == "complete":
                complete = True
                print(
                    json.dumps(
                        request(args.ssh_target, args.hostname, "GET", "/accounts"),
                        indent=2,
                    ),
                )
                return
            if status != "pending":
                raise RuntimeError(
                    f"Login ended: {status}. Existing credentials are unchanged.",
                )
        raise RuntimeError("Login timed out")
    finally:
        if started and not complete:
            try:
                request(args.ssh_target, args.hostname, "DELETE", path)
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                print(
                    "Could not cancel remotely; the pending login expires automatically.",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Login cancelled.", file=sys.stderr)
        sys.exit(130)
