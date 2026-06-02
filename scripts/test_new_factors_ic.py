"""
scripts/test_new_factors_ic.py
=============================

P0-1 验证: 8 个新因子的 IC (信息系数) 在 XAUUSD+ M15 50K bar 上。

每个因子输出 np.ndarray, 跟 (close[t+1] - close[t]) / close[t] 算 Pearson corr.
- abs(IC) >= 0.02 → active
- abs(IC) >= 0.01 → fading
- abs(IC) <  0.01 → dead

输出: 控制台表格 + 排名 + 落盘到 data/charts/new_factors_ic_report.txt
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.store import DataStore
from alpha.registry import factor_registry
from alpha.factor_engine import FactorEngine


# 待测的 8 个新因子
NEW_FACTORS = [
    "ema_slope", "supertrend_dir", "keltner_width", "obv_slope",
    "vol_ma_ratio", "engulfing", "pin_bar", "inside_bar",
]
# 对照组: 已有 7 个
BASELINE_FACTORS = [
    "rsi_14", "macd_hist", "adx", "bb_width", "di_spread", "stoch_k", "atr_ratio",
]


def main():
    print("=" * 78)
    print("  P0-1 验证: 8 个新因子 + 7 个 baseline 因子的 IC 报告 — XAUUSD+ M15 50K bar")
    print("=" * 78)

    # 1) 加载数据
    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    assert not df.empty, "No M15 data"
    print(f"\nLoaded {len(df)} bars, {df.index[0]} → {df.index[-1]}")
    print(f"Columns: {list(df.columns)}")

    # 2) 用 FactorEngine 算全部 15 个因子
    engine = FactorEngine(df)
    engine.compute_all()
    print(f"\n已计算 {len(engine._factor_cache)} 个因子")

    # 3) 算 1-bar 未来收益
    close = df["close"].values
    fwd_ret = (close[1:] - close[:-1]) / close[:-1]
    fwd_ret = np.append(fwd_ret, np.nan)  # 末尾对齐
    fwd_ret = pd.Series(fwd_ret).shift(-1).values  # IC 应该是 (t -> t+1)
    # 简化: 用 (t -> t+1) 跟因子对齐, 因子在 t 时刻, 收益在 t+1
    fwd_ret_aligned = np.full(len(close), np.nan)
    fwd_ret_aligned[:-1] = (close[1:] - close[:-1]) / close[:-1]

    # 4) IC 表
    all_factors = BASELINE_FACTORS + NEW_FACTORS
    print("\n" + "=" * 78)
    print(f"  {'Factor':<18s}  {'IC':>8s}  {'abs_IC':>8s}  {'Status':<10s}  {'N_valid':>7s}  {'Group':<8s}")
    print("-" * 78)

    records = []
    for name in all_factors:
        vals = engine._factor_cache.get(name)
        if vals is None:
            print(f"  {name:<18s}  N/A       N/A        skip       {0:>7d}")
            continue
        # 对齐: vals 在 t, fwd_ret_aligned 在 t (代表 t→t+1)
        mask = ~(np.isnan(vals) | np.isnan(fwd_ret_aligned))
        if mask.sum() < 30:
            print(f"  {name:<18s}  N/A       N/A        skip       {int(mask.sum()):>7d}")
            continue
        ic = float(np.corrcoef(vals[mask], fwd_ret_aligned[mask])[0, 1])
        abs_ic = abs(ic)
        if abs_ic >= 0.02:
            status = "ACTIVE"
        elif abs_ic >= 0.01:
            status = "fading"
        else:
            status = "dead"
        group = "NEW" if name in NEW_FACTORS else "base"
        print(f"  {name:<18s}  {ic:>+8.4f}  {abs_ic:>8.4f}  {status:<10s}  {int(mask.sum()):>7d}  {group:<8s}")
        records.append({"factor": name, "ic": ic, "abs_ic": abs_ic, "status": status, "n_valid": int(mask.sum()), "group": group})

    # 5) 排名
    print("\n" + "=" * 78)
    print("  IC 排名 (abs_ic 降序)")
    print("-" * 78)
    records.sort(key=lambda r: r["abs_ic"], reverse=True)
    for i, r in enumerate(records, 1):
        marker = "🆕" if r["group"] == "NEW" else "  "
        print(f"  #{i:>2d}  {r['factor']:<18s}  IC={r['ic']:+.4f}  abs={r['abs_ic']:.4f}  {r['status']:<8s}  {marker}")

    # 6) 新因子 vs baseline 对比
    print("\n" + "=" * 78)
    print("  对比: 新因子 vs baseline")
    print("-" * 78)
    new_abs = [r["abs_ic"] for r in records if r["group"] == "NEW"]
    base_abs = [r["abs_ic"] for r in records if r["group"] == "base"]
    print(f"  baseline (7 个): mean abs_IC = {np.mean(base_abs):.4f}, max = {max(base_abs):.4f}")
    print(f"  新因子 (8 个): mean abs_IC = {np.mean(new_abs):.4f}, max = {max(new_abs):.4f}")
    n_new_active = sum(1 for r in records if r["group"] == "NEW" and r["status"] == "ACTIVE")
    n_new_dead = sum(1 for r in records if r["group"] == "NEW" and r["status"] == "dead")
    print(f"  新因子中 ACTIVE: {n_new_active}/8, dead: {n_new_dead}/8")

    # 7) 落盘
    out_path = Path("data/charts/new_factors_ic_report.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("P0-1 新因子 IC 报告 — XAUUSD+ M15 50K bar\n")
        f.write(f"Period: {df.index[0]} → {df.index[-1]}\n\n")
        f.write(f"{'Factor':<18s}  {'IC':>8s}  {'abs_IC':>8s}  {'Status':<10s}  {'N_valid':>7s}  {'Group':<8s}\n")
        for r in records:
            f.write(f"{r['factor']:<18s}  {r['ic']:>+8.4f}  {r['abs_ic']:>8.4f}  {r['status']:<10s}  {r['n_valid']:>7d}  {r['group']:<8s}\n")
        f.write(f"\nbaseline mean abs_IC: {np.mean(base_abs):.4f}\n")
        f.write(f"新因子  mean abs_IC: {np.mean(new_abs):.4f}\n")
    print(f"\n→ 落盘: {out_path}")
    print("=" * 78)


if __name__ == "__main__":
    main()
