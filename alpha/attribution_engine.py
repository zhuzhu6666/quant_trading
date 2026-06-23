"""AttributionEngine — 实盘归因引擎。

开仓时记录 TradeAttribution, 平仓时计算每个因子的边际贡献。
使用线性 MC 近似作为默认方法, 当因子数 ≥ 3 且样本 ≥ 10 笔时
升级到 Gram-Schmidt 正交归因 (复用 alpha/evaluation/attribution.py)。

设计文档: docs/FACTOR_TAKEOVER_V4.md §8
"""

import json
import logging
import math
import time
import os as _os
from collections import deque
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_DB_ID_SEQ = count(1)

def _next_db_id() -> int:
    return int(time.time()) + next(_DB_ID_SEQ)



# ═══════════════════════════════════════════════════════════
# TradeAttribution — 开仓时记录
# ═══════════════════════════════════════════════════════════

@dataclass
class TradeAttribution:
    """开仓时记录的归因数据, 平仓时用于计算边际贡献。"""
    position_id: int
    open_ts: float
    open_price: float
    direction: int                    # 1=LONG, -1=SHORT
    factor_signals: dict[str, float]  # 归一化信号 {name: signal ∈ [-1,+1]}
    factor_values: dict[str, float]   # 原始值
    active_weights: dict[str, float]  # 实际权重
    composite_score: float            # 综合分
    tactical_score: float
    macro_score: float
    tags_breakdown: dict[str, float]
    total_signal_abs: float           # Σ|signal_j| 用于 MC 计算
    api_volume: float = 1.0           # cTrader API volume (filled)


# ═══════════════════════════════════════════════════════════
# FactorAttributionStats — 单因子归因滚动统计
# ═══════════════════════════════════════════════════════════

