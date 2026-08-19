"""
execution/analytics.py — 执行质量分析器 (Phase 4)

记录每笔成交的滑点、延迟、市场冲击，输出统计报告。
接入 live_service 的成交回调。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


def _latency_ms(signal_time: float, fill_time: float) -> float | None:
    """Return comparable wall-clock latency and reject mixed/stale timestamps."""
    if signal_time <= 0 or fill_time <= 0:
        return None
    latency = (fill_time - signal_time) * 1000.0
    if latency < 0 or latency > 300_000.0:
        return None
    return latency


@dataclass
class TradeExecution:
    """单笔成交的执行质量记录"""
    # 时间戳 (epoch seconds)
    signal_time: float = 0.0      # 信号生成时间
    submit_time: float = 0.0      # 提交到 bridge 时间
    fill_time: float = 0.0        # 收到成交回执时间

    # 价格
    signal_price: float = 0.0     # 信号触发时的参考价
    fill_price: float = 0.0       # 实际成交价
    bar_vwap: float = 0.0         # 同 bar VWAP (如果可用)

    # 订单信息
    symbol: str = ""
    direction: int = 0            # 1=buy, -1=sell
    volume: float = 0.0
    order_id: int = 0
    algo: str = ""                # 算法名 (TWAP/VWAP/...), 空=市价单


class ExecutionQuality:
    """执行质量分析器

    维护最近 N 笔成交的滚动窗口，计算:
    - avg_slippage_bps: 平均滑点 (基点)
    - avg_latency_ms: 平均延迟 (毫秒)
    - slippage_std: 滑点标准差
    - slippage_p95: 滑点 95 分位数
    - market_impact_bps: 成交价 vs VWAP 的偏差
    - fill_rate: 成交率

    用法:
        eq = ExecutionQuality(max_records=500)
        eq.record(TradeExecution(...))
        report = eq.report()
    """

    def __init__(self, max_records: int = 500):
        self._records: deque[TradeExecution] = deque(maxlen=max_records)
        self._total_submitted = 0
        self._total_filled = 0
        self._total_rejected = 0

    # ── 记录 ──

    def record(self, trade: TradeExecution):
        """记录一笔成交"""
        self._records.append(trade)
        self._total_submitted += 1
        if trade.fill_price > 0:
            self._total_filled += 1
        else:
            self._total_rejected += 1

        # 计算延迟 (毫秒)
        latency_ms = _latency_ms(trade.signal_time, trade.fill_time)
        slippage_bps = None
        if trade.signal_price > 0 and trade.fill_price > 0:
            raw_slip = (trade.fill_price - trade.signal_price) / trade.signal_price * 10000
            # 买入: fill > signal = 正滑点 (不利), 卖出: fill < signal = 正滑点 (不利)
            if trade.direction == -1:
                raw_slip = -raw_slip
            slippage_bps = raw_slip

        logger.debug(
            f"[ExecQuality] {trade.symbol} {trade.direction} "
            f"order={trade.order_id} vol={trade.volume:.4f} "
            f"signal={trade.signal_price:.2f} fill={trade.fill_price:.2f} "
            f"slip={slippage_bps or 0:.1f}bps "
            f"lat={f'{latency_ms:.0f}ms' if latency_ms is not None else 'n/a'}"
        )

    def record_rejected(self):
        """记录一笔拒单 (无成交)"""
        self._total_submitted += 1
        self._total_rejected += 1

    # ── 报告 ──

    def report(self) -> dict:
        """生成执行质量报告"""
        records = list(self._records)
        filled = [r for r in records if r.fill_price > 0]

        if not filled:
            return {
                "n_records": len(records),
                "n_filled": 0,
                "fill_rate": 0.0,
                "avg_slippage_bps": 0.0,
                "avg_latency_ms": 0.0,
                "note": "no filled trades",
            }

        # 滑点 (bps)
        slippages = []
        for r in filled:
            if r.signal_price > 0:
                raw = (r.fill_price - r.signal_price) / r.signal_price * 10000
                if r.direction == -1:
                    raw = -raw
                slippages.append(raw)

        # 延迟 (ms)
        latencies = []
        for r in filled:
            latency = _latency_ms(r.signal_time, r.fill_time)
            if latency is not None:
                latencies.append(latency)

        # 市场冲击: fill vs VWAP
        impacts = []
        for r in filled:
            if r.bar_vwap > 0:
                imp = (r.fill_price - r.bar_vwap) / r.bar_vwap * 10000
                if r.direction == -1:
                    imp = -imp
                impacts.append(imp)

        result = {
            "n_records": len(records),
            "n_filled": len(filled),
            "n_rejected": self._total_rejected,
            "n_submitted": self._total_submitted,
            "fill_rate": self._total_filled / max(self._total_submitted, 1),
        }

        if slippages:
            arr = np.array(slippages)
            result["avg_slippage_bps"] = float(np.mean(arr))
            result["slippage_std_bps"] = float(np.std(arr))
            result["slippage_p95_bps"] = float(np.percentile(np.abs(arr), 95))
            result["slippage_max_bps"] = float(np.max(np.abs(arr)))
            result["n_slippage_samples"] = len(arr)

        if latencies:
            arr = np.array(latencies)
            result["avg_latency_ms"] = float(np.mean(arr))
            result["latency_p95_ms"] = float(np.percentile(arr, 95))
            result["latency_max_ms"] = float(np.max(arr))
            result["n_latency_samples"] = len(arr)

        if impacts:
            arr = np.array(impacts)
            result["avg_market_impact_bps"] = float(np.mean(arr))
            result["impact_std_bps"] = float(np.std(arr))
            result["n_impact_samples"] = len(arr)
        else:
            result["avg_market_impact_bps"] = 0.0

        return result

    def summary(self) -> str:
        """单行摘要 (适合日志)"""
        r = self.report()
        slip = r.get("avg_slippage_bps", 0.0)
        lat = r.get("avg_latency_ms", 0.0)
        rate = r.get("fill_rate", 0.0)
        return (
            f"[ExecQuality] n={r['n_filled']} "
            f"slip={slip:.1f}bps lat={lat:.0f}ms "
            f"fill={rate:.1%}"
        )

    # ── 按算法分组 ──

    def report_by_algo(self) -> dict[str, dict]:
        """按算法分组统计"""
        records = list(self._records)
        algos: dict[str, list[TradeExecution]] = {}
        for r in records:
            key = r.algo or "market"
            algos.setdefault(key, []).append(r)

        result = {}
        for algo, trades in algos.items():
            result[algo] = {
                "n_trades": len(trades),
                "total_volume": sum(t.volume for t in trades),
            }
            filled = [t for t in trades if t.fill_price > 0]
            if filled and filled[0].signal_price > 0:
                slips = []
                for t in filled:
                    if t.signal_price > 0:
                        raw = (t.fill_price - t.signal_price) / t.signal_price * 10000
                        if t.direction == -1:
                            raw = -raw
                        slips.append(raw)
                if slips:
                    result[algo]["avg_slippage_bps"] = float(np.mean(slips))
        return result

    def clear(self):
        """清空历史记录"""
        self._records.clear()
        self._total_submitted = 0
        self._total_filled = 0
        self._total_rejected = 0
