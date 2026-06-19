"""alpha/evaluation/causal_check.py — Causal relationship failure detection.

Detects whether observed correlation between factor values and forward returns
reflects a genuine predictive relationship or is driven by confounding
(``cause_vs_corr``), and measures decay of the relationship over time.

The approach uses two simple diagnostics:

1. **Orthogonality test**: orthogonalise factor values against a set of
   control variables (or lagged versions of themselves) and measure whether
   the orthogonalised component retains predictive power.  A low p-value
   suggests the factor's signal is not fully explained by known confounds.

2. **Decaying correlation test**: partition the sample into early and late
   halves and compare the correlation.  A large decay rate signals the
   relationship is fading, which is a practical failure of causality
   (the factor stopped working).

The ``cause_vs_corr_score`` is a heuristic combining both tests.

Usage::

    check = CausalCheck()
    report = check.check(factor_values, forward_returns)
    print(report)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy import stats as scipy_stats

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CausalReport:
    """Report from a causal relationship check.

    Attributes:
        cause_vs_corr_score: Heuristic score in [-1, 1] indicating whether
            the observed correlation is likely causal (+1) or spurious (-1).
            Scores near 0 are inconclusive.
        orthogonality_pvalue: P-value from the orthogonality test.
            A low p-value (< 0.05) suggests the factor's predictive power is
            not fully explained by orthogonalised controls.
        decay_rate: Rate of decay of the factor's correlation over the sample
            period.  Positive means the relationship is weakening (decaying),
            negative means it is strengthening, near 0 means stable.
        raw_correlation: Pearson correlation between factor values and
            forward returns over the full sample.
        early_correlation: Pearson correlation in the first half of the sample.
        late_correlation: Pearson correlation in the second half of the sample.
        n_obs: Number of valid (non-NaN) observations used.
    """

    cause_vs_corr_score: float
    orthogonality_pvalue: float
    decay_rate: float
    raw_correlation: float
    early_correlation: float
    late_correlation: float
    n_obs: int


class CausalCheck:
    """Detect causal relationship failures between factor values and returns.

    Parameters
    ----------
    n_lags : int, default 1
        Number of lags to use for the orthogonalisation control.
        The factor values are regressed against their own lagged values;
        the residuals represent the "unexpected" component.
    """

    def __init__(self, n_lags: int = 1) -> None:
        if n_lags < 1:
            raise ValueError(f"n_lags must be >= 1, got {n_lags}")
        self.n_lags = n_lags

    # ── Public API ──────────────────────────────────────────────────────

    def check(
        self,
        factor_values: np.ndarray,
        forward_returns: np.ndarray,
    ) -> CausalReport:
        """Run causal diagnostics on a factor-return pair.

        Parameters
        ----------
        factor_values : np.ndarray of shape (n,)
            Factor or signal values.
        forward_returns : np.ndarray of shape (n,)
            Corresponding forward returns.

        Returns
        -------
        CausalReport
        """
        vals, rets = self._validate_paired(factor_values, forward_returns)
        n = len(vals)

        if n < 10:
            logger.warning(
                "CausalCheck.check(): only %d valid observations, returning neutral report",
                n,
            )
            return CausalReport(
                cause_vs_corr_score=0.0,
                orthogonality_pvalue=1.0,
                decay_rate=0.0,
                raw_correlation=0.0,
                early_correlation=0.0,
                late_correlation=0.0,
                n_obs=n,
            )

        # ── 1. Raw full-sample correlation ──────────────────────────
        raw_corr = 0.0
        if n >= 3:
            try:
                rc, _ = scipy_stats.pearsonr(vals, rets)
                raw_corr = float(rc) if not np.isnan(rc) else 0.0
            except (ValueError, ZeroDivisionError):
                raw_corr = 0.0

        # ── 2. Orthogonality test ──────────────────────────────────
        orth_pvalue = self._orthogonality_test(vals, rets)

        # ── 3. Decaying correlation test ────────────────────────────
        early_corr, late_corr, decay_rate = self._decay_test(vals, rets)

        # ── 4. Heuristic cause_vs_corr_score ────────────────────────
        # Combine the p-value and decay rate into a score in [-1, 1]:
        #   - Strong causal case: orthogonality p-value is very low
        #     (factor has unique info) AND decay rate is near 0 or
        #     negative (stable or improving).
        #   - Spurious case: high p-value (little unique info beyond
        #     controls) AND / OR decay rate is large positive.
        cause_score = self._compute_cause_score(orth_pvalue, decay_rate, raw_corr)

        return CausalReport(
            cause_vs_corr_score=cause_score,
            orthogonality_pvalue=orth_pvalue,
            decay_rate=decay_rate,
            raw_correlation=raw_corr,
            early_correlation=early_corr,
            late_correlation=late_corr,
            n_obs=n,
        )

    # ── Internal: orthogonality test ─────────────────────────────────

    def _orthogonality_test(
        self,
        vals: np.ndarray,
        rets: np.ndarray,
    ) -> float:
        """Test whether factor values have predictive power orthogonal to
        their own lagged values.

        Procedure:
        1. Build a design matrix of lagged factor values (``n_lags``).
        2. Regress ``vals`` on lags via OLS.
        3. Take the residuals (the "orthogonalised factor").
        4. Compute the p-value of a correlation test between residuals and
           forward returns.

        A low p-value indicates the residual component (unexplained by
        lags) retains predictive power — this is consistent with genuine
        causality.

        Returns
        -------
        float
            P-value from the correlation test of orthogonalised residuals
            vs forward returns.  Returns 1.0 if the test cannot be run.
        """
        n = len(vals)
        if n < self.n_lags + 5:
            return 1.0

        # Build lag matrix: shape (n - n_lags, n_lags)
        lag_matrix = np.zeros((n - self.n_lags, self.n_lags), dtype=np.float64)
        for lag in range(1, self.n_lags + 1):
            lag_matrix[:, lag - 1] = vals[lag: n - self.n_lags + lag]

        # Align target (future vals) with lag matrix
        target = vals[self.n_lags:]  # (n - n_lags,)

        if lag_matrix.shape[0] < 3:
            return 1.0

        # OLS via least squares
        try:
            coeffs, residuals_sum, rank, sv = np.linalg.lstsq(
                lag_matrix, target, rcond=None
            )
            predicted = lag_matrix @ coeffs
            residuals = target - predicted
        except np.linalg.LinAlgError:
            logger.warning("orthogonality_test: lstsq failed, returning pvalue=1.0")
            return 1.0

        # Align forward returns with residual length
        align_rets = rets[self.n_lags:]

        if len(residuals) < 5:
            return 1.0

        # Guard against constant residuals (variance near zero).
        if np.std(residuals) < 1e-12 or np.std(align_rets) < 1e-12:
            return 1.0

        # Correlation test: H0: correlation = 0 (no predictive power)
        try:
            _, pvalue = scipy_stats.pearsonr(residuals, align_rets)
        except ValueError:
            return 1.0

        if np.isnan(pvalue):
            return 1.0

        return float(pvalue)

    # ── Internal: decay test ─────────────────────────────────────────

    def _decay_test(
        self,
        vals: np.ndarray,
        rets: np.ndarray,
    ) -> tuple:
        """Compute early/late correlation and the decay rate.

        Splits the sample into two equal halves.  The decay rate is defined
        as the difference between late and early correlation:
        ``decay_rate = (late_corr - early_corr)``.
        A positive value means the relationship weakened (decayed).

        Returns
        -------
        tuple[float, float, float]
            ``(early_corr, late_corr, decay_rate)``
        """
        n = len(vals)
        mid = n // 2

        early_v, early_r = vals[:mid], rets[:mid]
        late_v, late_r = vals[mid:], rets[mid:]

        def _safe_corr(a, b):
            if len(a) < 3:
                return 0.0
            try:
                c, _ = scipy_stats.pearsonr(a, b)
                return float(c) if not np.isnan(c) else 0.0
            except (ValueError, ZeroDivisionError):
                return 0.0

        early_corr = _safe_corr(early_v, early_r)
        late_corr = _safe_corr(late_v, late_r)

        # Decay rate: positive = weakening, negative = strengthening.
        decay_rate = early_corr - late_corr
        # Normalise to a rate per unit time: small sample correction.
        # For simplicity, we keep the raw difference and clip to [-1, 1].
        decay_rate = max(-1.0, min(1.0, decay_rate))

        return early_corr, late_corr, decay_rate

    # ── Internal: combined heuristic ─────────────────────────────────

    def _compute_cause_score(
        self,
        orth_pvalue: float,
        decay_rate: float,
        raw_corr: float,
    ) -> float:
        """Heuristic cause_vs_corr_score in [-1, 1].

        Combines three signals:
        - Low orthogonality p-value (unique predictive power) → positive
        - Low decay rate (stable relationship) → positive
        - High raw correlation magnitude → amplifies confidence

        The formula is a bounded heuristic:

        ``score = sign(raw_corr) * (
            (1 - min(orth_pvalue, 1)) * 0.5
            + max(0, 1 - abs(decay_rate)) * 0.3
            + min(abs(raw_corr) / 0.1, 1) * 0.2
        ) * 2 - 1``

        Actually, we'll use a simpler exponential-based formula that
        maps to [-1, 1] smoothly:

        ``score = tanh(
            (1 - orth_pvalue) * 3
            - decay_rate * 2
            + raw_corr * 2
        )``

        This is bounded in [-1, 1] and responds to all three inputs.
        """
        # Safety clamp
        p = max(0.0, min(1.0, orth_pvalue))
        d = max(-1.0, min(1.0, decay_rate))
        r = max(-1.0, min(1.0, raw_corr))

        # Low p-value → high evidence for causality.
        # High decay_rate (positive) → evidence against causality.
        # High abs(raw_corr) amplifies both directions.
        evidence = (1.0 - p) * 3.0 - d * 2.0 + r * 2.0
        score = float(np.tanh(evidence))
        return score

    # ── Internal: validation ─────────────────────────────────────────

    @staticmethod
    def _validate_paired(
        a: np.ndarray, b: np.ndarray,
    ) -> tuple:
        a = np.asarray(a, dtype=np.float64).ravel()
        b = np.asarray(b, dtype=np.float64).ravel()
        if a.shape != b.shape:
            raise ValueError(
                f"factor_values shape {a.shape} != forward_returns shape {b.shape}"
            )
        mask = ~(np.isnan(a) | np.isnan(b) | np.isinf(a) | np.isinf(b))
        return a[mask], b[mask]

    def __repr__(self) -> str:
        return f"CausalCheck(n_lags={self.n_lags})"
