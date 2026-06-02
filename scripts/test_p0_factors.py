"""
scripts/test_p0_factors.py
==========================

P0-2 + P0-3 联合验证: 20 个因子的 IC + 相关矩阵 + PCA。
包含旧 15 + 新跨资产/事件距离 5 个。

如果 df 未注入外部数据, 跨资产/事件因子会全 NaN, 自动跳过。
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
from data.external_loader import ExternalDataLoader


def main():
    print("=" * 80)
    print("  P0-2 + P0-3 联合验证: 20 因子 IC + 相关矩阵 + PCA — M15 50K bar")
    print("=" * 80)

    # 1) 加载 bar + 外部数据
    store = DataStore("data/market_data.db")
    bars = store.load_bars("XAUUSD+", "M15")
    assert not bars.empty
    print(f"\nLoaded {len(bars)} bars, {bars.index[0]} → {bars.index[-1]}")

    loader = ExternalDataLoader("data/market_data.db")
    ext = loader.align_to_bars(bars)
    print(f"外部数据列: {list(ext.columns)}")

    # 合并: 因子访问 df 时能拿到 dxy/real_yield_10y/evt_fomc 等
    df_merged = bars.join(ext)
    print(f"合并后 df: {df_merged.shape}, 额外列: {[c for c in ext.columns if c in df_merged.columns][:5]}...")

    # 2) 算全部因子
    engine = FactorEngine(df_merged)
    engine.compute_all()
    factor_names = list(engine._factor_cache.keys())
    print(f"\n注册因子数: {len(factor_names)}")
    print("列表:")
    for f in factor_names:
        valid = (~np.isnan(engine._factor_cache[f])).sum()
        print(f"  {f:<18s}  valid={valid}")

    # 3) IC
    close = bars["close"].values
    fwd_ret = np.full(len(close), np.nan)
    fwd_ret[:-1] = (close[1:] - close[:-1]) / close[:-1]

    print("\n" + "=" * 80)
    print(f"  {'Factor':<18s}  {'IC':>8s}  {'abs_IC':>8s}  {'Status':<10s}  {'N':>6s}  Group")
    print("-" * 80)

    NEW = ["dxy_corr_20", "slv_gld_ratio", "real_yield_chg", "hours_to_fomc", "hours_to_nfp"]
    records = []
    for name in factor_names:
        vals = engine._factor_cache[name]
        mask = ~(np.isnan(vals) | np.isnan(fwd_ret))
        n = int(mask.sum())
        if n < 30:
            print(f"  {name:<18s}  N/A       N/A        skip       {n:>6d}")
            continue
        ic = float(np.corrcoef(vals[mask], fwd_ret[mask])[0, 1])
        abs_ic = abs(ic)
        status = "ACTIVE" if abs_ic >= 0.02 else ("fading" if abs_ic >= 0.01 else "dead")
        group = "P0-3" if name in NEW else "OLD"
        print(f"  {name:<18s}  {ic:>+8.4f}  {abs_ic:>8.4f}  {status:<10s}  {n:>6d}  {group}")
        records.append({"factor": name, "ic": ic, "abs_ic": abs_ic, "status": status, "n": n, "group": group})

    # 4) 排名
    print("\n" + "=" * 80)
    print("  排名 (abs_ic 降序)")
    print("-" * 80)
    records.sort(key=lambda r: r["abs_ic"], reverse=True)
    for i, r in enumerate(records, 1):
        marker = "🆕" if r["group"] == "P0-3" else "  "
        print(f"  #{i:>2d}  {r['factor']:<18s}  IC={r['ic']:+.4f}  abs={r['abs_ic']:.4f}  {r['status']:<8s}  {marker}")

    # 5) 跨资产/事件因子对基础因子的边际 IC
    # 思路: 把 5 个新因子加到原 4 有效因子上, 看 corr 矩阵是否带来新信息
    if "macd_hist" in engine._factor_cache and all(n in engine._factor_cache for n in NEW):
        print("\n" + "=" * 80)
        print("  跨资产因子 vs 基础 4 有效因子的相关 (边际信息检查)")
        print("-" * 80)
        base4 = ["macd_hist", "bb_width", "ema_slope", "di_spread"]
        common_mask = np.ones(len(close), dtype=bool)
        for n in base4 + NEW:
            common_mask &= ~np.isnan(engine._factor_cache[n])
        common_mask &= ~np.isnan(fwd_ret)
        print(f"  common valid: {int(common_mask.sum())} bar")
        for new in NEW:
            new_vals = engine._factor_cache[new][common_mask]
            max_corr = 0
            max_pair = ""
            for b in base4:
                b_vals = engine._factor_cache[b][common_mask]
                c = float(np.corrcoef(new_vals, b_vals)[0, 1])
                if abs(c) > abs(max_corr):
                    max_corr = c
                    max_pair = b
            print(f"  {new:<18s}  max |corr| with base4 = {abs(max_corr):.3f} (vs {max_pair})  "
                  f"独立度: {'高' if abs(max_corr) < 0.5 else '低'}")

    # 6) 落盘
    out_path = Path("data/charts/p0_factors_report.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"P0-2 + P0-3 因子 IC 报告 — XAUUSD+ M15 {len(bars)} bar\n")
        f.write(f"Period: {bars.index[0]} → {bars.index[-1]}\n\n")
        f.write(f"{'Factor':<18s}  {'IC':>8s}  {'abs_IC':>8s}  {'Status':<10s}  {'N':>6s}  Group\n")
        for r in records:
            f.write(f"{r['factor']:<18s}  {r['ic']:>+8.4f}  {r['abs_ic']:>8.4f}  {r['status']:<10s}  {r['n']:>6d}  {r['group']}\n")
    print(f"\n→ 落盘: {out_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
