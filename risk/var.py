"""
Risk: Value-at-Risk and Conditional VaR calculations.

Provides VaREngine class with parametric, historical, and Monte Carlo methods
for computing Value-at-Risk (VaR) and Conditional VaR (CVaR / Expected Shortfall).
"""

import numpy as np
from scipy.stats import norm
from collections import deque
from loguru import logger


class VaREngine:
    """
    VaR/CVaR calculation with 3 methods:

    1. **parametric** (variance-covariance): assumes normal distribution.
       VaR = portfolio_value * z_alpha * sigma * sqrt(hold_days)

    2. **historical**: uses recent N days of actual returns.
       VaR = -percentile(historical_returns, 1 - alpha)

    3. **monte_carlo**: simulates 10 000 paths with geometric Brownian motion.
       VaR = -percentile(simulated_returns, 1 - alpha)

    CVaR (Expected Shortfall) = mean of returns beyond the VaR threshold,
    reported as a positive loss value.

    All percentages are expressed as decimals (e.g. 0.05 = 5% loss).
    """

    def __init__(self, window: int = 1000) -> None:
        """
        Parameters
        ----------
        window : int
            Maximum number of returns to keep in the rolling window.
        """
        self.window = window
        self.returns: deque[float] = deque(maxlen=window)
        logger.info(f"VaREngine initialised — window={window}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_returns(self, new_return: float) -> None:
        """Append a single return to the rolling window (deque drop-out)."""
        self.returns.append(new_return)

    def compute_var(
        self,
        returns: list[float] | None = None,
        method: str = "historical",
        alpha: float = 0.95,
        portfolio_value: float = 1.0,
    ) -> dict:
        """
        Compute Value-at-Risk and Conditional VaR.

        Parameters
        ----------
        returns : list[float] or None
            Return series to use.  If ``None`` the internal rolling window
            (populated via :meth:`update_returns`) is used.
        method : str
            One of ``"parametric"``, ``"historical"``, ``"monte_carlo"``.
        alpha : float
            Confidence level (e.g. 0.95 for 95 % VaR).
        portfolio_value : float
            Notional portfolio / position value in USD.

        Returns
        -------
        dict
            Keys:
            - ``var_usd``   – VaR in dollars (positive = loss)
            - ``cvar_usd``  – CVaR in dollars
            - ``var_pct``   – VaR as decimal (0.05 = 5 %)
            - ``cvar_pct``  – CVaR as decimal
            - ``method``    – method used
            - ``alpha``     – confidence level

        Raises
        ------
        ValueError
            If *method* is not recognised.
        """
        # --- validate method ------------------------------------------------
        valid = {"parametric", "historical", "monte_carlo"}
        if method not in valid:
            raise ValueError(
                f"Unknown method: '{method}'. "
                f"Supported methods: {', '.join(sorted(valid))}"
            )

        # --- resolve returns ------------------------------------------------
        if returns is None:
            returns = list(self.returns)
        arr = np.asarray(returns, dtype=float)

        # --- edge cases -----------------------------------------------------
        if len(arr) < 10:
            logger.warning(
                f"Insufficient data ({len(arr)}  <  10) — returning zeros"
            )
            return self._zero_result(method, alpha)

        # Strip NaN / Inf
        finite = np.isfinite(arr)
        if not np.any(finite):
            logger.warning("All returns are NaN or Inf — returning zeros")
            return self._zero_result(method, alpha)
        arr = arr[finite]

        if len(arr) < 10:
            logger.warning(
                f"Only {len(arr)} finite returns  <  10 — returning zeros"
            )
            return self._zero_result(method, alpha)

        if np.allclose(arr, 0.0):
            logger.warning("All returns are zero — returning zeros")
            return self._zero_result(method, alpha)

        # --- dispatch -------------------------------------------------------
        if method == "parametric":
            var_pct, cvar_pct = self._parametric(arr, alpha)
        elif method == "historical":
            var_pct, cvar_pct = self._historical(arr, alpha)
        else:  # monte_carlo
            var_pct, cvar_pct = self._monte_carlo(arr, alpha)

        var_usd = var_pct * portfolio_value
        cvar_usd = cvar_pct * portfolio_value

        return {
            "var_usd": round(float(var_usd), 2),
            "cvar_usd": round(float(cvar_usd), 2),
            "var_pct": round(float(var_pct), 6),
            "cvar_pct": round(float(cvar_pct), 6),
            "method": method,
            "alpha": alpha,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _zero_result(method: str, alpha: float) -> dict:
        """Return a zero-filled result (edge / error case)."""
        return {
            "var_usd": 0.0,
            "cvar_usd": 0.0,
            "var_pct": 0.0,
            "cvar_pct": 0.0,
            "method": method,
            "alpha": alpha,
        }

    # ------------------------------------------------------------------
    # Computation methods
    # ------------------------------------------------------------------

    @staticmethod
    def _parametric(arr: np.ndarray, alpha: float) -> tuple[float, float]:
        """
        Variance-covariance (parametric normal) VaR & CVaR.

        VaR  = z_alpha * sigma * sqrt(hold_days)
        CVaR = sigma * phi(z_alpha) / (1 - alpha)   [analytical normal]
        """
        mu = float(np.nanmean(arr))
        sigma = float(np.nanstd(arr, ddof=1))
        hold_days = 1.0

        z_alpha = norm.ppf(alpha)  # positive for alpha > 0.5
        var_pct = z_alpha * sigma * np.sqrt(hold_days)  # positive loss

        # Analytical CVaR for a normal distribution N(mu, sigma):
        #   E[X | X <= q] = mu - sigma * phi(z_alpha) / (1 - alpha)
        #   where q is the (1-alpha) quantile.
        # We report CVaR as a positive loss, so we take the absolute value
        # (the conditional expectation is negative for typical params).
        # For zero-mean returns:
        #   CVaR_pct = sigma * phi(z_alpha) / (1 - alpha)
        phi_z = norm.pdf(z_alpha)
        cvar_cond_loss = mu - sigma * phi_z / (1.0 - alpha)
        cvar_pct = max(0.0, -cvar_cond_loss)

        return var_pct, cvar_pct

    @staticmethod
    def _historical(arr: np.ndarray, alpha: float) -> tuple[float, float]:
        """Historical simulation VaR & CVaR."""
        threshold = float(np.nanpercentile(arr, (1.0 - alpha) * 100.0))
        var_pct = abs(threshold)  # positive loss

        worst = arr[arr <= threshold]
        if len(worst) == 0:
            cvar_pct = 0.0
        else:
            cvar_pct = abs(float(np.nanmean(worst)))

        return var_pct, cvar_pct

    @staticmethod
    def _monte_carlo(arr: np.ndarray, alpha: float) -> tuple[float, float]:
        """Monte-Carlo simulation VaR & CVaR (10 000 paths)."""
        mu = float(np.nanmean(arr))
        sigma = float(np.nanstd(arr, ddof=1))

        simulated = np.random.default_rng().normal(mu, sigma, 10_000)
        threshold = float(np.percentile(simulated, (1.0 - alpha) * 100.0))
        var_pct = abs(threshold)

        worst = simulated[simulated <= threshold]
        if len(worst) == 0:
            cvar_pct = 0.0
        else:
            cvar_pct = abs(float(np.mean(worst)))

        return var_pct, cvar_pct
