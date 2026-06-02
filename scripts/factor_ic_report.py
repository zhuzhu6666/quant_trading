"""scripts/factor_ic_report.py — 4 因子 IC 分析报告

不做"接进 paper pipeline"那种侵入式集成 (那是 P8 router 的活),
这里只是离线 IC 评估,验证 4 因子对 50K M15 bar 的预测能力.

算法:
  1. 加载 50K M15 bar
  2. 用 sliding window 算 4 因子全序列 (向量化, 不用 ta 库)
  3. IC = corr(factor_t, (close_{t+1} - close_t) / close_t)
  4. 报告: per-factor IC / abs IC / 状态 (active=abs>=0.02 / fading=0.01-0.02 / dead)
  5. 加分: 因子间相关矩阵 + IC 排名

输出:
  - 控制台表格
  - data/charts/factor_ic_report.txt (文字)
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# 让项目根可被 import (因子里都是相对 import)
sys.path.insert(0, str(Path(__file__).parent.parent))

from data.store import DataStore


# ── 4 因子向量化版本 (从 factors/*.py 抽出 sliding window 逻辑) ──


def vec_aroon_up(highs: np.ndarray, period: int = 14) -> np.ndarray:
    """Aroon Up 全序列: 长度 < period 的位置填 nan."""
    n = len(highs)
    out = np.full(n, np.nan)
    if n < period:
        return out
    # 用 stride trick 也可以, 但 50K * 14 不大, 直接循环
    for i in range(period - 1, n):
        window = highs[i - period + 1 : i + 1]
        high_idx = int(np.argmax(window))
        bars_since_high = (period - 1) - high_idx
        out[i] = (period - bars_since_high) / period * 100.0
    return out


def vec_cci(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 20) -> np.ndarray:
    """CCI 全序列."""
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period:
        return out
    tp = (highs + lows + closes) / 3.0
    for i in range(period - 1, n):
        window = tp[i - period + 1 : i + 1]
        sma = window.mean()
        mad = np.mean(np.abs(window - sma))
        if mad < 1e-12:
            continue
        out[i] = (window[-1] - sma) / (0.015 * mad)
    return out


def vec_mfi(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
            volumes: np.ndarray, period: int = 14) -> np.ndarray:
    """MFI 全序列."""
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    if np.all(volumes <= 0):
        out[:] = 50.0
        return out
    tp = (highs + lows + closes) / 3.0
    rmf = tp * volumes
    for i in range(period, n):
        tp_window = tp[i - period : i + 1]      # period+1 个
        rmf_window = rmf[i - period : i + 1]
        tp_prev = tp_window[:-1]
        tp_now = tp_window[1:]
        mf_pos = rmf_window[1:][tp_now > tp_prev].sum()
        mf_neg = rmf_window[1:][tp_now < tp_prev].sum()
        if mf_neg < 1e-12:
            out[i] = 100.0
            continue
        mfr = mf_pos / mf_neg
        out[i] = 100.0 - 100.0 / (1.0 + mfr)
    return out


def vec_williams_r(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                   period: int = 14) -> np.ndarray:
    """Williams %R 全序列."""
    n = len(closes)
    out = np.full(n, np.nan)
    if n < period:
        return out
    for i in range(period - 1, n):
        h = highs[i - period + 1 : i + 1].max()
        l = lows[i - period + 1 : i + 1].min()
        c = closes[i]
        rng = h - l
        if rng < 1e-12:
            continue
        out[i] = (h - c) / rng * -100.0
    return out


# ── 报告主流程 ──


def main():
    print("=" * 72)
    print("  4 因子 IC 报告 — XAUUSD+ M15, 50K bar")
    print("=" * 72)

    # 1. 加载数据
    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    assert not df.empty, "No M15 data"
    print(f"Loaded {len(df)} bars, {df.index[0]} → {df.index[-1]}")

    highs = df["high"].values.astype(np.float64)
    lows = df["low"].values.astype(np.float64)
    closes = df["close"].values.astype(np.float64)
    volumes = df["volume"].values.astype(np.float64) if "volume" in df.columns else np.zeros(len(closes))

    # 2. 算 4 因子
    print("\n计算 4 因子 (向量化 sliding window)...")
    factors = {
        "aroon_up":   vec_aroon_up(highs, 14),
        "cci":        vec_cci(highs, lows, closes, 20),
        "mfi":        vec_mfi(highs, lows, closes, volumes, 14),
        "williams_r": vec_williams_r(highs, lows, closes, 14),
    }
    for name, vals in factors.items():
        valid = (~np.isnan(vals)).sum()
        print(f"  {name:12s}: {valid}/{len(vals)} valid, range=[{np.nanmin(vals):.2f}, {np.nanmax(vals):.2f}]")

    # 3. 算 1-bar 未来收益
    fwd_ret = np.full(len(closes), np.nan)
    fwd_ret[:-1] = (closes[1:] - closes[:-1]) / closes[:-1]

    # 4. IC 分析
    print("\n" + "=" * 72)
    print(f"  {'Factor':<12s}  {'IC':>8s}  {'abs_IC':>8s}  {'Status':<10s}  {'N_valid':>7s}")
    print("-" * 72)

    ic_records = []
    valid_matrix = {}
    for name, vals in factors.items():
        mask = ~(np.isnan(vals) | np.isnan(fwd_ret))
        if mask.sum() < 30:
            print(f"  {name:<12s}  N/A       N/A        skip       {mask.sum()}")
            continue
        ic = float(np.corrcoef(vals[mask], fwd_ret[mask])[0, 1])
        abs_ic = abs(ic)
        if abs_ic >= 0.02:
            status = "active"
        elif abs_ic >= 0.01:
            status = "fading"
        else:
            status = "dead"
        print(f"  {name:<12s}  {ic:>+8.4f}  {abs_ic:>8.4f}  {status:<10s}  {int(mask.sum()):>7d}")
        ic_records.append({"factor": name, "ic": ic, "abs_ic": abs_ic, "status": status, "n_valid": int(mask.sum())})
        valid_matrix[name] = vals[mask]

    # 5. 因子相关矩阵
    print("\n" + "=" * 72)
    print("  因子间 Pearson 相关矩阵 (对齐 valid 索引)")
    print("-" * 72)
    if valid_matrix:
        # 找所有因子都有效的索引
        common_mask = ~np.isnan(factors["aroon_up"]) & ~np.isnan(factors["cci"]) \
                    & ~np.isnan(factors["mfi"]) & ~np.isnan(factors["williams_r"]) \
                    & ~np.isnan(fwd_ret)
        names = list(factors.keys())
        mat = np.array([factors[n][common_mask] for n in names])
        corr = np.corrcoef(mat)
        # 表头
        print(f"  {'':12s}" + "".join(f"{n:>12s}" for n in names))
        for i, n in enumerate(names):
            print(f"  {n:12s}" + "".join(f"{corr[i, j]:>+12.3f}" for j in range(len(names))))

    # 6. IC 排名
    print("\n" + "=" * 72)
    print("  IC 排名 (abs_ic 降序)")
    print("-" * 72)
    ic_records.sort(key=lambda r: r["abs_ic"], reverse=True)
    for i, r in enumerate(ic_records, 1):
        print(f"  #{i}  {r['factor']:<12s}  IC={r['ic']:+.4f}  abs={r['abs_ic']:.4f}  {r['status']}")

    # 7. 落盘
    out_path = Path("data/charts/factor_ic_report.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("4 因子 IC 报告 — XAUUSD+ M15, 50K bar\n")
        f.write(f"Period: {df.index[0]} → {df.index[-1]}\n\n")
        f.write(f"{'Factor':<12s}  {'IC':>8s}  {'abs_IC':>8s}  {'Status':<10s}  {'N_valid':>7s}\n")
        for r in ic_records:
            f.write(f"{r['factor']:<12s}  {r['ic']:>+8.4f}  {r['abs_ic']:>8.4f}  {r['status']:<10s}  {r['n_valid']:>7d}\n")
    print(f"\n→ 落盘: {out_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
