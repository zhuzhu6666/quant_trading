#!/usr/bin/env python3
"""Compile or verify the Python 3.12 pip-tools lock files."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PIP_TOOLS_VERSION = "7.5.3"
LOCKS = (
    ("requirements.in", "requirements.txt"),
    ("requirements-dev.in", "requirements-dev.txt"),
)


def _require_toolchain() -> None:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit(
            f"Python 3.12 is required to compile locks; got {sys.version.split()[0]}"
        )
    try:
        installed = importlib.metadata.version("pip-tools")
    except importlib.metadata.PackageNotFoundError as exc:
        raise SystemExit(
            f"pip-tools {PIP_TOOLS_VERSION} is required; install it before compiling"
        ) from exc
    if installed != PIP_TOOLS_VERSION:
        raise SystemExit(
            f"pip-tools {PIP_TOOLS_VERSION} is required; found {installed}"
        )


def _compile(source: str, output: Path) -> None:
    env = dict(os.environ)
    env["CUSTOM_COMPILE_COMMAND"] = "python scripts/compile_python_locks.py"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    # Developer shells may export a cache directory outside the workspace.  Do
    # not let an inherited, non-writable path make lock verification depend on
    # the caller's home-directory permissions.
    cache_dir = Path(tempfile.gettempdir()) / f"quant-trading-pip-tools-{os.getuid()}"
    cache_dir.mkdir(parents=True, exist_ok=True)
    env["PIP_TOOLS_CACHE_DIR"] = str(cache_dir)
    command = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
        "--quiet",
        "--allow-unsafe",
        "--generate-hashes",
        "--no-emit-index-url",
        "--resolver=backtracking",
        "--strip-extras",
        "--output-file",
        str(output),
        source,
    ]
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate into a temporary directory and fail when a lock differs",
    )
    args = parser.parse_args()
    _require_toolchain()

    if not args.check:
        for source, target in LOCKS:
            _compile(source, ROOT / target)
        return 0

    stale: list[str] = []
    with tempfile.TemporaryDirectory(prefix="quant-lock-check-") as temp_dir:
        temp = Path(temp_dir)
        for source, target in LOCKS:
            generated = temp / target
            committed = ROOT / target
            # Seed pip-compile with the checked-in lock so verification checks
            # that the input files still produce that lock, without silently
            # upgrading an otherwise valid transitive pin merely because a new
            # compatible release appeared on the package index.  Changed or
            # removed input constraints still force pip-compile to update the
            # seeded output and are caught by the byte comparison below.
            if committed.exists():
                shutil.copy2(committed, generated)
            _compile(source, generated)
            if not committed.exists() or committed.read_bytes() != generated.read_bytes():
                stale.append(target)
    if stale:
        raise SystemExit(
            "stale Python lock file(s): "
            + ", ".join(stale)
            + "; run `python scripts/compile_python_locks.py`"
        )
    print("Python dependency locks are current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
