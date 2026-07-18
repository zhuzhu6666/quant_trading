#!/usr/bin/env python3
"""Compile or verify the Python 3.12 pip-tools lock files."""

from __future__ import annotations

import argparse
import importlib.metadata
import os
from pathlib import Path
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
    command = [
        sys.executable,
        "-m",
        "piptools",
        "compile",
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
            _compile(source, generated)
            committed = ROOT / target
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
