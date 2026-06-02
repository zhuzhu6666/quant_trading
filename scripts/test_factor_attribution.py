"""scripts/test_factor_attribution.py — FactorAttribution 4 因子验证

加载 50K M15 bar, 算 4 因子 (Aroon/CCI/MFI/Williams %R) 全序列,
构造 factor_returns (T, 4) 矩阵 + forward_returns,
调 FactorAttribution.full_report() 输出.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import time as _time
import numpy as np

from data.store import DataStore
from scripts.factor_ic_report import (
    vec_aroon_up, vec_cci, vec_mfi, vec_williams_r,
)
from alpha.factor_attribution import FactorAttribution


def main():
    print("=" * 78)
    print("  FactorAttribution — 4 因子 (Aroon/CCI/MFI/Williams %R) 归因报告")
    print("=" * 78)

    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    assert not df.empty
    n = len(df)
    print(f"Loaded {n} M15 bars")

    closes = df["close"].values.astype(np.float64)
    highs = df["high"].values.astype(np.float64)
    lows = df["low"].values.astype(np.float64)
    volumes = df["volume"].values.astype(np.float64) if "volume" in df.columns else np.zeros(n)

    t0 = _time.time()
    print("\n计算 4 因子 (向量化)...")
    aroon = vec_aroon_up(highs, 14)
    cci = vec_cci(highs, lows, closes, 20)
    mfi = vec_mfi(highs, lows, closes, volumes, 14)
    williams_r = vec_williams_r(highs, lows, closes, 14)
    print(f"  done [{_time.time()-t0:.1f}s]")

    # 构造 factor_returns (T, 4) + forward_returns
    forward_returns = np.full(n, np.nan)
    forward_returns[:-1] = (closes[1:] - closes[:-1]) / closes[:-1]

    factor_returns = np.column_stack([aroon, cci, mfi, williams_r])
    factor_names = ["aroon", "cci", "mfi", "williams_r"]

    # 去掉 nan 行
    valid = ~np.isnan(forward_returns)
    for i in range(factor_returns.shape[1]):
        valid &= ~np.isnan(factor_returns[:, i])
    factor_returns_v = factor_returns[valid]
    forward_returns_v = forward_returns[valid]
    print(f"  有效样本: {valid.sum()} / {n} ({100*valid.sum()/n:.1f}%)")

    # 跑归因
    print("\n跑 FactorAttribution (4 因子归因)...")
    t0 = _time.time()
    fa = FactorAttribution(factor_names, factor_returns_v, forward_returns_v)
    report = fa.full_report()
    print(f"  done [{_time.time()-t0:.1f}s]\n")
    print(report)

    # 关键断言: 4 因子 IC 全 < 0.02 (跟之前 factor_ic_report 一致)
    ic_df = fa.compute_ic_matrix()
    print("\n  断言: 4 因子 IC 全 < 0.02 (跟 factor_ic_report 一致)")
    for _, row in ic_df.iterrows():
        msg = "✓" if abs(row["ic_mean"]) < 0.02 else "✗"
        print(f"    {msg} {row['factor']:<15s} IC={row['ic_mean']:+.4f}  "
              f"active={row['active']}  t={row['t_stat']:.2f}")

    # 断言: redundancy 应有 6 对 (4 选 2 = 6), 相关 > 0.6
    redun = fa.redundancy_report(threshold=0.6)
    print(f"\n  冗余对 (|corr| > 0.6): {len(redun)} 个 (期望 6 = C(4,2))")
    for m in redun:
        print(f"    {m}")

    print()
    print("=" * 78)
    print("  ✅ FactorAttribution 验证完成")
    print("=" * 78)


if __name__ == "__main__":
    main()