@dataclass
class FactorAttributionStats:
    """单因子归因滚动统计。

    核心指标使用 Newey-West HAC Sharpe ratio (来自 execution/_sharpe.py),
    而非简单 mean/std IR。同时提供 Bootstrap CI 和 DSR 多重检验。
    """
    name: str
    n_trades: int = 0
    n_voted: int = 0          # 非弃权次数
    wins: int = 0
    total_mc: float = 0.0     # 边际贡献累计
    recent_mcs: deque = field(default_factory=lambda: deque(maxlen=250))
    recent_pnls: deque = field(default_factory=lambda: deque(maxlen=250))
    recent_pnl_directions: deque = field(default_factory=lambda: deque(maxlen=50))
    # ── 真实 PnL (来自 cTrader get_deals) ──
    total_gross: float = 0.0
    total_swap: float = 0.0
    total_commission: float = 0.0
    total_net_pnl: float = 0.0

    def record(self, mc: float, is_win: bool, tags: dict[str, float],
               real_pnl: dict | None = None):
        """记录一笔交易的归因结果。

        Args:
            mc: 边际贡献 (MC decomposition).
            is_win: 该笔是否盈利.
            tags: 因子标签.
            real_pnl: 可选，cTrader 真实盈亏:
                      {"gross": ..., "swap": ..., "commission": ..., "net": ...}
        """
        self.n_trades += 1
        self.total_mc += mc
        self.recent_mcs.append(mc)
        self.n_voted += 1
        if is_win:
            self.wins += 1
        self.recent_pnl_directions.append(1 if is_win else -1)
        # ── 真实 PnL ──
        if real_pnl:
            self.total_gross += real_pnl.get("gross", 0.0) or 0.0
            self.total_swap += real_pnl.get("swap", 0.0) or 0.0
            self.total_commission += real_pnl.get("commission", 0.0) or 0.0
            self.total_net_pnl += real_pnl.get("net", 0.0) or 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n_voted if self.n_voted > 0 else 0.0

    @property
    def avg_mc(self) -> float:
        return self.total_mc / self.n_voted if self.n_voted > 0 else 0.0

    # ── Newey-West HAC Sharpe ──

    @property
    def sharpe_short(self) -> float:
        """Sharpe(50): 最近 50 笔 MC 的 NW-HAC Sharpe。"""
        return self._compute_nw_sharpe(50)

    @property
    def sharpe_mid(self) -> float:
        """Sharpe(100): 最近 100 笔 MC 的 NW-HAC Sharpe。"""
        return self._compute_nw_sharpe(100)

    @property
    def sharpe_long(self) -> float:
        """Sharpe(250): 最近 250 笔 MC 的 NW-HAC Sharpe。"""
        return self._compute_nw_sharpe(250)

    @property
    def composite_sharpe_score(self) -> float:
        """综合 Sharpe 分数: 0.5×S50 + 0.3×S100 + 0.2×S250"""
        s50 = self.sharpe_short
        s100 = self.sharpe_mid
        s250 = self.sharpe_long
        score = 0.0
        weight_sum = 0.0
        for s, w in [(s50, 0.5), (s100, 0.3), (s250, 0.2)]:
            if not math.isnan(s):
                score += s * w
                weight_sum += w
        return score / weight_sum if weight_sum > 0 else 0.0

    def _compute_nw_sharpe(self, window: int) -> float:
        """从 MC 序列构造 equity curve, 计算 NW-HAC Sharpe。"""
        try:
            from execution._sharpe import sharpe_ratio_log_nw
        except ImportError:
            return float("nan")

        data = list(self.recent_mcs)[-window:]
        if len(data) < 30:
            return float("nan")
        # MC 序列 → equity curve
        equity = np.cumsum(data)
        equity = equity - equity[0] + 1000  # 归一化起始净值
        if np.any(equity <= 0):
            equity = equity - equity.min() + 100
        return sharpe_ratio_log_nw(equity, "M15")

    # ── 简单 IR (快速参考，非正式统计量) ──

    @property
    def ir_short(self) -> float:
        """IR(50): 最近 50 笔 MC 的 mean/std。"""
        return self._compute_ir(50)

    @property
    def ir_mid(self) -> float:
        """IR(100): 最近 100 笔 MC 的 mean/std。"""
        return self._compute_ir(100)

    @property
    def ir_long(self) -> float:
        """IR(250): 最近 250 笔 MC 的 mean/std。"""
        return self._compute_ir(250)

    def _compute_ir(self, window: int) -> float:
        data = list(self.recent_mcs)[-window:]
        if len(data) < 20:
            return float("nan")
        arr = np.array(data)
        mean, std = arr.mean(), arr.std()
        if std < 1e-10:
            return 0.0
        return float(mean / std)

    # ── Bootstrap CI (来自 alpha/evaluation/bootstrap_ci.py) ──

    def sharpe_ci(self, window: int = 100, alpha: float = 0.05
                  ) -> tuple[float, float] | None:
        """MC Sharpe 的 Bootstrap 置信区间。

        用 BootstrapCI.ci_sharpe() 计算，返回 (lo, hi)。
        如果 CI 包含 0，则该因子 Sharpe 在统计上不显著。
        """
        data = list(self.recent_mcs)[-window:]
        if len(data) < 30:
            return None
        try:
            from execution._sharpe import TF_BARS_PER_YEAR
            from alpha.evaluation.bootstrap_ci import BootstrapCI
            ci = BootstrapCI(
                alpha=alpha, n_iterations=1000, random_seed=42,
                annualization_factor=TF_BARS_PER_YEAR["M15"],
            )
            result = ci.ci_sharpe(np.array(data))
            return (result.ci_lower, result.ci_upper)
        except Exception:
            return None

    # ── DSR 多重检验 (来自 alpha/calibration.py) ──

    def is_statistically_significant(self, n_trials: int = 39) -> dict:
        """用 Deflated Sharpe Ratio 检验该因子的 Sharpe 是否显著 > 0。

        DSR 校正了：
        1. 多重检验偏倚 (39 个因子同时测试)
        2. 收益分布非正态 (skew/kurtosis)
        3. 收益自相关

        返回 {dsr, p_value, significant, emax_null}
        """
        data = list(self.recent_mcs)[-100:]
        if len(data) < 20:
            return {"dsr": 0.0, "p_value": 1.0, "significant": False,
                    "emax_null": 0.0}
        returns = np.array(data)
        observed_sharpe = self.sharpe_mid
        if math.isnan(observed_sharpe):
            return {"dsr": 0.0, "p_value": 1.0, "significant": False,
                    "emax_null": 0.0}
        try:
            from alpha.calibration import deflated_sharpe_ratio
            result = deflated_sharpe_ratio(
                observed_sr=observed_sharpe,
                returns=returns,
                n_trials=n_trials,
            )
            return result
        except Exception:
            return {"dsr": 0.0, "p_value": 1.0, "significant": False,
                    "emax_null": 0.0}

    # ── CausalCheck (来自 alpha/evaluation/causal_check.py) ──

    def causal_quality(self, factor_values: np.ndarray,
                       forward_returns: np.ndarray) -> dict:
        """检验因子的预测关系是否因果（而非伪相关）。

        返回 {cause_vs_corr_score, orthogonality_pvalue,
               decay_rate, raw_correlation, ...}
        """
        try:
            from alpha.evaluation.causal_check import CausalCheck
            checker = CausalCheck(n_lags=1)
            report = checker.check(factor_values, forward_returns)
            return {
                "cause_vs_corr_score": report.cause_vs_corr_score,
                "orthogonality_pvalue": report.orthogonality_pvalue,
                "decay_rate": report.decay_rate,
                "raw_correlation": report.raw_correlation,
                "early_correlation": report.early_correlation,
                "late_correlation": report.late_correlation,
            }
        except Exception:
            return {
                "cause_vs_corr_score": 0.0,
                "orthogonality_pvalue": 1.0,
                "decay_rate": 0.0,
                "raw_correlation": 0.0,
                "early_correlation": 0.0,
                "late_correlation": 0.0,
            }


