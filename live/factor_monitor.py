"""
live/factor_monitor.py
======================

P0-4 因子 IC 实时监控 (live 接入层):
  - 流式接收 bar 数据 (模拟 live tick)
  - 增量更新 ICTracker
  - 定期 (每 N bar 或 N 秒) 输出当前状态
  - 触发告警 (rolling IC 持续负向) → 钉钉/控制台/日志

与 paper 链路的接口:
  paper 跑时, 每根 bar 调一次 monitor.on_bar(bar_dict)
  monitor 内部维护 ICTracker + last_status, 可被 paper 调 query_status()

与回测的接口:
  scripts/factor_ic_rolling.py 离线算的 IC 时间序列 → 落盘 .npy
  本模块启动时可加载 .npy 作为 warmup, 避免冷启动
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class FactorStatus:
    """单个因子的实时状态"""
    name: str
    rolling_ic: float
    n_obs: int
    status: str  # 'ACTIVE' | 'fading' | 'DEAD' | 'REGIME_SHIFT'
    last_update_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "rolling_ic": self.rolling_ic,
            "n_obs": self.n_obs,
            "status": self.status,
            "last_update_ts": self.last_update_ts,
        }


@dataclass
class FactorMonitor:
    """
    实时因子 IC 监控器。

    用法:
        monitor = FactorMonitor(
            factor_names=["dxy_corr_20", "macd_hist", ...],
            window=500,
            regime_threshold=0.005,
            regime_min_days=10,
        )
        # paper / live 跑时每根 bar 调:
        monitor.on_bar(factor_values_dict, forward_return)
        # 查询当前状态:
        status = monitor.status()
    """
    factor_names: list[str]
    window: int = 500
    regime_threshold: float = 0.005
    regime_min_bars: int = 96 * 10  # 10 天 (M15)
    alert_callback: Callable | None = None  # 告警时调用

    # 内部: {factor_name: deque of (val, ret)}
    _history: dict[str, deque] = field(default_factory=dict)
    # 内部: {factor_name: list of bar_ts at which alert fired}
    _alerts: dict[str, list[float]] = field(default_factory=dict)

    def __post_init__(self):
        for n in self.factor_names:
            self._history[n] = deque(maxlen=self.window * 5)  # 留余量
            self._alerts[n] = []

    def on_bar(self, factor_values: dict[str, float], forward_return: float,
               bar_ts: float | None = None) -> None:
        """
        每根 bar 调一次, 推入 ICTracker.

        Args:
            factor_values: {factor_name: value_at_this_bar}
            forward_return: 该 bar 的 (close_{t+1} - close_t) / close_t
                           (实时拿不到, 用 0 占位, 实际 1 bar 滞后)
            bar_ts: bar 时间戳 (epoch seconds), None 用 wall clock
        """
        if bar_ts is None:
            bar_ts = time.time()
        for name, val in factor_values.items():
            if name not in self._history:
                self._history[name] = deque(maxlen=self.window * 5)
                self._alerts[name] = []
            if val is None or np.isnan(val) or np.isnan(forward_return):
                continue
            self._history[name].append((float(val), float(forward_return), bar_ts))

        # 检查告警
        self._check_alerts(bar_ts)

    def _rolling_ic(self, name: str) -> float:
        h = self._history.get(name)
        if not h or len(h) < 30:
            return 0.0
        # 取最近 window 个
        recent = list(h)[-self.window:]
        vals = np.array([x[0] for x in recent])
        rets = np.array([x[1] for x in recent])
        mask = ~(np.isnan(vals) | np.isnan(rets))
        if mask.sum() < 10:
            return 0.0
        if vals[mask].std() < 1e-12 or rets[mask].std() < 1e-12:
            return 0.0
        return float(np.corrcoef(vals[mask], rets[mask])[0, 1])

    def _check_alerts(self, bar_ts: float):
        """检查每个因子是否触发 regime shift 告警"""
        for name in self.factor_names:
            h = self._history.get(name)
            if not h or len(h) < 50:
                continue
            # 取最近 regime_min_bars 个, 看是否持续负向
            recent = list(h)[-self.regime_min_bars:]
            vals = np.array([x[0] for x in recent])
            rets = np.array([x[1] for x in recent])
            mask = ~(np.isnan(vals) | np.isnan(rets))
            if mask.sum() < 30:
                continue
            rolling_ic_short = float(np.corrcoef(vals[mask], rets[mask])[0, 1])
            if rolling_ic_short < -self.regime_threshold:
                # 触发告警 (但要避免重复: 1 天内不重复)
                last_alert = self._alerts[name][-1] if self._alerts[name] else 0
                if bar_ts - last_alert > 86400:  # 24h 节流
                    msg = (f"[REGIME_SHIFT] {name} rolling IC={rolling_ic_short:+.4f} "
                           f"< -{self.regime_threshold} 持续 ≥ {self.regime_min_bars/96:.0f} 天")
                    logger.warning(msg)
                    self._alerts[name].append(bar_ts)
                    if self.alert_callback:
                        self.alert_callback(name, rolling_ic_short, msg)

    def status(self) -> list[FactorStatus]:
        """当前所有因子的状态"""
        result = []
        for name in self.factor_names:
            ic = self._rolling_ic(name)
            n = len(self._history.get(name, []))
            if abs(ic) >= 0.02:
                status = "ACTIVE"
            elif abs(ic) >= 0.01:
                status = "fading"
            else:
                status = "DEAD"
            # 追加 regime_shift 标志
            if n >= self.regime_min_bars and ic < -self.regime_threshold:
                status = "REGIME_SHIFT"
            result.append(FactorStatus(
                name=name,
                rolling_ic=round(ic, 4),
                n_obs=n,
                status=status,
                last_update_ts=self._history[name][-1][2] if self._history[name] else 0,
            ))
        return result

    def to_json(self) -> str:
        return json.dumps([s.to_dict() for s in self.status()], indent=2, ensure_ascii=False)


# ── CLI 测试入口 ───────────────────────────────────────────────

def _main():
    """模拟接 live: 加载离线 IC rolling 结果, 模拟新 bar 流入"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup", type=str, default="data/charts/factor_ic_rolling.npy",
                        help="离线 warmup 数据 (.npy)")
    parser.add_argument("--n-sim", type=int, default=200,
                        help="模拟新 bar 数")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    factors = ["dxy_corr_20", "macd_hist", "bb_width", "di_spread"]
    monitor = FactorMonitor(
        factor_names=factors,
        window=500,
        regime_threshold=0.005,
        regime_min_bars=96 * 10,
    )

    # 1) 从离线 .npy 加载 warmup (用 ic_series 的真实值)
    warmup_path = Path(args.warmup)
    if warmup_path.exists():
        warmup = np.load(warmup_path, allow_pickle=True).item()
        print(f"Loaded warmup from {warmup_path}")
        for name in factors:
            ics = warmup[name]["ic_series"]
            print(f"  {name}: {len(ics)} historical IC points")
    else:
        print(f"(no warmup at {warmup_path}, starting cold)")

    # 2) 模拟新 bar 流入
    print(f"\n--- 模拟 {args.n_sim} 根新 bar ---")
    rng = np.random.default_rng(42)
    for i in range(args.n_sim):
        # 随机生成因子值 + 收益 (真实接 live 时替换为真实数据)
        fv = {n: float(rng.normal(0, 1)) for n in factors}
        ret = float(rng.normal(0, 0.001))
        monitor.on_bar(fv, ret, bar_ts=time.time() + i * 900)  # M15 间距
    print("Done.\n")

    # 3) 输出状态
    print("=" * 60)
    print("Final factor status:")
    print("=" * 60)
    print(monitor.to_json())


if __name__ == "__main__":
    _main()
