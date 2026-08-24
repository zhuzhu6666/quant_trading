from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "cleanup_market_closed_retry_storm_20260823.py"


def test_canonical_cleanup_script_has_no_physical_delete_sql():
    source = SCRIPT.read_text(encoding="utf-8").lower()
    forbidden_sql = "delete" + " from canonical_v2."

    assert forbidden_sql not in source
    assert "canonical_v2 is append-only" in source


def test_canonical_cleanup_script_rejects_apply_before_database_access(monkeypatch, capsys):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("market_closed_cleanup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "get_state_pg_conn", lambda: (_ for _ in ()).throw(AssertionError("DB access")))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--apply"])

    assert module.main() == 2
    assert "refusing --apply" in capsys.readouterr().err
