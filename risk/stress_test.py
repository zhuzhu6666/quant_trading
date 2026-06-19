"""
risk/stress_test.py — StressTester class for predefined stress scenarios.

Scenarios
---------
1. **Black Swan**       : XAUUSD -5% single-day price crash (2020.08 vol regime)
2. **NFP Shock**        : NFP beats 3σ → instant 10 bps slippage on exit
3. **Liquidity Drought**: spread from 0.2 pips → 5.0 pips (25×)
4. **cTrader Disconnect**: 2 h offline → gap on market open (max adverse move)
5. **Factor Failure**   : all factor signals go to zero simultaneously
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

# ---------------------------------------------------------------------------
# XAUUSD instrument constants
# ---------------------------------------------------------------------------

XAUUSD_PIP = 0.01  # 1 pip in price terms (typical 5-digit broker)
XAUUSD_CONTRACT_SIZE = 100  # oz per standard lot
XAUUSD_PIP_VALUE_PER_LOT = XAUUSD_PIP * XAUUSD_CONTRACT_SIZE  # $1.00 / lot
XAUUSD_TYPICAL_PRICE = 2000.0  # ~USD/oz

NORMAL_SPREAD_PIPS = 0.25  # mid-point of 0.2–0.3 pips
DROUGHT_SPREAD_PIPS = 5.0

ATR_M5_PER_LOT = 10.0  # $ per lot per 5-min bar (mid-point of $8–12)


# ---------------------------------------------------------------------------
# Result data class
# ---------------------------------------------------------------------------


@dataclass
class StressScenarioResult:
    """Outcome of a single stress scenario."""

    name: str
    description: str
    expected_loss_pct: float
    expected_loss_usd: float
    survivable: bool
    details: str = ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _pos_val(pos: dict[str, Any], key: str, default: Any) -> Any:
    """Gracefully access a position field, falling back to *default*."""
    return pos.get(key, default)


def _account_balance(acc: dict[str, Any]) -> float:
    """Return account balance (fallback → $100 k)."""
    return acc.get("balance", 100_000.0)


def _direction_sign(pos: dict[str, Any]) -> float:
    """+1 for long / buy; -1 for short / sell."""
    d = _pos_val(pos, "direction", 1)
    if isinstance(d, (int, float)):
        return 1.0 if d > 0 else -1.0
    return 1.0 if d.lower() in ("buy", "long", "bull") else -1.0


# ---------------------------------------------------------------------------
# StressTester
# ---------------------------------------------------------------------------


class StressTester:
    """Pre-defined stress test scenarios for risk assessment.

    Parameters
    ----------
    max_survivable_loss_pct : float
        Maximum portfolio loss (as % of balance) considered survivable.
        Default is 20.0 (i.e. 20 %).

    Usage
    -----
    >>> tester = StressTester(max_survivable_loss_pct=15.0)
    >>> results = tester.run_all(
    ...     positions=[{"symbol": "XAUUSD", "direction": "buy",
    ...                 "volume": 0.01, "entry_price": 2010.0,
    ...                 "current_price": 2020.0}],
    ...     account={"balance": 100_000.0, "equity": 101_000.0},
    ...     factor_signals={"momentum": 0.5, "carry": -0.2},
    ... )
    >>> summary = tester.summary(results)
    """

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self, max_survivable_loss_pct: float = 20.0) -> None:
        self.max_survivable_loss_pct = max_survivable_loss_pct
        logger.info(
            "StressTester initialised | max_survivable_loss_pct={}",
            max_survivable_loss_pct,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_all(
        self,
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        factor_signals: dict[str, Any] | None = None,
    ) -> list[StressScenarioResult]:
        """Run all 5 predefined stress scenarios.

        Parameters
        ----------
        positions : list[dict]
            Each dict should contain keys: *symbol*, *direction*, *volume*,
            *entry_price*, *current_price*. Missing keys are defaulted.
        account : dict
            Must contain *balance* (and optionally *equity*).
        factor_signals : dict | None
            Optional dict of factor-name → signal-value. Used only for the
            ``factor_failure`` scenario.

        Returns
        -------
        list[StressScenarioResult]
            One result per scenario.
        """
        logger.info("Running all 5 stress scenarios …")
        results = [
            self._scenario_black_swan(positions, account),
            self._scenario_nfp_shock(positions, account),
            self._scenario_liquidity_drought(positions, account),
            self._scenario_ctrader_disconnect(positions, account),
            self._scenario_factor_failure(positions, account, factor_signals),
        ]
        logger.info("Stress test complete — {} scenarios evaluated", len(results))
        return results

    def run_scenario(
        self,
        name: str,
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        factor_signals: dict[str, Any] | None = None,
    ) -> StressScenarioResult:
        """Run a single named scenario.

        Parameters
        ----------
        name : str
            One of ``black_swan``, ``nfp_shock``, ``liquidity_drought``,
            ``ctrader_disconnect``, ``factor_failure``.
        positions, account, factor_signals
            See :meth:`run_all`.

        Returns
        -------
        StressScenarioResult
        """
        logger.info("Running scenario '{}' …", name)
        name_map: dict[str, Any] = {
            "black_swan": self._scenario_black_swan,
            "nfp_shock": self._scenario_nfp_shock,
            "liquidity_drought": self._scenario_liquidity_drought,
            "ctrader_disconnect": self._scenario_ctrader_disconnect,
            "factor_failure": self._scenario_factor_failure,
        }
        if name not in name_map:
            msg = f"Unknown scenario: {name!r}. Choose from {list(name_map)}"
            logger.error(msg)
            raise ValueError(msg)

        result = name_map[name](positions, account, factor_signals)
        logger.info(
            "Scenario '{}' → loss_pct={} loss_usd={} survivable={}",
            name,
            result.expected_loss_pct,
            result.expected_loss_usd,
            result.survivable,
        )
        return result

    # ------------------------------------------------------------------
    # Scenario implementations
    # ------------------------------------------------------------------

    def _scenario_black_swan(
        self,
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        _factor_signals: dict[str, Any] | None = None,
    ) -> StressScenarioResult:
        """-5 % instant price drop → PnL impact on current positions.

        Long positions lose 5 % of notional; short positions gain 5 %.
        The *expected_loss* is the *net* loss (gains offset losses).
        """
        if not positions:
            return StressScenarioResult(
                name="Black Swan",
                description="-5% single-day price crash (2020 vol regime)",
                expected_loss_pct=0.0,
                expected_loss_usd=0.0,
                survivable=True,
                details="No positions open — no impact.",
            )

        total_pnl = 0.0
        lines: list[str] = []

        for pos in positions:
            vol = _pos_val(pos, "volume", 0.01)
            price = _pos_val(pos, "current_price", XAUUSD_TYPICAL_PRICE)
            direction = _direction_sign(pos)

            # 5 % of notional value
            impact = price * 0.05 * vol * XAUUSD_CONTRACT_SIZE
            pnl = -impact * direction  # longs lose, shorts gain
            total_pnl += pnl
            lbl = "gain" if pnl >= 0 else "loss"
            lines.append(
                f"{_pos_val(pos, 'direction', 'buy')} {vol} lot(s): "
                f"${abs(pnl):,.2f} ({lbl})"
            )

        balance = _account_balance(account)
        # Report only losses (gains reduce the loss)
        net_loss = max(-total_pnl, 0.0)
        loss_pct = (net_loss / balance * 100) if balance > 0 else 0.0
        survivable = loss_pct < self.max_survivable_loss_pct

        logger.info(
            "Black Swan → net_pnl=${:+,.2f} loss_pct={:.2f}% | survivable={}",
            total_pnl,
            loss_pct,
            survivable,
        )

        return StressScenarioResult(
            name="Black Swan",
            description="-5% single-day price crash (2020 vol regime)",
            expected_loss_pct=round(loss_pct, 4),
            expected_loss_usd=round(net_loss, 2),
            survivable=survivable,
            details="; ".join(lines),
        )

    def _scenario_nfp_shock(
        self,
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        _factor_signals: dict[str, Any] | None = None,
    ) -> StressScenarioResult:
        """Extra 10 bps slippage when exiting current positions during NFP.

        The cost is applied to the *current* notional value (10 bps = 0.001).
        """
        if not positions:
            return StressScenarioResult(
                name="NFP Shock",
                description="NFP beats 3σ → instant 10 bps slippage",
                expected_loss_pct=0.0,
                expected_loss_usd=0.0,
                survivable=True,
                details="No positions open — no impact.",
            )

        SLIPPAGE_RATIO = 10 / 10_000  # 10 basis points

        total_cost = 0.0
        lines: list[str] = []

        for pos in positions:
            vol = _pos_val(pos, "volume", 0.01)
            price = _pos_val(pos, "current_price", XAUUSD_TYPICAL_PRICE)

            cost = vol * XAUUSD_CONTRACT_SIZE * price * SLIPPAGE_RATIO
            total_cost += cost
            lines.append(
                f"{_pos_val(pos, 'direction', 'buy')} {vol} lot(s): "
                f"slippage ${cost:,.2f}"
            )

        balance = _account_balance(account)
        loss_pct = (total_cost / balance * 100) if balance > 0 else 0.0
        survivable = loss_pct < self.max_survivable_loss_pct

        logger.info(
            "NFP Shock → extra_cost=${:,.2f} ({:.2f}% of balance) | survivable={}",
            total_cost,
            loss_pct,
            survivable,
        )

        return StressScenarioResult(
            name="NFP Shock",
            description="NFP beats 3σ → instant 10 bps slippage",
            expected_loss_pct=round(loss_pct, 4),
            expected_loss_usd=round(total_cost, 2),
            survivable=survivable,
            details="; ".join(lines),
        )

    def _scenario_liquidity_drought(
        self,
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        _factor_signals: dict[str, Any] | None = None,
    ) -> StressScenarioResult:
        """Spread widens from 0.25 pips (normal) → 5.0 pips (drought).

        The extra cost is the difference in spread, applied per position.
        """
        if not positions:
            return StressScenarioResult(
                name="Liquidity Drought",
                description="Spread from 0.2 to 5.0 pips (25×)",
                expected_loss_pct=0.0,
                expected_loss_usd=0.0,
                survivable=True,
                details="No positions open — no impact.",
            )

        extra_spread_pips = DROUGHT_SPREAD_PIPS - NORMAL_SPREAD_PIPS  # 4.75

        total_cost = 0.0
        lines: list[str] = []

        for pos in positions:
            vol = _pos_val(pos, "volume", 0.01)

            # Cost = extra pips × pip-value-per-lot × lots
            cost = extra_spread_pips * XAUUSD_PIP_VALUE_PER_LOT * vol
            total_cost += cost
            lines.append(
                f"{_pos_val(pos, 'direction', 'buy')} {vol} lot(s): "
                f"extra spread ${cost:,.2f}"
            )

        balance = _account_balance(account)
        loss_pct = (total_cost / balance * 100) if balance > 0 else 0.0
        survivable = loss_pct < self.max_survivable_loss_pct

        logger.info(
            "Liquidity Drought → extra_cost=${:,.2f} ({:.2f}% of balance) | "
            "survivable={}",
            total_cost,
            loss_pct,
            survivable,
        )

        return StressScenarioResult(
            name="Liquidity Drought",
            description="Spread from 0.2 to 5.0 pips (25×)",
            expected_loss_pct=round(loss_pct, 4),
            expected_loss_usd=round(total_cost, 2),
            survivable=survivable,
            details="; ".join(lines),
        )

    def _scenario_ctrader_disconnect(
        self,
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        _factor_signals: dict[str, Any] | None = None,
    ) -> StressScenarioResult:
        """2 h cTrader outage → gap open → max adverse movement.

        2h ATR approximated as ``1.5 × ATR_M5 × 24`` per lot, giving a
        worst-case adverse price move in the *wrong* direction for each open
        position (longs lose on gap-down, shorts lose on gap-up).
        """
        if not positions:
            return StressScenarioResult(
                name="cTrader Disconnect",
                description="2 h offline → gap on market open",
                expected_loss_pct=0.0,
                expected_loss_usd=0.0,
                survivable=True,
                details="No positions open — no impact.",
            )

        # 2h ATR ≈ 1.5 × M5_ATR × 24  (dollars per lot)
        atr_2h_per_lot = 1.5 * ATR_M5_PER_LOT * 24  # e.g. 360.0

        total_loss = 0.0
        lines: list[str] = []

        for pos in positions:
            vol = _pos_val(pos, "volume", 0.01)
            # Worst-case adverse move = ATR magnitude regardless of direction
            loss = atr_2h_per_lot * vol
            total_loss += loss
            lines.append(
                f"{_pos_val(pos, 'direction', 'buy')} {vol} lot(s): "
                f"adverse move ${loss:,.2f}"
            )

        balance = _account_balance(account)
        loss_pct = (total_loss / balance * 100) if balance > 0 else 0.0
        survivable = loss_pct < self.max_survivable_loss_pct

        logger.info(
            "cTrader Disconnect → adverse_move=${:,.2f} ({:.2f}% of balance) | "
            "survivable={}",
            total_loss,
            loss_pct,
            survivable,
        )

        return StressScenarioResult(
            name="cTrader Disconnect",
            description="2 h offline → gap on market open",
            expected_loss_pct=round(loss_pct, 4),
            expected_loss_usd=round(total_loss, 2),
            survivable=survivable,
            details="; ".join(lines),
        )

    def _scenario_factor_failure(
        self,
        positions: list[dict[str, Any]],
        account: dict[str, Any],
        factor_signals: dict[str, Any] | None = None,
    ) -> StressScenarioResult:
        """All factor signals go to zero simultaneously.

        If factor signals are provided and the portfolio has open positions,
        the prudent action is to exit all positions at the normal spread
        (the cost of unwinding). If no signals are provided, the scenario
        reports no impact.
        """
        if not factor_signals:
            return StressScenarioResult(
                name="Factor Failure",
                description="All factor signals go to zero simultaneously",
                expected_loss_pct=0.0,
                expected_loss_usd=0.0,
                survivable=True,
                details="No factor signals provided — no impact.",
            )

        if not positions:
            return StressScenarioResult(
                name="Factor Failure",
                description="All factor signals go to zero simultaneously",
                expected_loss_pct=0.0,
                expected_loss_usd=0.0,
                survivable=True,
                details="No positions open — no impact.",
            )

        # Cost of closing all positions at the normal (tight) spread
        total_cost = 0.0
        lines: list[str] = []

        for pos in positions:
            vol = _pos_val(pos, "volume", 0.01)
            cost = NORMAL_SPREAD_PIPS * XAUUSD_PIP_VALUE_PER_LOT * vol
            total_cost += cost
            lines.append(
                f"{_pos_val(pos, 'direction', 'buy')} {vol} lot(s): "
                f"exit cost ${cost:,.2f}"
            )

        balance = _account_balance(account)
        loss_pct = (total_cost / balance * 100) if balance > 0 else 0.0
        survivable = loss_pct < self.max_survivable_loss_pct

        logger.info(
            "Factor Failure → exit_cost=${:,.2f} ({:.2f}% of balance) | "
            "survivable={}",
            total_cost,
            loss_pct,
            survivable,
        )

        return StressScenarioResult(
            name="Factor Failure",
            description="All factor signals go to zero simultaneously",
            expected_loss_pct=round(loss_pct, 4),
            expected_loss_usd=round(total_cost, 2),
            survivable=survivable,
            details="; ".join(lines),
        )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def summary(self, results: list[StressScenarioResult]) -> dict[str, Any]:
        """Aggregate scenario results into a summary dictionary.

        Returns
        -------
        dict with keys:
            - ``max_loss_pct``   : float — biggest single-scenario loss (%)
            - ``max_loss_usd``   : float — biggest single-scenario loss ($)
            - ``survivable``     : bool  — True if *all* scenarios are survivable
            - ``scenarios``      : list[dict] — per-scenario breakdown
        """
        max_loss_pct = max(r.expected_loss_pct for r in results)
        max_loss_usd = max(r.expected_loss_usd for r in results)
        all_survivable = all(r.survivable for r in results)

        scenarios_list = [
            {
                "name": r.name,
                "description": r.description,
                "expected_loss_pct": r.expected_loss_pct,
                "expected_loss_usd": r.expected_loss_usd,
                "survivable": r.survivable,
                "details": r.details,
            }
            for r in results
        ]

        out: dict[str, Any] = {
            "max_loss_pct": max_loss_pct,
            "max_loss_usd": max_loss_usd,
            "survivable": all_survivable,
            "scenarios": scenarios_list,
        }

        logger.info(
            "Stress summary → max_loss={:.2f}% / ${:,.2f} | all_survivable={}",
            max_loss_pct,
            max_loss_usd,
            all_survivable,
        )
        return out