# ═══════════════════════════════════════════════════════════
# AttributionEngine
# ═══════════════════════════════════════════════════════════

class AttributionEngine:
    """实盘归因引擎。

    开仓时记录 TradeAttribution, 平仓时计算边际贡献。
    使用线性 MC 近似 (signal_i / Σ|signal_j| × pnl)。
    当因子数 ≥ 3 且样本 ≥ 10 笔时尝试 Gram-Schmidt 正交归因。
    """

    def __init__(self, trade_log_path: str | None = None,
                 stats_snapshot_path: str | None = None):
        self._open_trades: dict[int, TradeAttribution] = {}
        self._per_factor: dict[str, FactorAttributionStats] = {}
        # 检测 pytest 环境 → 自动使用临时路径, 避免污染生产文件
        import os as _os
        if _os.environ.get("PYTEST_CURRENT_TEST"):
            import tempfile as _tf
            self._trade_log_path = trade_log_path or _tf.mktemp(suffix="_factor_trades.jsonl")
            self._stats_snapshot_path = (
                stats_snapshot_path or _tf.mktemp(suffix="_factor_attribution.json")
            )
        else:
            self._trade_log_path = trade_log_path or "data/charts/factor_trades.jsonl"
            self._stats_snapshot_path = (
                stats_snapshot_path or "data/charts/factor_attribution.json"
            )
        # Gram-Schmidt 归因 (lazy load)
        self._orthogonal_attribution = None
        # 逐笔 trade PnL 历史 (用于 Gram-Schmidt Y 向量)
        self._recent_trade_pnls: deque = deque(maxlen=250)
        # 从快照恢复状态
        self._load_stats_snapshot()

    # ── 开仓 ────────────────────────────────────────────

    def record_open(self, position_id: int, attribution: TradeAttribution):
        """开仓时记录归因数据。"""
        self._open_trades[position_id] = attribution
        # 写入 trades.duckdb
        try:
            import duckdb as _duckdb
            _tdb = _duckdb.connect("data/trades.duckdb")
            _tdb.execute("""
                INSERT OR REPLACE INTO trades
                (position_id, symbol, direction, volume,
                 open_ts, open_price, open_reason,
                 composite_score, tactical_score, macro_score,
                 status)
                VALUES (?, 'XAUUSD+', ?, ?,
                        ?, ?, 'signal',
                        ?, ?, ?,
                        'open')
            """, [
                position_id, attribution.direction, attribution.api_volume,
                attribution.open_ts, attribution.open_price,
                attribution.composite_score,
                attribution.tactical_score, attribution.macro_score,
            ])
            open_exec_id = _next_db_id()
            _tdb.execute("""
                INSERT INTO trade_executions
                (id, trade_id, exec_ts, exec_type, price, volume, reason)
                VALUES (?, ?, ?, 'open', ?, ?, 'signal_open')
            """, [open_exec_id, position_id, attribution.open_ts, attribution.open_price, attribution.api_volume])
            _tdb.close()
        except Exception as e:
            logger.warning("Failed to record open trade to DB: %s", e)

    # ── 平仓 ────────────────────────────────────────────

    def record_close(
        self, position_id: int, close_price: float, close_ts: float,
        real_pnl: dict | None = None,
    ) -> dict[str, float]:
        """平仓时计算归因。

        Args:
            position_id: 仓位 ID.
            close_price: 平仓价格 (用于估算, 同时用作降级回退).
            close_ts: 平仓时间戳.
            real_pnl: 可选，cTrader 真实盈亏:
                      {"gross": ..., "swap": ..., "commission": ..., "net": ...}

        Returns:
            {factor_name: marginal_contribution}
        """
        attrib = self._open_trades.pop(position_id, None)
        if attrib is None:
            logger.warning("No attribution for position %d", position_id)
            return {}

        trade_pnl = (close_price - attrib.open_price) * attrib.direction * attrib.api_volume

        # ── 优先使用真实 PnL ──
        actual_pnl = trade_pnl
        if real_pnl and real_pnl.get("net") is not None:
            actual_pnl = real_pnl["net"]

        # ── 尝试 Gram-Schmidt 正交归因 ──
        marginal_contributions = self._orthogonal_close(attrib, trade_pnl)

        # ── 回退到线性 MC 近似 ──
        if marginal_contributions is None:
            marginal_contributions = self._linear_mc_close(attrib, trade_pnl)

        # ── 更新滚动统计 (使用真实 PnL 判断盈亏) ──
        for name, mc in marginal_contributions.items():
            if name not in self._per_factor:
                self._per_factor[name] = FactorAttributionStats(name)
            self._per_factor[name].record(
                mc, actual_pnl > 0, attrib.tags_breakdown,
                real_pnl=real_pnl,
            )

        # ── 记录 trade PnL (用于后续 Gram-Schmidt Y 向量) ──
        self._recent_trade_pnls.append(trade_pnl)

        # ── 持久化快照 ──
        self._save_stats_snapshot()

        # ── 写入逐笔明细 ──
        self._write_trade_log(
            position_id, attrib, close_price, close_ts,
            marginal_contributions, trade_pnl, actual_pnl, real_pnl,
        )

        return marginal_contributions

    # ── 查询 ────────────────────────────────────────────

    def get_factor_stats(self, name: str) -> FactorAttributionStats | None:
        return self._per_factor.get(name)

    def get_all_factor_stats(self) -> dict[str, FactorAttributionStats]:
        return dict(self._per_factor)

    # ── 归因方法 ────────────────────────────────────────

    def _linear_mc_close(
        self, attrib: TradeAttribution, trade_pnl: float,
    ) -> dict[str, float]:
        """线性 MC 回退: signal_i / Σ|signal_j| × pnl"""
        total_abs_signal = attrib.total_signal_abs
        if abs(total_abs_signal) < 1e-10:
            return {}
        return {
            name: round((signal / total_abs_signal) * trade_pnl, 6)
            for name, signal in attrib.factor_signals.items()
            if signal is not None and abs(signal) >= 1e-10
        }

    def _orthogonal_close(
        self, attrib: TradeAttribution, trade_pnl: float,
    ) -> dict[str, float] | None:
        """Gram-Schmidt 正交归因 (主方法)。

        条件: 因子数 ≥ 3 且全局样本 ≥ 10 笔。
        返回 None 表示应回退到线性 MC。
        """
        active_factors = [
            n for n, s in attrib.factor_signals.items()
            if s is not None and abs(s) >= 1e-10
        ]
        if len(active_factors) < 3:
            return None  # 因子太少

        # 检查各因子的样本量
        factor_samples = [
            len(self._per_factor.get(n, FactorAttributionStats(n)).recent_mcs)
            for n in active_factors
        ]
        if min(factor_samples) < 10:
            return None  # 样本不足

        try:
            from alpha.evaluation.attribution import Attribution

            if self._orthogonal_attribution is None:
                self._orthogonal_attribution = Attribution(demean=True)

            # 构建因子值矩阵
            factor_matrix = []
            # 取最近 50 笔
            n_samples = min(50, min(factor_samples))
            for n in active_factors:
                stats = self._per_factor.get(n)
                mcs = list(stats.recent_mcs)[-n_samples:] if stats else []
                factor_matrix.append(mcs)

            # Y 向量: 对应 trade 的真实 PnL (不是某个因子的 MC!)
            pnl_series = list(self._recent_trade_pnls)[-n_samples:]
            if len(pnl_series) < 10:
                return None

            factor_matrix = np.array(factor_matrix).T  # (n_samples × n_factors)
            if factor_matrix.shape[0] < 10 or factor_matrix.shape[1] < 3:
                return None

            report = self._orthogonal_attribution.attribute(
                factor_matrix, np.array(pnl_series),
                factor_names=active_factors,
            )
            if report is None:
                return None

            total_r2 = max(report.total_r2, 1e-10)
            return {
                c.name: round(c.marginal_r2 / total_r2 * trade_pnl, 6)
                for c in report.contributions
                if c.name in attrib.factor_signals and c.marginal_r2 > 0
            }
        except Exception as e:
            logger.debug("Gram-Schmidt attribution failed: %s, falling back to linear MC", e)
            return None

    # ── 日志 ────────────────────────────────────────────

    def _write_trade_log(
        self, position_id: int, attrib: TradeAttribution,
        close_price: float, close_ts: float,
        marginal_contributions: dict[str, float],
        trade_pnl: float,
        actual_pnl: float | None = None,
        real_pnl: dict | None = None,
    ):
        """追加一行逐笔归因明细到 JSONL 和 trades.duckdb。

        Args:
            ...
            actual_pnl: 实际使用的 PnL (优先 real_pnl, 降级估算).
            real_pnl: cTrader 原始盈亏 {"gross", "swap", "commission", "net"}.
        """
        try:
            entry = {
                "position_id": position_id,
                "open_ts": attrib.open_ts,
                "close_ts": close_ts,
                "open_price": attrib.open_price,
                "close_price": close_price,
                "direction": attrib.direction,
                "trade_pnl": round(trade_pnl, 6),
                "actual_pnl": round(actual_pnl, 6) if actual_pnl is not None else round(trade_pnl, 6),
                "api_volume": attrib.api_volume,
                "composite_score": attrib.composite_score,
                "tactical_score": attrib.tactical_score,
                "macro_score": attrib.macro_score,
                "marginal_contributions": marginal_contributions,
                "factor_signals": attrib.factor_signals,
                "active_weights": attrib.active_weights,
                "tags_breakdown": attrib.tags_breakdown,
                "real_gross": round(real_pnl.get("gross", 0), 6) if real_pnl else None,
                "real_swap": round(real_pnl.get("swap", 0), 6) if real_pnl else None,
                "real_commission": round(real_pnl.get("commission", 0), 6) if real_pnl else None,
                "real_net": round(real_pnl.get("net", 0), 6) if real_pnl else None,
                "real_balance": round(real_pnl.get("balance", 0), 2) if real_pnl else None,
                "deal_id": real_pnl.get("deal_id") if real_pnl else None,
            }
            # JSONL 日志 (保留)
            path = Path(self._trade_log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning("Failed to write trade log: %s", e)

        # ── 写入 trades.duckdb ──
        try:
            import duckdb as _duckdb
            _tdb = _duckdb.connect("data/trades.duckdb")
            # 更新 trade 主表
            pnl_pct = round(trade_pnl / attrib.open_price * 100, 4) if attrib.open_price else 0.0
            _tdb.execute("""
                INSERT OR REPLACE INTO trades
                (position_id, symbol, direction, volume,
                 open_ts, open_price, open_reason,
                 close_ts, close_price, trade_pnl, pnl_pct,
                 composite_score, tactical_score, macro_score,
                 status, updated_at)
                VALUES (?, 'XAUUSD+', ?, ?,
                        ?, ?, 'signal',
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        'closed', EXTRACT(EPOCH FROM CURRENT_TIMESTAMP))
            """, [
                position_id, attrib.direction, attrib.api_volume,
                attrib.open_ts, attrib.open_price,
                close_ts, close_price, round(trade_pnl, 6), pnl_pct,
                attrib.composite_score, attrib.tactical_score, attrib.macro_score,
            ])
            trade_write_base = _next_db_id()
            # 写入因子归因明细
            for idx, (fname, mc) in enumerate(marginal_contributions.items()):
                _tdb.execute("""
                    INSERT INTO trade_factor_attributions
                    (id, trade_id, factor_name, marginal_contribution)
                    VALUES (?, ?, ?, ?)
                """, [trade_write_base + idx + 1, position_id, fname, round(mc, 6)])

            close_exec_id = trade_write_base + 999
            _tdb.execute("""
                INSERT INTO trade_executions
                (id, trade_id, exec_ts, exec_type, price, volume, reason)
                VALUES (?, ?, ?, 'close', ?, ?, 'signal_close')
            """, [close_exec_id, position_id, close_ts, close_price, attrib.api_volume])
            _tdb.close()
        except Exception as e:
            logger.warning("Failed to write trade to DB: %s", e)

    # ── 持久化: 归因快照 (原子写入 → factor_attribution.json) ──

    def _save_stats_snapshot(self):
        """每笔交易后原子写入因子统计快照。

        格式匹配设计文档 §13.1，用于重启后恢复滚动窗口。
        """
        if not self._per_factor:
            return
        try:
            snapshot = {}
            for name, s in self._per_factor.items():
                if s.n_trades < 3:
                    continue  # 跳过样本太少因子，避免极端 Sharpe
                snapshot[name] = {
                    "n_trades": s.n_trades,
                    "n_voted": s.n_voted,
                    "wins": s.wins,
                    "total_mc": round(s.total_mc, 6),
                    "avg_mc": round(s.avg_mc, 6),
                    "recent_mcs": [round(x, 6) for x in list(s.recent_mcs)],
                    "recent_pnl_directions": list(s.recent_pnl_directions),
                    "composite_sharpe_score": round(s.composite_sharpe_score, 4),
                    "ir_short": round(s.ir_short, 4) if not math.isnan(s.ir_short) else None,
                    "ir_mid": round(s.ir_mid, 4) if not math.isnan(s.ir_mid) else None,
                    "ir_long": round(s.ir_long, 4) if not math.isnan(s.ir_long) else None,
                    # ── 真实 PnL ──
                    "total_gross": round(s.total_gross, 6),
                    "total_swap": round(s.total_swap, 6),
                    "total_commission": round(s.total_commission, 6),
                    "total_net_pnl": round(s.total_net_pnl, 6),
                    "avg_gross": round(s.total_gross / s.n_voted, 6) if s.n_voted > 0 else 0,
                    "avg_net": round(s.total_net_pnl / s.n_voted, 6) if s.n_voted > 0 else 0,
                }
            # ★ 写入 state.db (唯一持久化路径)
            if not _os.environ.get("PYTEST_CURRENT_TEST"):
                try:
                    from backend.core.db import get_state_conn
                    conn = get_state_conn()
                    try:
                        import time as _t
                        now = _t.time()
                        for name, s in snapshot.items():
                            conn.execute(
                                "INSERT OR REPLACE INTO attribution_snapshot (factor, data_json, updated_at) VALUES (?, ?, ?)",
                                (name, json.dumps(s, ensure_ascii=False, default=str), now)
                            )
                        conn.commit()
                    finally:
                        conn.close()
                except Exception as _sdb_err:
                    logger.warning("Failed to write attribution_snapshot to state.db: %s", _sdb_err)
        except Exception as e:
            logger.warning("Failed to save stats snapshot: %s", e)

    # ── 逐日盯市归因 (Phase 1) ──────────────────────────

    def mark_to_market(
        self,
        current_factors: dict[str, float],
        current_price: float,
    ) -> dict:
        """对当前持仓做逐日盯市归因。

        Args:
            current_factors: 当前 bar 的因子值 {name: value}
            current_price: 当前价格

        Returns:
            {
                "per_factor": {name: unrealized_pnl},
                "per_tag": {tag: unrealized_pnl},
                "total_unrealized": float,
                "n_open_positions": int,
            }
        """
        from typing import Any as _Any
        result: dict[str, _Any] = {
            "per_factor": {},
            "per_tag": {},
            "total_unrealized": 0.0,
            "n_open_positions": len(self._open_trades),
        }
        if not self._open_trades:
            return result

        for _pid, attrib in self._open_trades.items():
            unrealized = (current_price - attrib.open_price) * attrib.direction
            result["total_unrealized"] += unrealized

            # 线性 MC: signal_i / Σ|signal_j| × unrealized
            total_abs = attrib.total_signal_abs
            if total_abs <= 0:
                continue

            for name, signal_val in attrib.factor_signals.items():
                mc = (signal_val / total_abs) * unrealized
                result["per_factor"][name] = (
                    result["per_factor"].get(name, 0.0) + mc
                )

            # 按 tag 聚合 (简化版: 均匀分配)
            tags = attrib.tags_breakdown
            if tags:
                per_tag_share = unrealized / len(tags)
                for tag in tags:
                    result["per_tag"][tag] = (
                        result["per_tag"].get(tag, 0.0) + per_tag_share
                    )

        # Round values
        result["total_unrealized"] = round(result["total_unrealized"], 2)
        result["per_factor"] = {
            k: round(v, 2) for k, v in result["per_factor"].items()
        }
        result["per_tag"] = {
            k: round(v, 2) for k, v in result["per_tag"].items()
        }
        return result

    # ── 加载快照 ──────────────────────────────────────────

    def _load_stats_snapshot(self):
        """从 state.db (首选) 或 JSON 文件恢复因子统计 (启动时调用)。"""
        data: dict[str, Any] = {}

        # 尝试从 state.db 恢复 (更可靠, 事务保护)
        if not _os.environ.get("PYTEST_CURRENT_TEST"):
            try:
                from backend.core.db import get_state_conn
                conn = get_state_conn()
                try:
                    rows = conn.execute(
                        "SELECT factor, data_json FROM attribution_snapshot"
                    ).fetchall()
                    for r in rows:
                        try:
                            data[r["factor"]] = json.loads(r["data_json"])
                        except (json.JSONDecodeError, TypeError):
                            continue
                finally:
                    conn.close()
            except Exception as e:
                logger.warning("Failed to load stats from state.db: %s", e)

        if not data:
            return

        for name, d in data.items():
            stats = FactorAttributionStats(name=name)
            stats.n_trades = d.get("n_trades", 0)
            stats.n_voted = d.get("n_voted", 0)
            stats.wins = d.get("wins", 0)
            stats.total_mc = d.get("total_mc", 0.0)
            # ── 恢复真实 PnL ──
            stats.total_gross = d.get("total_gross", 0.0)
            stats.total_swap = d.get("total_swap", 0.0)
            stats.total_commission = d.get("total_commission", 0.0)
            stats.total_net_pnl = d.get("total_net_pnl", 0.0)
            # 恢复 deque
            mcs = d.get("recent_mcs", [])
            if mcs:
                stats.recent_mcs.extend(mcs)
            dirs = d.get("recent_pnl_directions", [])
            if dirs:
                stats.recent_pnl_directions.extend(dirs)
            self._per_factor[name] = stats
        logger.info(
            "Restored %d factor stats from state.db", len(data),
        )
