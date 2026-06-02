"""
factors/_test_factors.py
========================

测试 4 个因子的正确性和数值合理性.

流程:
    1) 从 data/store.py 加载 XAUUSD+ M15 最近 500 根 bar
    2) 对每根 bar (rolling 方式) 调用 4 个因子, 收集序列
    3) 打印每个因子的最后 5 个值 + 基本统计 (mean/std/min/max)
    4) 验证: warmup 之后不应出现 NaN

运行:  python factors/_test_factors.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

# 让脚本既能 `python factors/_test_factors.py` 又能 `python -m factors._test_factors` 跑
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.store import DataStore  # noqa: E402
from factors import (  # noqa: E402
    compute_aroon,
    compute_cci,
    compute_mfi,
    compute_williams_r,
)


# 每个因子的 (name, period, function) — 形参统一 (h, l, c[, v])
FACTOR_SPECS = [
    ("Aroon(14)", 14, lambda h, l, c, v: compute_aroon(h, l, period=14)),
    ("CCI(20)", 20, lambda h, l, c, v: compute_cci(h, l, c, period=20)),
    ("MFI(14)", 14, lambda h, l, c, v: compute_mfi(h, l, c, v, period=14)),
    ("WilliamsR(14)", 14, lambda h, l, c, v: compute_williams_r(h, l, c, period=14)),
]


def _stats(arr: np.ndarray) -> str:
    """对去掉 NaN 的序列输出 mean/std/min/max."""
    valid = arr[~np.isnan(arr)]
    if valid.size == 0:
        return "no valid values"
    return (
        f"mean={valid.mean():.4f}  std={valid.std(ddof=0):.4f}  "
        f"min={valid.min():.4f}  max={valid.max():.4f}  n={valid.size}"
    )


def main() -> int:
    print("=" * 70)
    print("Loading XAUUSD+ M15 (latest 500 bars) from data/store.py ...")
    ds = DataStore("data/market_data.db")
    df = ds.load_bars("XAUUSD+", "M15")
    if df.empty:
        print("ERROR: no bars loaded")
        return 1

    # 只取最近 500 根
    df = df.tail(500).copy()
    print(f"Loaded {len(df)} bars, range: {df.index[0]}  →  {df.index[-1]}")
    print(f"Columns: {list(df.columns)}")
    print(f"Volume stats: min={df['volume'].min()}, max={df['volume'].max()}, "
          f"mean={df['volume'].mean():.2f}")
    print()

    highs = df["high"].to_numpy(dtype=np.float64)
    lows = df["low"].to_numpy(dtype=np.float64)
    closes = df["close"].to_numpy(dtype=np.float64)
    volumes = df["volume"].to_numpy(dtype=np.float64)

    # Rolling 计算: 从第 1 根 (i=0) 起就用 windows=[: i+1], 长度不够时因子返回 nan
    n = len(df)
    series: dict[str, np.ndarray] = {name: np.full(n, np.nan) for name, _, _ in FACTOR_SPECS}

    for i in range(n):
        h = highs[: i + 1]
        l = lows[: i + 1]
        c = closes[: i + 1]
        v = volumes[: i + 1]
        for name, _, fn in FACTOR_SPECS:
            series[name][i] = fn(h, l, c, v)

    # 打印每个因子的最后 5 个值 + 统计
    print("=" * 70)
    print("Factor results (last 5 values + stats)")
    print("=" * 70)
    for name, _, _ in FACTOR_SPECS:
        arr = series[name]
        last5 = arr[-5:]
        # 把 NaN 渲染成 'nan' 字符串便于阅读
        last5_str = ", ".join(
            f"{x:.4f}" if not (isinstance(x, float) and np.isnan(x)) else "nan"
            for x in last5
        )
        print(f"\n{name}")
        print(f"  last 5: [{last5_str}]")
        print(f"  stats : {_stats(arr)}")

    # 验证: 早期 warmup 之外不应有 NaN
    # 取最长 period = 20 (CCI), 前面 20 根预期 NaN, 之后必须全非 NaN
    print()
    print("=" * 70)
    print("NaN check (after warmup=20, no NaN expected)")
    print("=" * 70)
    ok = True
    warmup = 20
    for name, _, _ in FACTOR_SPECS:
        arr = series[name]
        tail = arr[warmup:]
        nan_count = int(np.isnan(tail).sum())
        status = "OK" if nan_count == 0 else f"FAIL ({nan_count} NaN)"
        if nan_count > 0:
            ok = False
        print(f"  {name:20s}  NaN after warmup: {status}")

    # 边界值检查 (合理范围)
    print()
    print("=" * 70)
    print("Range check (valid bars only)")
    print("=" * 70)
    ranges = {
        "Aroon(14)": (-0.0, 100.0),
        "CCI(20)": (-1000.0, 1000.0),     # 极端值
        "MFI(14)": (0.0, 100.0),
        "WilliamsR(14)": (-100.0, -0.0),
    }
    for name, _, _ in FACTOR_SPECS:
        arr = series[name][warmup:]
        valid = arr[~np.isnan(arr)]
        lo, hi = ranges[name]
        in_range = bool(((valid >= lo) & (valid <= hi)).all())
        print(f"  {name:20s}  range [{valid.min():.4f}, {valid.max():.4f}]  "
              f"in [{lo}, {hi}]: {in_range}")
        if not in_range:
            ok = False

    print()
    print("=" * 70)
    print("FINAL:", "ALL PASS" if ok else "SOME CHECKS FAILED")
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
