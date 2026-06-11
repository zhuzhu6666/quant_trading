"""alpha/evaluation/attribution.py — Factor-level performance attribution.

Decomposes portfolio or strategy performance into marginal contributions
from each factor using **sequential orthogonalisation** (also known as
the "Gram-Schmidt" or "double-entry" attribution).

The idea: factor returns are often correlated, so a simple multi-factor
regression coefficient does not reflect each factor's unique contribution.
Sequential orthogonalisation orthogonalises each factor against previously
included factors, so the marginal R² contribution of a factor represents
the increment in explanatory power it adds beyond all earlier factors.

Usage::

    import numpy as np
    from alpha.evaluation.attribution import Attribution

    # factor_values: (T, n_factors) array
    # returns: (T,) array
    factor_values = np.random.randn(1000, 5)
    returns = np.random.randn(1000)

    attr = Attribution()
    report = attr.attribute(factor_values, returns, factor_names=["A", "B", "C", "D", "E"])
    print(report)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactorContribution:
    """Marginal contribution of a single factor.

    Attributes:
        name: Factor name (or index label).
        marginal_r2: Incremental R² from adding this factor in sequential
            orthogonalisation.  Represents the fraction of variance in
            ``returns`` uniquely explained by this factor.
        marginal_coefficient: Regression coefficient (beta) for the
            orthogonalised factor w.r.t. returns.
        cumulative_r2: Cumulative R² after including this factor in the
            sequential order.
        standalone_r2: R² if this factor is used alone (simple univariate
            regression).  Provided for comparison.
        standalone_coefficient: Coefficient from univariate regression.
    """

    name: str
    marginal_r2: float
    marginal_coefficient: float
    cumulative_r2: float
    standalone_r2: float
    standalone_coefficient: float


@dataclass(frozen=True)
class AttributionReport:
    """Complete factor attribution report.

    Attributes:
        contributions: List of FactorContribution, one per factor, in the
            order they were orthogonalised.
        total_r2: Total R² of the full model including all factors.
        n_obs: Number of observations used.
        n_factors: Number of factors included.
    """

    contributions: List[FactorContribution] = field(default_factory=list)
    total_r2: float = 0.0
    n_obs: int = 0
    n_factors: int = 0


class Attribution:
    """Factor-level performance attribution via sequential orthogonalisation.

    Parameters
    ----------
    demean : bool, default True
        If True, demean the target returns before fitting.
    """

    def __init__(self, demean: bool = True) -> None:
        self.demean = demean

    # ── Public API ──────────────────────────────────────────────────────

    def attribute(
        self,
        factor_values: np.ndarray,
        returns: np.ndarray,
        factor_names: Optional[List[str]] = None,
        order: Optional[List[int]] = None,
    ) -> AttributionReport:
        """Compute marginal factor contributions.

        Parameters
        ----------
        factor_values : np.ndarray of shape (T, n_factors)
            Factor/feature values (T observations, K factors).
        returns : np.ndarray of shape (T,)
            Target forward returns.
        factor_names : list of str, optional
            Names for each factor column.  If omitted, uses ``["F0", ...]``.
        order : list of int, optional
            Permutation of ``[0, ..., n_factors-1]`` specifying the
            sequential orthogonalisation order.  If omitted, factors are
            used in their column order.

        Returns
        -------
        AttributionReport
        """
        X, y = self._validate(factor_values, returns)
        T, K = X.shape

        if factor_names is None:
            factor_names = [f"F{i}" for i in range(K)]
        else:
            if len(factor_names) != K:
                raise ValueError(
                    f"Got {len(factor_names)} names for {K} factors"
                )
            factor_names = list(factor_names)

        if order is None:
            order = list(range(K))
        else:
            if sorted(order) != list(range(K)):
                raise ValueError(
                    f"order must be a permutation of [0, {K-1}], got {order}"
                )

        if self.demean:
            y = y - np.mean(y)

        # Main loop: sequential orthogonalisation (Gram-Schmidt style)
        contributions: List[FactorContribution] = []
        prev_residuals = y.copy()  # residuals after previous factor
        ortho_factors: List[np.ndarray] = []  # list of (T,) orthogonalised factors
        cum_r2 = 0.0

        for step, idx in enumerate(order):
            f = X[:, idx].copy().ravel()
            name = factor_names[idx]

            # ── 1. Standalone regression (univariate) ───────────────
            stand_beta, stand_r2 = self._univariate_regression(f, y)
            if np.isnan(stand_r2):
                stand_r2 = 0.0
            if np.isnan(stand_beta):
                stand_beta = 0.0

            # ── 2. Orthogonalise this factor against previous factors ─
            f_ortho = f.copy()
            for prev_f in ortho_factors:
                # Remove projection onto previous orthogonal factor
                denom = np.dot(prev_f, prev_f)
                if denom > 1e-12:
                    coeff = np.dot(f_ortho, prev_f) / denom
                    f_ortho = f_ortho - coeff * prev_f

            ortho_factors.append(f_ortho)

            # ── 3. Marginal regression of residuals on orthogonalised factor
            beta, marginal_r2 = self._univariate_regression(f_ortho, prev_residuals)
            if np.isnan(marginal_r2):
                marginal_r2 = 0.0
            if np.isnan(beta):
                beta = 0.0

            # ── 4. Update cumulative R² ─────────────────────────────
            cum_r2 = min(1.0, cum_r2 + marginal_r2)

            contributions.append(
                FactorContribution(
                    name=name,
                    marginal_r2=round(marginal_r2, 6),
                    marginal_coefficient=round(beta, 6),
                    cumulative_r2=round(cum_r2, 6),
                    standalone_r2=round(stand_r2, 6),
                    standalone_coefficient=round(stand_beta, 6),
                )
            )

            # ── 5. Update residuals for next iteration ──────────────
            predicted = beta * f_ortho
            prev_residuals = prev_residuals - predicted

        # Total R² of the full model
        total_r2 = cum_r2

        return AttributionReport(
            contributions=contributions,
            total_r2=round(total_r2, 6),
            n_obs=T,
            n_factors=K,
        )

    # ── Internal helpers ────────────────────────────────────────────────

    @staticmethod
    def _validate(
        factor_values: np.ndarray,
        returns: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        X = np.asarray(factor_values, dtype=np.float64)
        y = np.asarray(returns, dtype=np.float64).ravel()

        if X.ndim != 2:
            raise ValueError(
                f"factor_values must be 2-D (T, n_factors), got shape {X.shape}"
            )
        if y.ndim != 1:
            raise ValueError(
                f"returns must be 1-D, got shape {returns.shape}"
            )
        if X.shape[0] != y.shape[0]:
            raise ValueError(
                f"factor_values rows ({X.shape[0]}) != returns length ({y.shape[0]})"
            )

        # Drop rows with any NaN
        nan_mask = np.isnan(y)
        if X.shape[1] > 0:
            nan_mask = nan_mask | np.any(np.isnan(X), axis=1)
        X = X[~nan_mask]
        y = y[~nan_mask]

        return X, y

    @staticmethod
    def _univariate_regression(
        x: np.ndarray,
        y: np.ndarray,
    ) -> Tuple[float, float]:
        """Simple OLS: y ~ beta * x.

        Returns ``(beta, r2)``.
        """
        if len(x) < 3:
            return 0.0, 0.0

        x_demeaned = x - np.mean(x)
        y_demeaned = y - np.mean(y)

        ss_xx = np.dot(x_demeaned, x_demeaned)
        ss_yy = np.dot(y_demeaned, y_demeaned)

        if ss_xx < 1e-12 or ss_yy < 1e-12:
            return 0.0, 0.0

        beta = np.dot(x_demeaned, y_demeaned) / ss_xx
        ss_reg = beta * beta * ss_xx
        r2 = min(1.0, max(0.0, ss_reg / ss_yy))

        return float(beta), float(r2)

    def __repr__(self) -> str:
        return f"Attribution(demean={self.demean})"
