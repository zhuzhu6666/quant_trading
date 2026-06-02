"""
live/meta_learner_monitor.py
============================

P0-7 元学习监控 — 模型预测 vs 实际 漂移检测:

核心: 模型预测概率 p_pred vs 实际结果 y_true 的偏差监控
  1. 预测校准 (calibration): 当 p_pred=0.7, 实际胜率应 ≈ 70%
  2. 漂移告警: 实际胜率 - 预测胜率 持续偏低 > 阈值 → 触发降权/重训
  3. 频率监控: 模型预测置信度分布 (0-1 区间直方图)
  4. 多模型对比: 同时跟踪多个模型 (xgb / logreg / baseline), 找最优

使用:
  - P0-5/P0-6 OOS 预测作为输入 (data/charts/walkforward_oos.npy)
  - 模拟 live: 加载模型 + 持续喂新 bar, 监控漂移
  - 输出: status JSON + 告警 (调 alert_callback)
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
class MetaLearnerStatus:
    """模型监控状态"""
    model_name: str
    n_obs: int
    avg_pred_prob: float           # 平均预测概率
    actual_win_rate: float         # 实际胜率 (y=1 占比)
    calibration_gap: float         # 校准误差 (avg_pred - actual_win_rate)
    drift_status: str              # 'OK' | 'DRIFT' | 'SEVERE_DRIFT'
    last_update_ts: float = 0.0

    def to_dict(self) -> dict:
        return {
            "model_name": self.model_name,
            "n_obs": self.n_obs,
            "avg_pred_prob": round(self.avg_pred_prob, 4),
            "actual_win_rate": round(self.actual_win_rate, 4),
            "calibration_gap": round(self.calibration_gap, 4),
            "drift_status": self.drift_status,
            "last_update_ts": self.last_update_ts,
        }


@dataclass
class MetaLearnerMonitor:
    """
    多模型元学习监控器。

    用法:
        monitor = MetaLearnerMonitor(
            model_names=["xgboost", "logreg", "baseline"],
            window=500,
            drift_threshold=0.05,
            severe_threshold=0.10,
        )
        # 每根 OOS bar 调:
        monitor.on_observation("xgboost", y_pred_prob, y_true)
        # 查状态:
        status = monitor.status()
    """
    model_names: list[str]
    window: int = 500
    drift_threshold: float = 0.05     # 校准误差 > 5% → DRIFT
    severe_threshold: float = 0.10    # > 10% → SEVERE_DRIFT
    min_obs: int = 50                 # 至少 50 obs 才报
    alert_callback: Callable | None = None

    # {model_name: deque of (pred_prob, y_true, bar_ts)}
    _history: dict[str, deque] = field(default_factory=dict)
    _last_alert_ts: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        for n in self.model_names:
            self._history[n] = deque(maxlen=self.window * 5)
            self._last_alert_ts[n] = 0

    def on_observation(self, model_name: str, pred_prob: float, y_true: int,
                       bar_ts: float | None = None) -> None:
        """每根 bar 调一次, 推入预测 + 实际结果"""
        if bar_ts is None:
            bar_ts = time.time()
        if model_name not in self._history:
            self._history[model_name] = deque(maxlen=self.window * 5)
            self._last_alert_ts[model_name] = 0
        if pred_prob is None or np.isnan(pred_prob) or y_true is None:
            return
        self._history[model_name].append((float(pred_prob), int(y_true), bar_ts))
        self._check_drift(model_name, bar_ts)

    def _check_drift(self, model_name: str, bar_ts: float):
        h = self._history[model_name]
        if len(h) < self.min_obs:
            return
        recent = list(h)[-self.window:]
        probs = np.array([x[0] for x in recent])
        ys = np.array([x[1] for x in recent])
        gap = float(np.mean(probs) - np.mean(ys))
        if abs(gap) >= self.severe_threshold:
            level = "SEVERE_DRIFT"
        elif abs(gap) >= self.drift_threshold:
            level = "DRIFT"
        else:
            return  # OK, 不告警
        # 节流: 同一 level 1 小时内不重复
        if bar_ts - self._last_alert_ts[model_name] > 3600:
            msg = (f"[{level}] {model_name} 校准误差 {gap:+.4f} "
                   f"(预测均值 {np.mean(probs):.4f}, 实际胜率 {np.mean(ys):.4f}, "
                   f"n={len(recent)})")
            logger.warning(msg)
            self._last_alert_ts[model_name] = bar_ts
            if self.alert_callback:
                self.alert_callback(model_name, level, gap, msg)

    def status(self) -> list[MetaLearnerStatus]:
        result = []
        for name in self.model_names:
            h = self._history.get(name, [])
            n = len(h)
            if n < self.min_obs:
                result.append(MetaLearnerStatus(
                    model_name=name, n_obs=n,
                    avg_pred_prob=0.0, actual_win_rate=0.0,
                    calibration_gap=0.0, drift_status="WARMING_UP",
                ))
                continue
            recent = list(h)[-self.window:]
            probs = np.array([x[0] for x in recent])
            ys = np.array([x[1] for x in recent])
            avg_p = float(np.mean(probs))
            avg_y = float(np.mean(ys))
            gap = avg_p - avg_y
            if abs(gap) >= self.severe_threshold:
                level = "SEVERE_DRIFT"
            elif abs(gap) >= self.drift_threshold:
                level = "DRIFT"
            else:
                level = "OK"
            result.append(MetaLearnerStatus(
                model_name=name, n_obs=n,
                avg_pred_prob=avg_p, actual_win_rate=avg_y,
                calibration_gap=gap, drift_status=level,
                last_update_ts=h[-1][2],
            ))
        return result

    def to_json(self) -> str:
        return json.dumps([s.to_dict() for s in self.status()], indent=2, ensure_ascii=False)

    def calibration_table(self, model_name: str, n_bins: int = 10) -> list[dict]:
        """分桶校准: 把 [0,1] 分 n_bins, 看每桶的 (avg_pred, actual_win_rate, count)"""
        h = self._history.get(model_name, [])
        if len(h) < self.min_obs:
            return []
        recent = list(h)[-self.window:]
        probs = np.array([x[0] for x in recent])
        ys = np.array([x[1] for x in recent])
        bins = np.linspace(0, 1, n_bins + 1)
        table = []
        for i in range(n_bins):
            lo, hi = bins[i], bins[i + 1]
            if i == n_bins - 1:
                mask = (probs >= lo) & (probs <= hi)
            else:
                mask = (probs >= lo) & (probs < hi)
            n = int(mask.sum())
            if n == 0:
                continue
            table.append({
                "bin": f"[{lo:.1f}, {hi:.1f})",
                "n": n,
                "avg_pred": round(float(probs[mask].mean()), 4),
                "actual_wr": round(float(ys[mask].mean()), 4),
            })
        return table


# ── CLI 测试入口 ───────────────────────────────────────────────

def _main():
    """从 P0-6 walkforward_oos.npy 加载预测, 模拟元学习监控"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=str, default="data/charts/walkforward_oos.npy")
    parser.add_argument("--n-sim", type=int, default=0,
                        help="模拟额外新 bar 数 (0=只回放 OOS)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)

    inp = Path(args.input)
    if not inp.exists():
        print(f"  ⚠ {inp} 不存在, 请先跑 walkforward_p0_6.py")
        return
    data = np.load(inp, allow_pickle=True).item()
    y = data["y"]
    prob = data["prob"]
    dates = data["dates"]
    print(f"Loaded OOS predictions: n={len(y)} bar")

    # 1) 监控 XGBoost (主模型) + logreg (基线) + baseline (always majority)
    monitor = MetaLearnerMonitor(
        model_names=["xgboost", "logreg", "baseline"],
        window=500,
        drift_threshold=0.05,
        severe_threshold=0.10,
    )

    # 2) 加载 P0-5 的 logreg 概率 (用 .npy 里的字段)
    prob_lr = data.get("prob", None)  # 实际 P0-5 .npy 含 y_prob_lr
    # 如果 walkforward OOS 没 logreg 概率, 构造一个 baseline 概率
    if "y_prob_lr" in data:
        prob_lr = data["y_prob_lr"]
    else:
        # 退化: baseline = 0.5 (随机)
        prob_lr = np.full_like(prob, 0.5)
    baseline_prob = np.full_like(prob, 0.5)  # always predict 0.5

    # 3) 回放 OOS
    print(f"\n--- 回放 {len(y)} OOS 预测到 monitor ---")
    ts_base = time.time()
    for i in range(len(y)):
        bar_ts = ts_base + i * 900  # 模拟 M15 间距
        monitor.on_observation("xgboost", float(prob[i]), int(y[i]), bar_ts)
        monitor.on_observation("logreg", float(prob_lr[i]), int(y[i]), bar_ts)
        monitor.on_observation("baseline", float(baseline_prob[i]), int(y[i]), bar_ts)

    # 4) 状态
    print("\n" + "=" * 60)
    print("Meta-Learner Status (回放 OOS 后):")
    print("=" * 60)
    print(monitor.to_json())

    # 5) 校准表 (XGBoost)
    print("\n" + "=" * 60)
    print("XGBoost 校准表 (10 桶):")
    print("=" * 60)
    print(f"  {'Bin':<14s}  {'n':>5s}  {'avg_pred':>10s}  {'actual_wr':>10s}  {'gap':>8s}")
    for row in monitor.calibration_table("xgboost"):
        gap = row["avg_pred"] - row["actual_wr"]
        print(f"  {row['bin']:<14s}  {row['n']:>5d}  {row['avg_pred']:>10.4f}  "
              f"{row['actual_wr']:>10.4f}  {gap:>+8.4f}")

    # 6) 落盘
    out = Path("data/charts/meta_learner_report.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("P0-7 Meta-Learner 监控报告\n")
        f.write(f"输入: {inp}\n")
        f.write(f"OOS 样本: {len(y)} bar\n\n")
        f.write("== Status ==\n")
        for s in monitor.status():
            f.write(f"  {s.model_name}: n={s.n_obs}  avg_pred={s.avg_pred_prob:.4f}  "
                    f"actual_wr={s.actual_win_rate:.4f}  gap={s.calibration_gap:+.4f}  "
                    f"[{s.drift_status}]\n")
        f.write("\n== XGBoost 校准 ==\n")
        for row in monitor.calibration_table("xgboost"):
            f.write(f"  {row['bin']}  n={row['n']}  avg_pred={row['avg_pred']:.4f}  "
                    f"actual_wr={row['actual_wr']:.4f}\n")
    print(f"\n→ 落盘: {out}")


if __name__ == "__main__":
    _main()
