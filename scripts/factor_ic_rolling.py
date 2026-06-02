"""
scripts/factor_ic_rolling.py
============================

P0-4 因子 IC 滚动监控 (模拟"接 live"流程):
  1. 加载 50K M15 bar
  2. 选 4 个有效因子 (来自 P0-2 PCA 输出): dxy_corr_20 / macd_hist / bb_width / di_spread
  3. 按 96 bar (1 天) 步长滑动, 累计 500 bar 窗口算 rolling IC
  4. 时间序列打印 + 落盘
  5. 衰减告警: rolling IC < threshold 连续 N 天
  6. 输出图表 (matplotlib, 可选) + txt 报告

输出:
  - data/charts/factor_ic_rolling.txt      时间序列 + 告警
  - data/charts/factor_ic_rolling.npy      数值 (供后续读)
  - data/charts/factor_ic_rolling.png      图表 (可选)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.store import DataStore
from data.external_loader import ExternalDataLoader
from alpha.registry import factor_registry
from alpha.factor_engine import FactorEngine


# 来自 P0-2 PCA 输出的有效因子集
SELECTED_FACTORS = ["dxy_corr_20", "macd_hist", "bb_width", "di_spread"]

# Rolling IC 窗口 (500 bar ≈ 5.2 天)
WINDOW = 500
# 步长 (96 bar = 1 天)
STEP = 96
# 告警阈值
DEAD_THRESHOLD = 0.005   # rolling IC 跌破此值连续 5 天 → 标 dead
DECAY_DAYS = 5           # 连续天数


def compute_rolling_ic_series(factor_vals: np.ndarray, fwd_ret: np.ndarray,
                               window: int, step: int) -> tuple[np.ndarray, np.ndarray]:
    """
    在 fwd_ret 上滑动, 每 step 算一次 rolling IC (前 window 个有效 bar).

    Returns:
        (ic_series, anchor_indices)
        anchor_indices[i] 是第 i 个 IC 锚点对应的 bar 索引
    """
    n = len(factor_vals)
    mask = ~(np.isnan(factor_vals) | np.isnan(fwd_ret))
    ics = []
    anchors = []
    for anchor in range(window, n, step):
        # 取 [anchor-window+1, anchor] 窗口
        win_mask = mask[anchor - window + 1: anchor + 1]
        if win_mask.sum() < 50:
            continue
        win_vals = factor_vals[anchor - window + 1: anchor + 1][win_mask]
        win_rets = fwd_ret[anchor - window + 1: anchor + 1][win_mask]
        if win_vals.std() < 1e-12 or win_rets.std() < 1e-12:
            continue
        ic = float(np.corrcoef(win_vals, win_rets)[0, 1])
        ics.append(ic)
        anchors.append(anchor)
    return np.array(ics), np.array(anchors)


def detect_decay(ic_series: np.ndarray, anchor_indices: np.ndarray,
                  threshold: float, decay_days: int,
                  bars_per_day: int = 96) -> list[dict]:
    """检测 rolling IC 连续低于 threshold 的区段。

    Returns: list of {start, end, n_days, mean_ic}
    """
    decays = []
    in_decay = False
    start_idx = 0
    for i, ic in enumerate(ic_series):
        if abs(ic) < threshold:
            if not in_decay:
                in_decay = True
                start_idx = i
        else:
            if in_decay:
                # 区段结束
                end_idx = i - 1
                seg = ic_series[start_idx:end_idx + 1]
                seg_anchors = anchor_indices[start_idx:end_idx + 1]
                n_bars = int(seg_anchors[-1] - seg_anchors[0])
                n_days = n_bars / bars_per_day
                if n_days >= decay_days:
                    decays.append({
                        "start_anchor": int(seg_anchors[0]),
                        "end_anchor": int(seg_anchors[-1]),
                        "n_days": round(n_days, 1),
                        "mean_abs_ic": round(float(np.mean(np.abs(seg))), 4),
                    })
                in_decay = False
    # 末尾段
    if in_decay:
        seg = ic_series[start_idx:]
        seg_anchors = anchor_indices[start_idx:]
        n_bars = int(seg_anchors[-1] - seg_anchors[0])
        n_days = n_bars / bars_per_day
        if n_days >= decay_days:
            decays.append({
                "start_anchor": int(seg_anchors[0]),
                "end_anchor": int(seg_anchors[-1]),
                "n_days": round(n_days, 1),
                "mean_abs_ic": round(float(np.mean(np.abs(seg))), 4),
            })
    return decays


def detect_decay_neg(ic_series: np.ndarray, anchor_indices: np.ndarray,
                     threshold: float, decay_days: int,
                     bars_per_day: int = 96) -> list[dict]:
    """检测 rolling IC 连续低于 -threshold 的区段 (regime shift 检测)。

    Returns: list of {start, end, n_days, mean_ic}
    """
    decays = []
    in_decay = False
    start_idx = 0
    for i, ic in enumerate(ic_series):
        if ic < -threshold:
            if not in_decay:
                in_decay = True
                start_idx = i
        else:
            if in_decay:
                end_idx = i - 1
                seg = ic_series[start_idx:end_idx + 1]
                seg_anchors = anchor_indices[start_idx:end_idx + 1]
                n_bars = int(seg_anchors[-1] - seg_anchors[0])
                n_days = n_bars / bars_per_day
                if n_days >= decay_days:
                    decays.append({
                        "start_anchor": int(seg_anchors[0]),
                        "end_anchor": int(seg_anchors[-1]),
                        "n_days": round(n_days, 1),
                        "mean_ic": round(float(np.mean(seg)), 4),
                    })
                in_decay = False
    if in_decay:
        seg = ic_series[start_idx:]
        seg_anchors = anchor_indices[start_idx:]
        n_bars = int(seg_anchors[-1] - seg_anchors[0])
        n_days = n_bars / bars_per_day
        if n_days >= decay_days:
            decays.append({
                "start_anchor": int(seg_anchors[0]),
                "end_anchor": int(seg_anchors[-1]),
                "n_days": round(n_days, 1),
                "mean_ic": round(float(np.mean(seg)), 4),
            })
    return decays


def main():
    print("=" * 80)
    print("  P0-4 因子 IC 滚动监控 (模拟接 live) — XAUUSD+ M15 50K bar")
    print("=" * 80)

    # 1) 加载数据
    store = DataStore("data/market_data.db")
    bars = store.load_bars("XAUUSD+", "M15")
    assert not bars.empty
    print(f"\nLoaded {len(bars)} bars, {bars.index[0]} → {bars.index[-1]}")

    loader = ExternalDataLoader("data/market_data.db")
    ext = loader.align_to_bars(bars)
    df = bars.join(ext)

    # 2) 算因子
    engine = FactorEngine(df)
    engine.compute_all()

    # 3) 算 1-bar 未来收益
    close = bars["close"].values
    fwd_ret = np.full(len(close), np.nan)
    fwd_ret[:-1] = (close[1:] - close[:-1]) / close[:-1]

    # 4) 每个有效因子算 rolling IC 时间序列
    print(f"\nWINDOW={WINDOW} bar (≈{WINDOW/96:.1f} 天), STEP={STEP} bar (1 天)")
    print(f"共 {len(SELECTED_FACTORS)} 个因子: {SELECTED_FACTORS}")

    all_series = {}  # name -> (ic_series, anchor_indices, anchor_dates)
    for name in SELECTED_FACTORS:
        vals = engine._factor_cache[name]
        ics, anchors = compute_rolling_ic_series(vals, fwd_ret, WINDOW, STEP)
        # anchor 对应的日期
        anchor_dates = [bars.index[a] for a in anchors]
        all_series[name] = (ics, anchors, anchor_dates)
        print(f"  {name:<18s}  锚点数={len(ics)},  IC mean={ics.mean():+.4f},  "
              f"std={ics.std():.4f},  min={ics.min():+.4f},  max={ics.max():+.4f}")

    # 5) 衰减告警 — 两类: A) 连续低 IC (阈值 0.005), B) 持续负向 (regime shift)
    print("\n" + "=" * 80)
    print(f"  衰减告警 A: rolling |IC| < {DEAD_THRESHOLD} 连续 ≥ {DECAY_DAYS} 天")
    print("=" * 80)
    all_decays_a = {}
    for name, (ics, anchors, dates) in all_series.items():
        decays = detect_decay(ics, anchors, DEAD_THRESHOLD, DECAY_DAYS)
        all_decays_a[name] = decays
        if decays:
            print(f"\n  {name}:  {len(decays)} 个衰减段")
            for d in decays:
                start_date = bars.index[d["start_anchor"]]
                end_date = bars.index[d["end_anchor"]]
                print(f"    {start_date} → {end_date}  ({d['n_days']:.1f} 天,  mean |IC|={d['mean_abs_ic']:.4f})")
        else:
            print(f"  {name}:  无显著衰减 (rolling IC 持续 > {DEAD_THRESHOLD})")

    # B) 持续负向告警: rolling IC 均值 < -0.005 持续 10 天 (regime shift 迹象)
    REGIME_THRESHOLD = 0.005
    REGIME_DAYS = 10
    print("\n" + "=" * 80)
    print(f"  衰减告警 B: rolling IC < -{REGIME_THRESHOLD} 连续 ≥ {REGIME_DAYS} 天 (regime shift)")
    print("=" * 80)
    all_decays_b = {}
    for name, (ics, anchors, dates) in all_series.items():
        # 用 B 类阈值 (负向) 重新检测
        decays = detect_decay_neg(ics, anchors, REGIME_THRESHOLD, REGIME_DAYS)
        all_decays_b[name] = decays
        if decays:
            print(f"\n  {name}:  {len(decays)} 个负向段 (regime shift 候选)")
            for d in decays:
                start_date = bars.index[d["start_anchor"]]
                end_date = bars.index[d["end_anchor"]]
                print(f"    {start_date} → {end_date}  ({d['n_days']:.1f} 天,  mean IC={d['mean_ic']:+.4f})")
        else:
            print(f"  {name}:  无持续负向 (信号方向稳定)")

    # 6) 当前 IC 状态 (最新一个锚点)
    print("\n" + "=" * 80)
    print("  最新 rolling IC (50K bar 末端)")
    print("-" * 80)
    for name, (ics, anchors, dates) in all_series.items():
        latest_ic = ics[-1]
        latest_date = dates[-1]
        status = "ACTIVE" if abs(latest_ic) >= 0.02 else ("fading" if abs(latest_ic) >= 0.01 else "DEAD")
        print(f"  {name:<18s}  IC={latest_ic:+.4f}  ({status})  @ {latest_date}")

    # 7) 落盘
    out_dir = Path("data/charts")
    out_dir.mkdir(parents=True, exist_ok=True)

    # npy 数值
    np.save(out_dir / "factor_ic_rolling.npy", {
        name: {"ic_series": ics, "anchors": anchors, "dates": [str(d) for d in dates]}
        for name, (ics, anchors, dates) in all_series.items()
    })

    # txt 报告
    out_txt = out_dir / "factor_ic_rolling.txt"
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(f"P0-4 因子 IC 滚动监控 — XAUUSD+ M15 {len(bars)} bar\n")
        f.write(f"Period: {bars.index[0]} → {bars.index[-1]}\n")
        f.write(f"Window={WINDOW} bar, Step={STEP} bar (1 day)\n")
        f.write(f"Decay threshold: |IC| < {DEAD_THRESHOLD} for ≥ {DECAY_DAYS} days\n\n")

        for name in SELECTED_FACTORS:
            ics, anchors, dates = all_series[name]
            f.write(f"=== {name} ===\n")
            f.write(f"  锚点数: {len(ics)}\n")
            f.write(f"  IC mean={ics.mean():+.4f},  std={ics.std():.4f}\n")
            f.write(f"  IC min={ics.min():+.4f},  max={ics.max():+.4f}\n")
            f.write(f"  时间序列 (每 ~5 天抽样):\n")
            step_show = max(1, len(ics) // 20)
            for i in range(0, len(ics), step_show):
                f.write(f"    {dates[i]}  IC={ics[i]:+.4f}\n")
            f.write(f"  衰减段 A (低 |IC|):\n")
            if all_decays_a[name]:
                for d in all_decays_a[name]:
                    sd = bars.index[d["start_anchor"]]
                    ed = bars.index[d["end_anchor"]]
                    f.write(f"    {sd} → {ed}  ({d['n_days']:.1f} 天,  mean |IC|={d['mean_abs_ic']:.4f})\n")
            else:
                f.write(f"    (无)\n")
            f.write(f"  衰减段 B (持续负向 / regime shift):\n")
            if all_decays_b[name]:
                for d in all_decays_b[name]:
                    sd = bars.index[d["start_anchor"]]
                    ed = bars.index[d["end_anchor"]]
                    f.write(f"    {sd} → {ed}  ({d['n_days']:.1f} 天,  mean IC={d['mean_ic']:+.4f})\n")
            else:
                f.write(f"    (无)\n")
            f.write("\n")
    print(f"\n→ 落盘: {out_txt}")
    print(f"→ 落盘: {out_dir / 'factor_ic_rolling.npy'}")

    # 8) 图表 (matplotlib)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(len(SELECTED_FACTORS), 1, figsize=(12, 3 * len(SELECTED_FACTORS)),
                                  sharex=True)
        if len(SELECTED_FACTORS) == 1:
            axes = [axes]
        for ax, name in zip(axes, SELECTED_FACTORS):
            ics, anchors, dates = all_series[name]
            ax.plot(dates, ics, linewidth=0.8, label=f"rolling IC (w={WINDOW})")
            ax.axhline(0.02, color="green", linestyle="--", alpha=0.5, label="ACTIVE (0.02)")
            ax.axhline(-0.02, color="green", linestyle="--", alpha=0.5)
            ax.axhline(DEAD_THRESHOLD, color="red", linestyle=":", alpha=0.5,
                       label=f"DEAD ({DEAD_THRESHOLD})")
            ax.axhline(-DEAD_THRESHOLD, color="red", linestyle=":", alpha=0.5)
            ax.axhline(0, color="black", linewidth=0.5)
            ax.set_title(f"{name}  |  mean={ics.mean():+.4f}  std={ics.std():.4f}")
            ax.set_ylabel("IC")
            ax.legend(loc="upper right", fontsize=8)
            ax.grid(True, alpha=0.3)
        axes[-1].set_xlabel("Date")
        plt.tight_layout()
        out_png = out_dir / "factor_ic_rolling.png"
        plt.savefig(out_png, dpi=100)
        plt.close(fig)
        print(f"→ 落盘: {out_png}")
    except ImportError:
        print("\n  (matplotlib 未装, 跳过图表)")

    print("=" * 80)


if __name__ == "__main__":
    main()
