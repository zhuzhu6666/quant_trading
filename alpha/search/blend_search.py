from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BlendSolution:
    """A blend of factors with learned coefficients.

    Attributes:
        factor_names: Names of factors in the blend
        coefficients: Linear coefficients (same order as factor_names)
        intercept: Blend intercept term
        score: Evaluation score (e.g., IC, Sharpe)
        method: Optimization method ("equal_weight", "ic_weighted", "scipy_optimize")
    """

    factor_names: list[str]
    coefficients: list[float]
    intercept: float = 0.0
    score: float = 0.0
    method: str = "equal_weight"


class BlendSearch:
    """GP domain for blend coefficient search.

    Full scipy.optimize implementation is deferred. Currently provides
    coefficient storage, equal-weight baseline, and IC-weighted blending.
    """

    def __init__(self):
        self._solutions: list[BlendSolution] = []

    def add_solution(self, solution: BlendSolution) -> None:
        self._solutions.append(solution)

    def best(self, k: int = 1) -> list[BlendSolution]:
        sorted_s = sorted(
            self._solutions, key=lambda s: s.score, reverse=True
        )
        return sorted_s[:k]

    def equal_weight_blend(self, factor_names: list[str]) -> BlendSolution:
        """Create an equal-weight blend of given factors."""
        n = len(factor_names)
        if n == 0:
            return BlendSolution(factor_names=[], coefficients=[])
        return BlendSolution(
            factor_names=factor_names,
            coefficients=[1.0 / n] * n,
            method="equal_weight",
        )

    def ic_weighted_blend(
        self, factor_names: list[str], ics: list[float]
    ) -> BlendSolution:
        """Create an IC-weighted blend. Weights are abs(IC) normalized to sum to 1."""
        if not factor_names or not ics:
            return BlendSolution(factor_names=[], coefficients=[])
        abs_ics = [abs(ic) for ic in ics]
        total = sum(abs_ics)
        if total == 0:
            return self.equal_weight_blend(factor_names)
        return BlendSolution(
            factor_names=factor_names,
            coefficients=[aic / total for aic in abs_ics],
            method="ic_weighted",
        )
