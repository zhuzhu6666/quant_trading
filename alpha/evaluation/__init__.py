"""alpha/evaluation — Out-of-sample evaluation toolkit (Phase 2.2).

Provides time-series cross-validation, purged walk-forward, bootstrap CI,
causal check, and factor-level performance attribution for quantitative
factor evaluation.

Also provides EvaluationResult — unified interface across backtest/live/shadow.

Lazy exports (PEP 562): importing this package must not pull scipy.
``bootstrap_ci``/``causal_check``/``attribution`` load scipy only when the
corresponding class is first accessed.
"""

from __future__ import annotations

from typing import Any

_LAZY_EXPORTS = {
    "EvaluationContext": "alpha.evaluation.evaluation_context",
    "CVSplit": "alpha.evaluation.evaluation_context",
    "PurgedWalkForward": "alpha.evaluation.purged_walkforward",
    "FoldContext": "alpha.evaluation.purged_walkforward",
    "BootstrapCI": "alpha.evaluation.bootstrap_ci",
    "CausalCheck": "alpha.evaluation.causal_check",
    "CausalReport": "alpha.evaluation.causal_check",
    "Attribution": "alpha.evaluation.attribution",
    "AttributionReport": "alpha.evaluation.attribution",
    "FactorContribution": "alpha.evaluation.attribution",
    "EvaluationResult": "alpha.evaluation.result",
}

__all__ = [
    "EvaluationContext",
    "CVSplit",
    "PurgedWalkForward",
    "FoldContext",
    "BootstrapCI",
    "CausalCheck",
    "CausalReport",
    "Attribution",
    "AttributionReport",
    "FactorContribution",
    "EvaluationResult",
]


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
