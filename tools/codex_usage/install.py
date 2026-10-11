"""Install and verify the pinned official Codex Linux package."""

# ruff: noqa: T201 — Startup reports the verified runtime version to the operator.

from __future__ import annotations

import hashlib
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx2

VERSION = "0.154.0"
SHA256 = "fc6e3e3b85f2cf7d664520ee5c66a7fe4aa12bae7d46834f47e2f165fd0d6f78"
URL = f"https://github.com/openai/codex/releases/download/rust-v{VERSION}/codex-package-x86_64-unknown-linux-musl.tar.gz"
ROOT = Path("/data/codex")


def digest(path: Path) -> str:
    """Hash a local file without loading it into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def verify(archive: Path, installation: Path) -> bool:
    """Compare every installed package file with the checksum-verified archive."""
    if not installation.is_dir():
        return False

    with tarfile.open(archive, "r:gz") as package:
        for member in package:
            if not member.isfile():
                continue
            target = installation / member.name
            if target.is_symlink() or not target.is_file():
                return False
            if not target.resolve().is_relative_to(installation.resolve()):
                return False

            source = package.extractfile(member)
            if source is None:
                return False
            with source:
                expected = hashlib.file_digest(source, "sha256").hexdigest()
            if digest(target) != expected:
                return False
    return True


def main() -> None:
    """Install atomically, preserving active credentials and existing runtimes."""
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise RuntimeError("This installer requires Linux amd64 inside AppDaemon")

    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    ROOT.chmod(0o700)
    packages = ROOT / "packages"
    packages.mkdir(exist_ok=True, mode=0o700)
    archive = packages / f"codex-{VERSION}.tar.gz"
    if not archive.exists() or digest(archive) != SHA256:
        partial = archive.with_suffix(".partial")
        try:
            with httpx2.stream("GET", URL, follow_redirects=True, timeout=60) as response:
                response.raise_for_status()
                with partial.open("wb") as destination:
                    for chunk in response.iter_bytes():
                        destination.write(chunk)
            if digest(partial) != SHA256:
                raise RuntimeError("Codex package checksum did not match")
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)

    current = ROOT / "runtime"
    if verify(archive, current):
        print(f"Codex {VERSION} verified")
        return

    runtimes = ROOT / "runtimes"
    runtimes.mkdir(exist_ok=True, mode=0o700)
    installation = Path(tempfile.mkdtemp(prefix=f"{VERSION}-", dir=runtimes))
    try:
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(installation, filter="data")
        if not verify(archive, installation):
            raise RuntimeError("Extracted Codex package failed verification")  # noqa: TRY301 — Cleanup must also cover extraction and symlink failures.

        replacement = ROOT / "runtime.next"
        replacement.unlink(missing_ok=True)
        replacement.symlink_to(installation, target_is_directory=True)
        replacement.replace(current)
    except BaseException:
        shutil.rmtree(installation)
        raise

    print(f"Codex {VERSION} installed")


if __name__ == "__main__":
    main()
