from __future__ import annotations
import importlib
from types import MappingProxyType, SimpleNamespace
import pytest
from backend.jobs.handlers import PERSISTENT_JOB_HANDLERS

# Canonical mapping after glue removal (194 lines saved)
# discover/tuning/ab now direct to scripts/*, external_refresh/factor_health inlined in handlers
@pytest.mark.parametrize(
    ("kind", "module_name", "function_name"),
    (
        ("backtest", "backend.services.backtest_service", "run_backtest"),
        ("discover", "scripts.discover_factors", "run_discovery"),
        ("tuning", "scripts.tune_risk_params", "run_tuning"),
        ("ab_test", "scripts.p1_e_ab_test", "run_ab"),
        (
            "external_refresh",
            "backend.jobs.handlers",
            "run_external_refresh_job",
        ),
        ("sync", "backend.services.sync_service", "run_sync_once"),
        (
            "factor_health",
            "backend.jobs.handlers",
            "run_factor_health_job",
        ),
        (
            "parameter_template_validation",
            "backend.services.parameter_template_validation",
            "run_parameter_template_offline_validation",
        ),
    ),
)
def test_each_persistent_handler_routes_serializable_params_and_progress(
    monkeypatch,
    kind: str,
    module_name: str,
    function_name: str,
) -> None:
    # For inlined handlers (external_refresh, factor_health), patching the handler itself
    # would not affect PERSISTENT_JOB_HANDLERS dict reference (lazy import vs inline).
    # Instead verify the handler exists and is callable, and for delegated handlers verify routing.
    if kind in ("external_refresh", "factor_health"):
        # Smoke: handler is registered and callable; inline logic tested elsewhere
        assert kind in PERSISTENT_JOB_HANDLERS
        assert callable(PERSISTENT_JOB_HANDLERS[kind])
        # Also verify module attribute exists
        mod = importlib.import_module(module_name)
        assert hasattr(mod, function_name)
        return
    service_module = importlib.import_module(module_name)
    calls = []
    progress = lambda *_args: None
    def service(params, callback):
        calls.append((dict(params), callback))
        return {"handled": kind}
    monkeypatch.setattr(service_module, function_name, service)
    result = PERSISTENT_JOB_HANDLERS[kind](
        MappingProxyType({"kind": kind, "value": 7}),
        progress,
    )
    assert result == {"handled": kind}
    assert calls == [({"kind": kind, "value": 7}, progress)]
