"""Tests for research/model_registry.py — ML 模型版本注册器."""

import tempfile
import pytest
from research.model_registry import ModelRegistry


@pytest.fixture
def reg():
    return ModelRegistry(db_path=tempfile.mktemp(suffix=".db"))


class TestModelRegistry:
    def test_register_returns_version(self, reg):
        v = reg.register("example_model", artifact_path="/tmp/model.pkl",
                         params={"n_estimators": 200},
                         metrics={"oos_acc": 0.68})
        assert v.model_type == "example_model"
        assert v.version == 1
        assert v.artifact_path == "/tmp/model.pkl"
        assert v.metrics["oos_acc"] == 0.68
        assert v.params["n_estimators"] == 200

    def test_version_auto_increment(self, reg):
        v1 = reg.register("example_model")
        v2 = reg.register("example_model")
        assert v1.version == 1
        assert v2.version == 2

    def test_list_versions_desc(self, reg):
        reg.register("example_model", metrics={"oos_acc": 0.6})
        reg.register("example_model", metrics={"oos_acc": 0.7})
        versions = reg.list_versions("example_model")
        assert len(versions) == 2
        assert versions[0].version == 2  # desc
        assert versions[1].version == 1

    def test_best_version_by_metric(self, reg):
        reg.register("example_model", metrics={"oos_acc": 0.6})
        reg.register("example_model", metrics={"oos_acc": 0.8})
        best = reg.best_version("example_model", metric="oos_acc")
        assert best is not None
        assert best.version == 2
        assert best.metrics["oos_acc"] == 0.8

    def test_get_version(self, reg):
        v1 = reg.register("example_model")
        v = reg.get_version("example_model", version=v1.version)
        assert v is not None
        assert v.version == v1.version

    def test_load_version_auto_best(self, reg):
        reg.register("example_model", artifact_path="/v1.pkl", metrics={"oos_acc": 0.6})
        reg.register("example_model", artifact_path="/v2.pkl", metrics={"oos_acc": 0.9})
        path = reg.load_version("example_model")
        assert path == "/v2.pkl"

    def test_load_version_specific(self, reg):
        reg.register("example_model", artifact_path="/v1.pkl")
        reg.register("example_model", artifact_path="/v2.pkl")
        path = reg.load_version("example_model", version=1)
        assert path == "/v1.pkl"

    def test_summary(self, reg):
        reg.register("example_model", symbol="XAUUSD+")
        reg.register("pca", symbol="XAUUSD+")
        s = reg.summary()
        assert "example_model/XAUUSD+/M5" in s
        assert "pca/XAUUSD+/M5" in s
        assert s["example_model/XAUUSD+/M5"]["n_versions"] == 1

    def test_per_symbol_independence(self, reg):
        reg.register("example_model", symbol="XAUUSD+")
        reg.register("example_model", symbol="BTCUSD")
        assert len(reg.list_versions("example_model", symbol="XAUUSD+")) == 1
        assert len(reg.list_versions("example_model", symbol="BTCUSD")) == 1
