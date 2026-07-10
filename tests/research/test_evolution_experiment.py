"""Tests for research/evolution_experiment.py — 进化实验注册器."""

import time
import json
import sqlite3
import pytest
from research.evolution_experiment import EvolutionExperimentRegistry
from research.experiment_tracker import ExperimentTracker


class TestExperimentRegistry:
    def _make_reg(self):
        import tempfile
        return EvolutionExperimentRegistry(db_path=tempfile.mktemp(suffix=".db"))

    def test_start_run_returns_run_id(self):
        reg = self._make_reg()
        run_id = reg.start_run("gp_search", params={"pop": 50})
        assert run_id.startswith("gp_search_")
        assert reg.get_run(run_id) is not None

    def test_finish_run_updates_status(self):
        reg = self._make_reg()
        run_id = reg.start_run("model_shadow_train")
        ok = reg.finish_run(run_id, accepted=True, metrics_oos={"sharpe": 1.5})
        assert ok
        record = reg.get_run(run_id)
        assert record["status"] == "accepted"
        assert record["metrics_oos"]["sharpe"] == 1.5

    def test_fail_run(self):
        reg = self._make_reg()
        run_id = reg.start_run("param_tune")
        reg.fail_run(run_id, error="timeout")
        record = reg.get_run(run_id)
        assert record["status"] == "failed"

    def test_list_runs_filters_by_type(self):
        reg = self._make_reg()
        reg.start_run("gp_search")
        reg.start_run("model_shadow_train")
        reg.start_run("gp_search")
        gp_runs = reg.list_runs(experiment_type="gp_search")
        assert len(gp_runs) >= 2
        assert all(r["experiment_type"] == "gp_search" for r in gp_runs)

    def test_finish_unknown_run(self):
        reg = self._make_reg()
        ok = reg.finish_run("nonexistent", accepted=True)
        assert not ok


def test_evolution_registry_and_experiment_tracker_share_one_schema(tmp_path):
    db_path = tmp_path / "experiments.db"
    tracker = ExperimentTracker(str(db_path))
    tracker_id = tracker.start_run("backtest", params={"window": 20})

    registry = EvolutionExperimentRegistry(db_path=str(db_path))
    assert registry.get_run(tracker_id)["params_json"] == {"window": 20}
    evolution_id = registry.start_run("gp_search", params={"pop": 10})
    assert registry.finish_run(evolution_id, accepted=True, metrics_oos={"sharpe": 1.4})

    stored = tracker.get_run(evolution_id)
    assert stored is not None
    assert stored.status == "accepted"
    assert stored.metrics["oos"]["sharpe"] == 1.4


def test_experiment_tracker_migrates_legacy_json_blob_rows(tmp_path):
    db_path = tmp_path / "legacy_experiments.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE experiments (run_id TEXT PRIMARY KEY, data TEXT, updated_at REAL)"
        )
        conn.execute(
            "INSERT INTO experiments (run_id, data, updated_at) VALUES (?, ?, ?)",
            (
                "legacy_run",
                json.dumps({
                    "run_id": "legacy_run",
                    "timestamp": 123.0,
                    "experiment_type": "legacy_backtest",
                    "params": {"n": 5},
                    "metrics": {"sharpe": 0.8},
                    "tags": ["legacy"],
                    "artifacts": [],
                    "status": "completed",
                }),
                123.0,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    migrated = ExperimentTracker(str(db_path)).get_run("legacy_run")
    assert migrated is not None
    assert migrated.experiment_type == "legacy_backtest"
    assert migrated.params == {"n": 5}
    assert migrated.metrics == {"sharpe": 0.8}
