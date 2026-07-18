from __future__ import annotations

import importlib
from types import MappingProxyType

import pytest

from backend.jobs.handlers import PERSISTENT_JOB_HANDLERS


@pytest.mark.parametrize(
    ("kind", "module_name", "function_name"),
    (
        ("backtest", "backend.services.backtest_service", "run_backtest"),
        ("discover", "backend.services.discover_service", "run_discovery"),
        ("tuning", "backend.services.tuning_service", "run_tuning"),
        ("ab_test", "backend.services.ab_service", "run_ab"),
        (
            "external_refresh",
            "backend.services.external_data_refresh",
            "run_external_data_refresh",
        ),
        ("sync", "backend.services.sync_service", "run_sync_once"),
        (
            "factor_health",
            "backend.services.factor_health_service",
            "run_factor_health",
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
