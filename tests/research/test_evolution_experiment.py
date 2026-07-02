"""Tests for research/evolution_experiment.py — 进化实验注册器."""

import time
import pytest
from research.evolution_experiment import EvolutionExperimentRegistry


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
