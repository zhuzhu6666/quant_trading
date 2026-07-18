from __future__ import annotations

import sys

import pytest

from scripts import compile_python_locks as lock_compiler


def _lock_fixture(tmp_path):
    (tmp_path / "requirements.in").write_text("example==1\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("example==1 --hash=sha256:abc\n", encoding="utf-8")
    return (("requirements.in", "requirements.txt"),)


def test_lock_check_seeds_compiler_with_committed_lock(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lock_compiler, "ROOT", tmp_path)
    monkeypatch.setattr(lock_compiler, "LOCKS", _lock_fixture(tmp_path))
    monkeypatch.setattr(lock_compiler, "_require_toolchain", lambda: None)
    monkeypatch.setattr(sys, "argv", ["compile_python_locks.py", "--check"])

    def verify_seed(_source: str, output) -> None:
        assert output.read_text(encoding="utf-8") == "example==1 --hash=sha256:abc\n"

    monkeypatch.setattr(lock_compiler, "_compile", verify_seed)

    assert lock_compiler.main() == 0


def test_lock_check_still_rejects_changed_compiler_output(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lock_compiler, "ROOT", tmp_path)
    monkeypatch.setattr(lock_compiler, "LOCKS", _lock_fixture(tmp_path))
    monkeypatch.setattr(lock_compiler, "_require_toolchain", lambda: None)
    monkeypatch.setattr(sys, "argv", ["compile_python_locks.py", "--check"])

    def change_output(_source: str, output) -> None:
        output.write_text("example==2 --hash=sha256:def\n", encoding="utf-8")

    monkeypatch.setattr(lock_compiler, "_compile", change_output)

    with pytest.raises(SystemExit, match="stale Python lock file"):
        lock_compiler.main()
