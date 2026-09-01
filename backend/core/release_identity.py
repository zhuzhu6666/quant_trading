"""Frozen source identity for a running backend process.

The identity is collected once when this module is imported by the backend.
Health requests only return the frozen value; they never run Git or rescan the
worktree.  A target-side preflight uses the same collector to compare the
current checkout with the process that answered ``/api/health``.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Mapping


SCHEMA_VERSION = "process_release_identity.v1"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# These fields describe the loaded code, rather than the individual process
# that collected them.  PID and capture time intentionally stay out of the
# cross-process comparison contract.
RELEASE_IDENTITY_MATCH_FIELDS = (
    "schema_version",
    "ok",
    "root",
    "head",
    "clean",
    "worktree_fingerprint",
    "tracked_and_unignored_file_count",
    "source",
)


def _result_parts(result: Any) -> tuple[int, str, str]:
    if isinstance(result, Mapping):
        return (
            int(result.get("returncode", 0) or 0),
            str(result.get("stdout") or ""),
            str(result.get("stderr") or ""),
        )
    return (
        int(getattr(result, "returncode", 0) or 0),
        str(getattr(result, "stdout", "") or ""),
        str(getattr(result, "stderr", "") or ""),
    )


def _run_git(
    command: tuple[str, ...],
    *,
    root: Path,
    runner: Callable[..., Any],
) -> tuple[int, str, str]:
    try:
        result = runner(
            command,
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
        return _result_parts(result)
    except Exception as exc:
        return 1, "", f"{type(exc).__name__}:{exc}"


def _split_nul(value: str) -> list[str]:
    return sorted({item for item in str(value or "").split("\0") if item})


def _hash_worktree(root: Path, head: str, paths: list[str]) -> tuple[str, str]:
    """Hash current tracked/non-ignored files once, independent of mtimes."""

    digest = hashlib.sha256()
    digest.update(SCHEMA_VERSION.encode("ascii"))
    digest.update(b"\0head\0")
    digest.update(head.encode("utf-8"))
    for relative in paths:
        path = root / relative
        digest.update(b"\0path\0")
        digest.update(relative.encode("utf-8", "surrogateescape"))
        try:
            if path.is_symlink():
                digest.update(b"\0symlink\0")
                digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
                continue
            stat = path.stat()
            digest.update(b"\0mode\0")
            digest.update(str(stat.st_mode & 0o7777).encode("ascii"))
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except FileNotFoundError:
            # A deleted tracked file is part of the current worktree identity,
            # not a collector failure.
            digest.update(b"\0missing\0")
        except OSError as exc:
            return "", f"worktree_file_unreadable:{relative}:{type(exc).__name__}:{exc}"
    return digest.hexdigest(), ""


def collect_release_identity(
    root: str | Path = PROJECT_ROOT,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Collect the current checkout identity for one preflight/startup."""

    resolved_root = Path(root).expanduser().resolve()
    root_rc, reported_root, root_err = _run_git(
        ("git", "rev-parse", "--show-toplevel"), root=resolved_root, runner=runner
    )
    head_rc, head_output, head_err = _run_git(
        ("git", "rev-parse", "HEAD"), root=resolved_root, runner=runner
    )
    status_rc, status_output, status_err = _run_git(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        root=resolved_root,
        runner=runner,
    )
    files_rc, files_output, files_err = _run_git(
        ("git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"),
        root=resolved_root,
        runner=runner,
    )
    reported_root = reported_root.strip()
    head = head_output.strip()
    commands_ok = all(rc == 0 for rc in (root_rc, head_rc, status_rc, files_rc))
    root_matches = bool(reported_root) and Path(reported_root).resolve() == resolved_root
    paths = _split_nul(files_output)
    fingerprint, fingerprint_error = _hash_worktree(resolved_root, head, paths)
    error = next(
        (
            item
            for item in (root_err, head_err, status_err, files_err, fingerprint_error)
            if item
        ),
        "",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": bool(commands_ok and root_matches and head and fingerprint and not error),
        "root": str(resolved_root),
        "reported_root": reported_root,
        "head": head,
        "clean": not bool(status_output.strip()),
        "status_porcelain": status_output,
        "worktree_fingerprint": fingerprint,
        "tracked_and_unignored_file_count": len(paths),
        "error": error,
        "source": "git_worktree_snapshot",
    }


_PROCESS_RELEASE_IDENTITY = collect_release_identity()
_PROCESS_RELEASE_IDENTITY.update(
    {
        "pid": int(os.getpid()),
        "captured_at": float(time.time()),
    }
)

_PUBLIC_IDENTITY_FIELDS = (
    "schema_version",
    "ok",
    "root",
    "head",
    "clean",
    "worktree_fingerprint",
    "tracked_and_unignored_file_count",
    "error",
    "source",
    "pid",
    "captured_at",
)


def process_release_identity() -> dict[str, Any]:
    """Return only the summary identity frozen at backend process import time."""

    return {
        key: _PROCESS_RELEASE_IDENTITY.get(key)
        for key in _PUBLIC_IDENTITY_FIELDS
    }


def release_identity_contract(identity: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the stable code identity used to compare cooperating processes."""

    source = dict(process_release_identity() if identity is None else identity)
    return {key: source.get(key) for key in RELEASE_IDENTITY_MATCH_FIELDS}
