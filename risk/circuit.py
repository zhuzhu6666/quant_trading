"""
Circuit Breaker — 熔断机制

触发条件：
1. 日内亏损超限 (max_daily_loss_pct，相对日内峰值权益)
2. 连续亏损N笔 (consecutive_losses)
3. 滑点过大（已实现平均滑点 > max_slippage_pct）
4. 波动率异常（当前ATR > 历史中位数 × volatility_mult）

一旦触发：当日禁止所有新开仓；已持仓 SL/TP 照常走（不平）。

说明：
- 日损分母用 peak_equity 而非 balance：避免"亏到小数后分母变小"反复触发的死循环
- ATR 序列由调用方维护（中位数基线 + 当前 ATR）
- 熔断触发后必须等到 reset() 才解除（防止同一根 bar 反复触发）
"""

import logging
from collections import deque
from statistics import median

from core.state import state
from core.event_bus import bus, Event, EventType

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """
    熔断控制器

    一旦触发，当日禁止所有新交易。
    """

    def __init__(self, max_daily_loss_pct: float = 5.0,
                 max_consecutive_loss: int = 5,
                 max_slippage_pct: float = 0.5,
                 volatility_mult: float = 3.0,
                 atr_window: int = 100):
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_loss = max_consecutive_loss
        self.max_slippage_pct = max_slippage_pct
        self.volatility_mult = volatility_mult
        self.atr_window = atr_window

        # ATR 历史（bar 末尾喂入）
        self._atr_history: deque[float] = deque(maxlen=atr_window)
        self._slippage_sum: float = 0.0
        self._slippage_count: int = 0

    def feed_atr(self, atr: float):
        """每根 bar 喂入当前 ATR（用于波动率熔断）"""
        if atr is not None and atr > 0:
            self._atr_history.append(atr)

    def feed_slippage(self, slippage_pct: float):
        """每笔成交喂入实际滑点（百分比形式，如 0.02 表示 0.02%）"""
        if slippage_pct is not None and slippage_pct >= 0:
            self._slippage_sum += slippage_pct
            self._slippage_count += 1

    @property
    def avg_slippage_pct(self) -> float:
        return (self._slippage_sum / self._slippage_count) if self._slippage_count else 0.0

    @property
    def is_tripped(self) -> bool:
        return state.is_circuit_breaker

    def check_all(self) -> tuple[bool, str]:
        """
        检查所有熔断条件

        Returns: (是否触发, 原因)
        """
        # 已触发就别再检查（避免重复日志）
        if state.is_circuit_breaker:
            return True, state.circuit_reason

        # 1. 日内亏损（相对 peak_equity，更稳定）
        peak = state.daily.peak_equity or state.balance
        if peak > 0:
            dd_pct = (peak - state.equity) / peak * 100
            if dd_pct >= self.max_daily_loss_pct:
                self.trip(f"日内回撤{dd_pct:.1f}%达到上限{self.max_daily_loss_pct}%")
                return True, state.circuit_reason

        # 2. 连续亏损
        if state.daily.consecutive_losses >= self.max_consecutive_loss:
            self.trip(f"连续亏损{state.daily.consecutive_losses}笔达到上限{self.max_consecutive_loss}")
            return True, state.circuit_reason

        # 3. 滑点
        if self._slippage_count >= 5 and self.avg_slippage_pct > self.max_slippage_pct:
            self.trip(f"平均滑点{self.avg_slippage_pct:.2f}%超过上限{self.max_slippage_pct}%")
            return True, state.circuit_reason

        # 4. 波动率异常
        if len(self._atr_history) >= max(20, self.atr_window // 5):
            med = median(self._atr_history)
            cur = self._atr_history[-1]
            if med > 0 and cur > self.volatility_mult * med:
                self.trip(f"波动率异常：ATR={cur:.2f} > {self.volatility_mult}×中位数{med:.2f}")
                return True, state.circuit_reason

        return False, "OK"

    def trip(self, reason: str):
        """手动触发熔断

        P5a (audit 2026-06-04 BUG-10): 走 state.mark_breaker() 发 event,
        而不是直接赋 state.is_circuit_breaker = True (那样不通知 bus subscribers)。
        """
        if state.is_circuit_breaker:
            return  # 已触发
        state.mark_breaker(True, reason)
        logger.warning(f"CIRCUIT BREAKER TRIPPED: {reason}")
        # 注: state.mark_breaker 已经 publish event, 这里不再 publish
        # (老代码重复 publish 会被 bus 收到 2 次)

    def reset(self):
        """重置熔断（跨日后）

        注意: 不调 state.reset_daily() — daily stats 的重置由调用方 (paper_trader._reset_daily_stats
        / mab_paper_runner) 负责, 它们用 DailyStats(date, peak_equity=peak) 保留 peak。
        这里只清 circuit 自己的状态 (ATR 序列 + 滑点累计 + 触发标志)。
        P5a: 走 state.mark_breaker(False, "") 而非直写。
        """
        state.mark_breaker(False, "")
        self._atr_history.clear()
        self._slippage_sum = 0.0
        self._slippage_count = 0
        logger.info("Circuit breaker reset")


# ── Module-level helper: auto-tune risk parameters ─────────────────────


def auto_tune_risk(df: "pd.DataFrame", equity: float = 1000.0) -> dict:
    """Dynamically tune risk parameters based on volatility (ATR percentile)
    and current equity.

    1. Compute ATR(14) over the last 100+ bars via :func:`risk.regime._atr`.
    2. Locate the latest ATR's percentile within its own history (0-100).
    3. Adjust ``max_daily_loss_pct``:
       - ATR > 70th pctile → tighten 30% (5.0 * 0.7)
       - ATR < 30th pctile → loosen 20%  (5.0 * 1.2)
       - otherwise          → keep 5.0 %
    4. ``single_trade_risk_usd = max(2.0, equity * 0.002)``  (0.2 % of equity, min $2).

    Returns:
        {"max_daily_loss_pct", "single_trade_risk_usd", "atr_percentile"}
    """
    import numpy as np

    atr_percentile: float = 50.0  # neutral default if we cannot compute

    # ── ATR percentile ──────────────────────────────────────────────
    # We need at least high + low + close for true ATR.
    has_hl = "high" in df.columns and "low" in df.columns
    if "close" in df.columns:
        closes = df["close"].values
        if has_hl:
            highs = df["high"].values
            lows = df["low"].values
        else:
            highs = lows = closes  # will fall through to close-range below

        # Use the last 101 bars (100 + 1 for ATR lookback)
        n = min(101, len(closes))
        if n >= 16:  # at least ATR(14) + 1
            if has_hl:
                from risk.regime import _atr as _regime_atr

                atr_series = _regime_atr(
                    highs[-n:], lows[-n:], closes[-n:], period=14
                )
            else:
                # Fallback: close-to-close range as a volatility proxy
                ranges = np.abs(np.diff(closes[-n:]))
                # Pad front with NaN so lengths match
                atr_series = np.empty(n)
                atr_series[:] = np.nan
                atr_series[1:] = ranges

            atr_valid = atr_series[~np.isnan(atr_series)]
            if len(atr_valid) >= 5:
                last_atr = atr_valid[-1]
                pct = float(np.sum(atr_valid <= last_atr)) / len(atr_valid) * 100.0
                # Clamp to 0-100 (the <= can give 100 exactly; cap it)
                atr_percentile = min(max(pct, 0.0), 100.0)

    # ── Adjust max_daily_loss_pct ───────────────────────────────────
    if atr_percentile > 70:
        max_daily_loss_pct = 5.0 * 0.7  # tighten 30 %
    elif atr_percentile < 30:
        max_daily_loss_pct = 5.0 * 1.2  # loosen 20 %
    else:
        max_daily_loss_pct = 5.0

    # ── Single-trade risk based on equity ───────────────────────────
    single_trade_risk_usd = max(2.0, equity * 0.002)

    return {
        "max_daily_loss_pct": round(max_daily_loss_pct, 2),
        "single_trade_risk_usd": round(single_trade_risk_usd, 2),
        "atr_percentile": round(atr_percentile, 1),
    }
