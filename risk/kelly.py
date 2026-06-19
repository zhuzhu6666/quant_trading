"""
risk/kelly.py — Kelly Criterion Position Sizing for XAUUSD M5 trading.

Full Kelly:    f* = (b * p - q) / b
where p = win rate, b = avg_win / avg_loss (odds), q = 1 - p

Half-Kelly:   f* / 2  (default, more conservative)
Quarter-Kelly: f* / 4 (ultra conservative, auto-selected when win_rate <= 0.5)

Then lot = equity * kelly_fraction * risk_per_trade / (atr * contract_size)
"""

from __future__ import annotations

from typing import Dict, Optional

from loguru import logger


class KellyPositionSizer:
    """
    Kelly Criterion position sizing for XAUUSD M5 trading.

    Full Kelly: f* = (b * p - q) / b
    where p = win rate, b = avg_win / avg_loss (odds), q = 1 - p

    Half-Kelly:   f* / 2  (default, more conservative)
    Quarter-Kelly: f* / 4 (ultra conservative)

    Then lot = equity * kelly_fraction * risk_per_trade / (atr * contract_size)

    Parameters
    ----------
    kelly_fraction : float, optional
        Fraction of full Kelly to use (default 0.5 for half-Kelly).
    max_kelly_pct : float, optional
        Maximum fraction of capital to risk via Kelly (cap).
    min_lots : float, optional
        Minimum lot size to return (default 0.01).
    max_lots : float, optional
        Maximum lot size to return (default 0.5).
    risk_per_trade_pct : float, optional
        Fraction of equity risked per trade (default 0.01 = 1%).
    contract_size : float, optional
        Contract size in units per lot (default 100 for XAUUSD, oz/lot).
    """

    def __init__(
        self,
        kelly_fraction: float = 0.5,
        max_kelly_pct: float = 0.25,
        min_lots: float = 0.01,
        max_lots: float = 0.5,
        risk_per_trade_pct: float = 0.01,
        contract_size: float = 100,
    ) -> None:
        self.kelly_fraction = kelly_fraction
        self.max_kelly_pct = max_kelly_pct
        self.min_lots = min_lots
        self.max_lots = max_lots
        self.risk_per_trade_pct = risk_per_trade_pct
        self.contract_size = contract_size

        logger.debug(
            "KellyPositionSizer initialized: kelly_frac={}, max_kelly_pct={}, "
            "min_lots={}, max_lots={}, risk_pct={}, contract={}",
            kelly_fraction,
            max_kelly_pct,
            min_lots,
            max_lots,
            risk_per_trade_pct,
            contract_size,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute_kelly_fraction(
        self,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
    ) -> float:
        """
        Compute the Kelly fraction (capped and scaled).

        Full Kelly formula:
            f* = (b * p - q) / b
        where:
            p = win_rate
            q = 1 - p
            b = avg_win / avg_loss  (odds)

        Applies the configured kelly_fraction scaling (e.g. half-Kelly = 0.5)
        and caps the result at max_kelly_pct.

        When win_rate <= 0.5, automatically uses quarter-Kelly (kelly_fraction
        reduced to 0.25) to be more conservative for low-win-rate strategies.

        Parameters
        ----------
        win_rate : float
            Historical win rate (0.0 to 1.0).
        avg_win : float
            Average winning trade P&L (positive).
        avg_loss : float
            Average losing trade P&L (positive, magnitude of loss).

        Returns
        -------
        float
            Scaled and capped Kelly fraction (0.0 to max_kelly_pct).
        """
        # Edge cases
        if win_rate <= 0.0:
            logger.info("win_rate <= 0, returning 0 Kelly fraction")
            return 0.0

        if avg_loss <= 0.0:
            logger.warning(
                "avg_loss={} is non-positive, using epsilon=1e-6", avg_loss
            )
            avg_loss = 1e-6

        if avg_win <= 0.0:
            logger.info("avg_win <= 0, returning 0 Kelly fraction")
            return 0.0

        # Odds ratio
        b = avg_win / avg_loss
        if b <= 0.0:
            logger.info("odds ratio <= 0, returning 0 Kelly fraction")
            return 0.0

        q = 1.0 - win_rate

        # Full Kelly
        full_kelly = (b * win_rate - q) / b

        # If full Kelly is negative, no edge — return 0
        if full_kelly <= 0.0:
            logger.info("full_kelly={:.4f} (no edge), returning 0", full_kelly)
            return 0.0

        # Determine effective fraction: auto quarter-Kelly for low win rates
        effective_fraction = self.kelly_fraction
        if win_rate <= 0.5:
            effective_fraction = min(effective_fraction, 0.25)
            logger.debug(
                "win_rate={:.3f} <= 0.5, using quarter-Kelly (frac={})",
                win_rate,
                effective_fraction,
            )

        scaled = full_kelly * effective_fraction
        capped = min(scaled, self.max_kelly_pct)

        logger.debug(
            "Kelly: p={:.3f}, b={:.3f}, full={:.4f}, scaled={:.4f}, capped={:.4f}",
            win_rate,
            b,
            full_kelly,
            scaled,
            capped,
        )

        return capped

    def compute_lots(
        self,
        equity: float,
        current_atr: float,
        kelly_frac: float | None = None,
    ) -> float:
        """
        Convert a Kelly fraction to actual lot size.

        Formula:
            risk_dollars = equity * risk_per_trade_pct * kelly_frac
            lot = risk_dollars / (atr * contract_size)

        Result is clamped to [min_lots, max_lots].

        Parameters
        ----------
        equity : float
            Current account equity in account currency.
        current_atr : float
            Current ATR value (in price units) for the instrument.
        kelly_frac : float, optional
            Kelly fraction to use. If None, defaults to max_kelly_pct
            (used when no strategy-specific Kelly has been computed).

        Returns
        -------
        float
            Lot size clamped to [min_lots, max_lots].
        """
        if equity <= 0.0:
            logger.warning("equity={} <= 0, returning min_lots={}", equity, self.min_lots)
            return self.min_lots

        if current_atr <= 0.0:
            logger.warning(
                "atr={} <= 0, using epsilon=1e-6 to avoid division by zero",
                current_atr,
            )
            current_atr = 1e-6

        kf = kelly_frac if kelly_frac is not None else self.max_kelly_pct

        risk_dollars = equity * self.risk_per_trade_pct * kf
        lot = risk_dollars / (current_atr * self.contract_size)

        # Clamp
        raw_lot = lot
        lot = max(self.min_lots, min(lot, self.max_lots))

        logger.debug(
            "Lots: equity={:.2f}, atr={:.5f}, kelly_frac={:.4f}, "
            "risk_dollars={:.2f}, raw_lot={:.4f}, clamped_lot={:.4f}",
            equity,
            current_atr,
            kf,
            risk_dollars,
            raw_lot,
            lot,
        )

        return lot

    def compute_from_stats(
        self,
        equity: float,
        current_atr: float,
        stats: Dict[str, float],
    ) -> Dict[str, object]:
        """
        Convenience method: compute position sizing from a stats dictionary.

        The *stats* dict is expected to contain the keys ``win_rate``,
        ``avg_win``, and ``avg_loss`` (as produced by e.g. AttributionEngine).

        Parameters
        ----------
        equity : float
            Current account equity.
        current_atr : float
            Current ATR value.
        stats : dict[str, float]
            Dictionary with keys ``win_rate``, ``avg_win``, ``avg_loss``.

        Returns
        -------
        dict
            Dictionary containing:
            - ``kelly_frac`` : float — computed Kelly fraction
            - ``lots`` : float — computed lot size
            - ``risk_dollars`` : float — dollar amount at risk
            - ``method`` : str — description of the method used
        """
        win_rate = stats.get("win_rate", 0.0)
        avg_win = stats.get("avg_win", 0.0)
        avg_loss = stats.get("avg_loss", 1e-6)

        kelly_frac = self.compute_kelly_fraction(win_rate, avg_win, avg_loss)

        risk_dollars = equity * self.risk_per_trade_pct * kelly_frac
        lots = self.compute_lots(equity, current_atr, kelly_frac)

        # Determine method string
        if kelly_frac <= 0.0:
            method = "no_edge"
        elif win_rate <= 0.5:
            method = "quarter_kelly"
        elif self.kelly_fraction <= 0.25:
            method = "quarter_kelly"
        elif abs(self.kelly_fraction - 0.5) < 1e-9:
            method = "half_kelly"
        elif abs(self.kelly_fraction - 1.0) < 1e-9:
            method = "full_kelly"
        else:
            method = f"custom_kelly({self.kelly_fraction})"

        result = {
            "kelly_frac": kelly_frac,
            "lots": lots,
            "risk_dollars": round(risk_dollars, 2),
            "method": method,
        }

        logger.info(
            "Kelly sizing result: {method}, kelly_frac={kelly_frac:.4f}, "
            "lots={lots:.4f}, risk_dollars={risk_dollars:.2f}",
            **result,
        )

        return result
