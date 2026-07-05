"""alpha/search/blend_search.py — scipy.optimize for optimal factor blend (Task 2.1.5).

Uses SLSQP to maximise Sharpe ratio of a linear factor blend.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from alpha.ic_tracker import safe_corrcoef

logger = logging.getLogger(__name__)


@dataclass
class BlendSolution:
    """A blend of factors with learned coefficients.

    Attributes:
        factor_names: Names of factors in the blend.
        coefficients: Linear coefficients (same order as factor_names).
        intercept: Blend intercept term.
        score: Sharpe ratio (annualised) of the blend.
        method: Optimization method used.
    """
    factor_names: list[str]
    coefficients: list[float]
    intercept: float = 0.0
    score: float = 0.0
    method: str = "equal_weight"

    def to_dict(self) -> dict:
        return {
            "factor_names": self.factor_names,
            "coefficients": [round(c, 6) for c in self.coefficients],
            "intercept": self.intercept,
            "score": round(self.score, 6),
            "method": self.method,
        }


class BlendSearch:
    """GP domain for blend coefficient search.

    Provides equal-weight baseline, IC-weighted blending, and
    scipy.optimize-based optimal blend via SLSQP.
    """

    def __init__(self):
        self._solutions: list[BlendSolution] = []

    def add_solution(self, solution: BlendSolution) -> None:
        self._solutions.append(solution)

    def best(self, k: int = 1) -> list[BlendSolution]:
        return sorted(self._solutions, key=lambda s: s.score, reverse=True)[:k]

    # ── Simple blends ────────────────────────────────────────────────

    def equal_weight_blend(self, factor_names: list[str]) -> BlendSolution:
        """Equal-weight blend: 1/n for each factor."""
        n = len(factor_names)
        if n == 0:
            return BlendSolution(factor_names=[], coefficients=[])
        return BlendSolution(
            factor_names=factor_names,
            coefficients=[1.0 / n] * n,
            method="equal_weight",
        )

    def ic_weighted_blend(self, factor_names: list[str], ics: list[float]) -> BlendSolution:
        """IC-weighted blend: weight proportional to |IC|."""
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

    # ── Scipy optimize ───────────────────────────────────────────────

    def optimize(
        self,
        factor_returns: np.ndarray,
        forward_returns: np.ndarray | None = None,
        factor_names: list[str] | None = None,
        method: str = "SLSQP",
        max_iter: int = 1000,
        max_single_weight: float = 0.5,
    ) -> BlendSolution:
        """Optimise blend coefficients to maximise Sharpe ratio.

        Args:
            factor_returns: (T, n_factors) array of factor values per bar.
            forward_returns: (T,) array of forward returns (optional — if None,
                             uses factor_returns as the blended portfolio return).
            factor_names: Optional names for each column.
            method: scipy.optimize.minimize method ('SLSQP' or 'L-BFGS-B').
            max_iter: Maximum optimisation iterations.
            max_single_weight: Maximum weight for any single factor (0.5 = no single
                               factor dominates). Set to 1.0 to disable.

        Returns:
            BlendSolution with optimised coefficients and Sharpe score.
        """
        # Late import so scipy is optional
        try:
            from scipy.optimize import minimize as _minimize
        except ImportError:
            logger.warning("scipy not available, falling back to equal_weight")
            n = factor_returns.shape[1]
            names = factor_names or [f"f{i}" for i in range(n)]
            return self.equal_weight_blend(names)

        T, n = factor_returns.shape
        if n == 0:
            return BlendSolution(factor_names=[], coefficients=[])
        if n == 1:
            return BlendSolution(
                factor_names=factor_names or ["f0"],
                coefficients=[1.0],
                method=method,
            )

        names = factor_names or [f"f{i}" for i in range(n)]

        # If forward_returns not given, treat factor_returns as portfolio returns
        target = forward_returns if forward_returns is not None else np.ones(T)

        def _neg_sharpe(w: np.ndarray) -> float:
            """Negative annualised Sharpe of blended portfolio vs target."""
            blended = factor_returns @ w
            # Use correlation with target as a smooth objective
            mask = np.isfinite(blended) & np.isfinite(target)
            b = blended[mask]
            t = target[mask]
            if len(b) < 30:
                return 0.0
            # Sharpe-like: mean(b * t) / std(b) * sqrt(252)
            ret = np.mean(b * t)  # cross-moment as proxy
            std = np.std(b)
            if std < 1e-10:
                return 0.0
            return -(ret / std * math.sqrt(252))

        # Constraints: sum(w) = 1, w >= 0
        constraints = [{"type": "eq", "fun": lambda w: np.sum(w) - 1.0}]
        bounds = [(0.0, max_single_weight) for _ in range(n)]

        # Dual warmstart
        best_w: np.ndarray | None = None
        best_obj = float("inf")

        for guess in ["equal", "ic"]:
            if guess == "equal":
                w0 = np.ones(n) / n
            else:
                abs_ic = np.array([
                    abs(safe_corrcoef(factor_returns[:, i], target, min_samples=30))
                    for i in range(n)
                ], dtype=float)
                w0 = abs_ic / (abs_ic.sum() + 1e-10)
                # Clamp to bounds
                w0 = np.clip(w0, 0.0, max_single_weight)
                w0 = w0 / w0.sum()

            try:
                res = _minimize(
                    _neg_sharpe, w0, method=method,
                    bounds=bounds, constraints=constraints,
                    options={"maxiter": max_iter, "ftol": 1e-12},
                )
                if res.fun < best_obj:
                    best_obj = float(res.fun)
                    best_w = res.x.copy()
            except Exception as e:
                logger.debug("BlendSearch %s warmstart failed: %s", guess, e)

        # Fallback to equal weight if both failed
        if best_w is None:
            logger.warning("BlendSearch both warmstarts failed, using equal_weight")
            return BlendSolution(
                factor_names=names,
                coefficients=[1.0 / n] * n,
                method=f"{method}_fallback",
            )

        # Compute final Sharpe
        blended = factor_returns @ best_w
        mask = np.isfinite(blended) & np.isfinite(target)
        b = blended[mask]
        t = target[mask]
        if len(b) >= 30:
            sharpe = np.mean(b * t) / np.std(b) * math.sqrt(252)
        else:
            sharpe = 0.0

        solution = BlendSolution(
            factor_names=names,
            coefficients=[float(c) for c in best_w],
            score=sharpe,
            method=method,
        )
        self._solutions.append(solution)
        return solution
