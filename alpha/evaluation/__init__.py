"""alpha/evaluation — Out-of-sample evaluation toolkit (Phase 2.2).

Provides time-series cross-validation, purged walk-forward, bootstrap CI,
causal check, and factor-level performance attribution for quantitative
factor evaluation.
"""

from alpha.evaluation.evaluation_context import EvaluationContext, CVSplit
from alpha.evaluation.purged_walkforward import PurgedWalkForward, FoldContext
from alpha.evaluation.bootstrap_ci import BootstrapCI
from alpha.evaluation.causal_check import CausalCheck, CausalReport
from alpha.evaluation.attribution import Attribution, AttributionReport, FactorContribution

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
]
