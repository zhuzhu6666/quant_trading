"""Verify paths resolve from any CWD."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.core.paths import (
    CHARTS_DIR, CONFIG_DIR, DATA_DIR, DB_PATH, LOGS_DIR, PROJECT_ROOT, ensure_logs_dir,
)


def test_project_root_is_quant_trading():
    assert PROJECT_ROOT.name == "quant_trading"


def test_data_dir_under_root():
    assert DATA_DIR == PROJECT_ROOT / "data"
    assert LOGS_DIR == PROJECT_ROOT / "logs"
    assert CONFIG_DIR == PROJECT_ROOT / "config"


def test_ensure_logs_dir_is_idempotent():
    p = ensure_logs_dir()
    assert p.exists()
    p2 = ensure_logs_dir()
    assert p2 == p


def test_paths_resolve_from_subdir(tmp_path, monkeypatch):
    """When CWD is a subdir, paths still point at the project root."""
    monkeypatch.chdir(tmp_path)
    # Re-import to re-evaluate the module-level Path() computation
    result = subprocess.run(
        [sys.executable, "-c", "from backend.core.paths import PROJECT_ROOT; print(PROJECT_ROOT)"],
        capture_output=True, text=True, env={**os.environ},
    )
    assert "quant_trading" in result.stdout
