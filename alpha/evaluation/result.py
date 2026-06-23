"""alpha/evaluation/result.py — 统一评价接口.

回测/实盘/影子共用 EvaluationResult, 三种来源可通过工厂方法互相转换.

用法:
    # Backtest:
    result = EvaluationResult.from_backtest(bt_result, symbol="XAUUSD+")

    # Live:
    result = EvaluationResult.from_attribution(attr_engine, symbol="XAUUSD+")

    # Shadow:
    result = EvaluationResult.from_shadow(shadow_perf, symbol="XAUUSD+")

    # 统一输出:
    print(result.summary_text())
    api_data = result.to_dict()
"""

from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field, asdict
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ── 年化因子 ────────────────────────────────────────────────────────
_BPV: dict[str, int] = {
    "M1": 525600, "M5": 105120, "M15": 35040,
    "M30": 17520, "H1": 8760, "H4": 2190, "D1": 365,
}


@dataclass
class EvaluationResult:
    """统一评价结果 — 回测/实盘/影子共用.

    Args:
        source: 来源 ("backtest" | "live" | "shadow")
        symbol: 品种
        timeframe: 周期
        n_trades: 总交易笔数
        total_pnl: 总盈亏 (USD)
        win_rate: 胜率 (0~1)
        sharpe: 年化夏普
        max_drawdown: 最大回撤 (%)
        avg_holding_bars: 平均持仓 bar 数
        profit_factor: 盈亏比
        period_start: 评估起始时间戳
        period_end: 评估结束时间戳
        total_return_pct: 总收益率 (%)
        factor_returns: {因子名 → 累计收益} (可选)
        meta: 其他元数据
    """
    source: str = "backtest"
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"

    n_trades: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    sharpe: float = 0.0
    max_drawdown: float = 0.0
    avg_holding_bars: float = 0.0
    profit_factor: float = 0.0

    period_start: float = 0.0
    period_end: float = 0.0
    total_return_pct: float = 0.0

    factor_returns: dict[str, float] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON 兼容字典."""
        d = asdict(self)
        d["period_start_iso"] = _ts_to_iso(self.period_start) if self.period_start else ""
        d["period_end_iso"] = _ts_to_iso(self.period_end) if self.period_end else ""
        d["total_pnl"] = round(self.total_pnl, 2)
        d["win_rate"] = round(self.win_rate, 4)
        d["sharpe"] = round(self.sharpe, 4)
        d["max_drawdown"] = round(self.max_drawdown, 2)
        d["profit_factor"] = round(self.profit_factor, 4)
        d["total_return_pct"] = round(self.total_return_pct, 2)
        return d

    def summary_text(self) -> str:
        """可读摘要."""
        return (
            f"━━━ {self.source.upper()} ━━━ {self.symbol} {self.timeframe} ━━━\n"
            f"  交易: {self.n_trades}笔  |  胜率: {self.win_rate:.1%}\n"
            f"  总PnL: ${self.total_pnl:+.2f}  |  收益率: {self.total_return_pct:+.2f}%\n"
            f"  夏普: {self.sharpe:.2f}  |  最大回撤: {self.max_drawdown:.2f}%\n"
            f"  盈亏比: {self.profit_factor:.2f}  |  平均持仓: {self.avg_holding_bars:.0f} bar\n"
            f"  期间: {_ts_to_iso(self.period_start)} → {_ts_to_iso(self.period_end)}"
        )

    # ── 工厂方法 ────────────────────────────────────────────────────

    @classmethod
    def from_backtest(
        cls,
        bt_result: Any,
        *,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
        **meta,
    ) -> EvaluationResult:
        """从 BacktestResult 构造."""
        return cls(
            source="backtest",
            symbol=symbol,
            timeframe=timeframe,
            n_trades=getattr(bt_result, "n_trades", 0),
            total_pnl=getattr(bt_result, "total_pnl", 0.0),
            win_rate=getattr(bt_result, "win_rate", 0.0),
            sharpe=getattr(bt_result, "sharpe_ratio", 0.0),
            max_drawdown=getattr(bt_result, "max_drawdown", 0.0),
            avg_holding_bars=getattr(bt_result, "avg_hold_bars", 0.0),
            profit_factor=getattr(bt_result, "profit_factor", 0.0),
            total_return_pct=getattr(bt_result, "total_return", 0.0) * 100,
            meta=meta,
        )

    @classmethod
    def from_attribution(
        cls,
        attr_engine: Any,
        *,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
        **meta,
    ) -> EvaluationResult:
        """从 AttributionEngine 构造 (实盘归因)."""
        try:
            stats = attr_engine.get_all_factor_stats()
        except Exception:
            stats = {}

        n_trades = 0
        total_pnl = 0.0
        total_wins = 0
        sharpe_sum = 0.0
        dd_sum = 0.0
        hold_sum = 0.0
        factor_returns: dict[str, float] = {}
        n_factors = 0

        for name, s in stats.items():
            n = getattr(s, "n_trades", 0)
            n_trades += n
            total_pnl += getattr(s, "net_pnl", 0.0) if hasattr(s, "net_pnl") else getattr(s, "total_pnl", 0.0)
            total_wins += getattr(s, "win_count", 0) if hasattr(s, "win_count") else 0
            sharpe_sum += getattr(s, "composite_sharpe_score", 0.0) if hasattr(s, "composite_sharpe_score") else 0.0
            dd_sum += getattr(s, "max_dd", 0.0) if hasattr(s, "max_dd") else 0.0
            hold_sum += getattr(s, "avg_holding_runtime", 0.0) if hasattr(s, "avg_holding_runtime") else 0.0
            mc = getattr(s, "avg_mc", 0.0) if hasattr(s, "avg_mc") else 0.0
            if mc != 0:
                factor_returns[name] = mc
            n_factors += 1

        win_rate = total_wins / n_trades if n_trades > 0 else 0.0
        avg_sharpe = sharpe_sum / n_factors if n_factors > 0 else 0.0
        avg_dd = dd_sum / n_factors if n_factors > 0 else 0.0
        avg_hold = hold_sum / n_factors if n_factors > 0 else 0.0

        return cls(
            source="live",
            symbol=symbol,
            timeframe=timeframe,
            n_trades=n_trades,
            total_pnl=total_pnl,
            win_rate=win_rate,
            sharpe=avg_sharpe,
            max_drawdown=avg_dd,
            avg_holding_bars=avg_hold,
            factor_returns=factor_returns,
            total_return_pct=0.0,  # 需要初始资金才能算
            meta=meta,
        )

    @classmethod
    def from_shadow(
        cls,
        shadow_perf: Any,
        *,
        symbol: str = "XAUUSD+",
        timeframe: str = "",
        **meta,
    ) -> EvaluationResult:
        """从 ShadowPerf 构造 (影子虚拟交易)."""
        factor_name = getattr(shadow_perf, "factor", "unknown")
        oos_bars = getattr(shadow_perf, "oos_bars", 0)
        cumulative_pnl = getattr(shadow_perf, "cumulative_pnl", 0.0)
        hit_rate = getattr(shadow_perf, "hit_rate", 0.0)
        max_dd = getattr(shadow_perf, "max_drawdown", 0.0)

        tf = timeframe or getattr(shadow_perf, "timeframe", "M5")

        return cls(
            source="shadow",
            symbol=symbol,
            timeframe=tf,
            n_trades=oos_bars,
            total_pnl=cumulative_pnl,
            win_rate=hit_rate,
            sharpe=0.0,  # 影子无 sharpe
            max_drawdown=max_dd,
            avg_holding_bars=1,  # 影子是 1-bar-ahead
            profit_factor=0.0,
            factor_returns={factor_name: cumulative_pnl},
            total_return_pct=cumulative_pnl * 100,
            meta=meta,
        )


def _ts_to_iso(ts: float) -> str:
    """时间戳 → ISO 字符串."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()[:19]
