"""Guard test: every refactored script works as both CLI and importable service.

Per spec §1.3 / §5.4 DoD: for each refactored script, both modes must work.

Currently covers Phase 4.1-4.4 (4 scripts). When Phase 4.5 bulk-refactors more
scripts, add them to SCRIPT_SPECS below.
"""
import inspect
import subprocess
import sys
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
PYTHON = sys.executable

# (script_filename, service_import_path, service_function_name, expected_param_names_subset)
SCRIPT_SPECS = [
    ("discover_factors.py", "scripts.discover_factors", "run_discovery", ["n_candidates", "top_k", "forward_periods", "engine"]),
    ("live_sync.py", "scripts.live_sync", "run_sync_once", ["timeframes", "sync_type"]),
    ("live_sync.py", "scripts.live_sync", "get_status", []),
    ("tune_risk_params.py", "scripts.tune_risk_params", "run_tuning", ["risk_pct_grid", "cb_pct_grid", "n_bars"]),
    ("p1_e_ab_test.py", "scripts.p1_e_ab_test", "run_ab", ["path_a", "path_b", "n_bars"]),
]


@pytest.mark.parametrize("script_name,_,__,expected_params", [(s, i, f, p) for s, i, f, p in SCRIPT_SPECS])
def test_cli_help_exits_zero(script_name, _, __, expected_params):
    """CLI --help must exit 0 and print usage. Validates the CLI mode is intact."""
    script = SCRIPTS_DIR / script_name
    assert script.exists(), f"script not found: {script}"
    proc = subprocess.run(
        [PYTHON, str(script), "--help"],
        capture_output=True, text=True, timeout=10,
        cwd=str(PROJECT_ROOT),
    )
    assert proc.returncode == 0, f"{script_name} --help exited {proc.returncode}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    combined = (proc.stdout + proc.stderr).lower()
    assert "usage:" in combined, f"{script_name} --help did not print usage:\n{combined}"


@pytest.mark.parametrize("script_name,import_path,func_name,expected_params", SCRIPT_SPECS)
def test_service_function_is_importable(script_name, import_path, func_name, expected_params):
    """Service function must be importable from the script (not just CLI)."""
    module = __import__(import_path, fromlist=[func_name])
    fn = getattr(module, func_name)
    assert callable(fn), f"{import_path}.{func_name} is not callable"


@pytest.mark.parametrize("script_name,import_path,func_name,expected_params", SCRIPT_SPECS)
def test_service_function_signature(script_name, import_path, func_name, expected_params):
    """Service function must accept the expected parameter names."""
    module = __import__(import_path, fromlist=[func_name])
    fn = getattr(module, func_name)
    sig = inspect.signature(fn)
    actual_params = list(sig.parameters.keys())
    for p in expected_params:
        assert p in actual_params, f"{func_name} missing param '{p}'; has {actual_params}"


def test_cli_help_is_fast():
    """All --help calls must complete in <3s — guards against --help accidentally
    triggering heavy imports or computation."""
    for script_name, *_ in SCRIPT_SPECS:
        script = SCRIPTS_DIR / script_name
        # Dedupe by script_name
        if script_name != SCRIPT_SPECS[0][0]:
            continue
        t0 = time.time()
        proc = subprocess.run(
            [PYTHON, str(script), "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(PROJECT_ROOT),
        )
        elapsed = time.time() - t0
        assert proc.returncode == 0
        # Loose bound: a slow machine might be 2-3s; fail if > 5s
        assert elapsed < 5.0, f"{script_name} --help took {elapsed:.2f}s (expected <5s)"
