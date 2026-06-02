"""
scripts/factor_pca.py
=====================

P0-2 因子结构分析: 相关矩阵 + PCA 降维 + 有效因子集。

输入: alpha/registry 全部因子 (15 个), XAUUSD+ M15 50K bar
输出:
  - 15x15 Pearson 相关矩阵 (控制台 + 落盘)
  - PCA: 解释方差比 + 累计方差 + 各主成分 top-5 贡献因子
  - 有效因子集 (基于 corr < 0.7 + IC >= 0.005 双重门槛)

落盘: data/charts/factor_pca_report.txt + .npy (loadings)
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


CORR_THRESHOLD = 0.7   # |corr| > 0.7 视为冗余
IC_THRESHOLD = 0.005   # abs(IC) < 0.005 视为无信号


def main():
    print("=" * 80)
    print("  P0-2 因子结构分析: 相关矩阵 + PCA — XAUUSD+ M15 50K bar")
    print("=" * 80)

    # 1) 加载
    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    assert not df.empty
    print(f"\nLoaded {len(df)} bars, {df.index[0]} → {df.index[-1]}")

    # 1b) 合并外部数据 (跨资产/事件因子需要 dxy / real_yield / evt_fomc 等)
    from data.external_loader import ExternalDataLoader
    loader = ExternalDataLoader("data/market_data.db")
    ext = loader.align_to_bars(df)
    df = df.join(ext)
    print(f"合并外部数据后: {df.shape}, 额外列: {len(ext.columns)} 个")

    # 2) 算全部因子
    engine = FactorEngine(df)
    engine.compute_all()
    factor_names = list(engine._factor_cache.keys())
    print(f"已计算 {len(factor_names)} 个因子")

    # 3) 算 1-bar 未来收益
    close = df["close"].values
    fwd_ret = np.full(len(close), np.nan)
    fwd_ret[:-1] = (close[1:] - close[:-1]) / close[:-1]

    # 4) 收集 IC
    ics = {}
    for name in factor_names:
        vals = engine._factor_cache[name]
        mask = ~(np.isnan(vals) | np.isnan(fwd_ret))
        if mask.sum() < 100:
            continue
        ics[name] = float(np.corrcoef(vals[mask], fwd_ret[mask])[0, 1])
    print(f"有效 IC 因子: {len(ics)}/{len(factor_names)}")

    # 5) 找 common valid 索引
    common_mask = np.ones(len(close), dtype=bool)
    for name in factor_names:
        vals = engine._factor_cache[name]
        common_mask &= ~np.isnan(vals)
    common_mask &= ~np.isnan(fwd_ret)
    print(f"common valid bars: {int(common_mask.sum())}")

    # 6) 因子矩阵 (n_bars x n_factors), 标准化
    X = np.column_stack([engine._factor_cache[n][common_mask] for n in factor_names])
    means = X.mean(axis=0)
    stds_raw = X.std(axis=0)
    stds = stds_raw.copy()
    stds[stds == 0] = 1.0
    Xn = (X - means) / stds  # z-score

    # 过滤: 真正零方差 (constant) 的列
    # 修复: 不要用修改后的 stds (被改成 1.0) 判定, 用 stds_raw
    valid_cols = ~np.isclose(stds_raw, 0.0) & (stds_raw > 1e-10)
    n_dropped = int((~valid_cols).sum())
    if n_dropped > 0:
        dropped = [n for n, v in zip(factor_names, valid_cols) if not v]
        print(f"\n  ⚠ Dropped {n_dropped} zero-variance factor(s) from corr/PCA: {dropped}")
        factor_names = [n for n, v in zip(factor_names, valid_cols)]
        Xn = Xn[:, valid_cols]
        means = means[valid_cols]
        stds = stds[valid_cols]

    # 7) Pearson 相关矩阵
    corr = np.corrcoef(Xn.T)
    print("\n" + "=" * 80)
    print("  因子间 Pearson 相关矩阵 (z-score)")
    print("=" * 80)
    # 控制台打印
    col_w = 10
    header = " " * 18 + "".join(f"{n[:col_w-1]:>{col_w}}" for n in factor_names)
    print(header)
    for i, n in enumerate(factor_names):
        row = f"{n:<18s}" + "".join(f"{corr[i, j]:>+{col_w}.2f}" for j in range(len(factor_names)))
        print(row)

    # 8) 冗余对 (|corr| > 0.7)
    print("\n" + "=" * 80)
    print(f"  冗余对 (|corr| > {CORR_THRESHOLD})")
    print("-" * 80)
    redundant_pairs = []
    for i in range(len(factor_names)):
        for j in range(i + 1, len(factor_names)):
            c = corr[i, j]
            if abs(c) > CORR_THRESHOLD:
                redundant_pairs.append((factor_names[i], factor_names[j], c))
                # 保留 IC 大的那个
                ic_i = abs(ics.get(factor_names[i], 0))
                ic_j = abs(ics.get(factor_names[j], 0))
                keep = factor_names[i] if ic_i >= ic_j else factor_names[j]
                drop = factor_names[j] if keep == factor_names[i] else factor_names[i]
                print(f"  {factor_names[i]:<18s}  ↔  {factor_names[j]:<18s}  "
                      f"corr={c:+.3f}  IC_i={ic_i:.4f}  IC_j={ic_j:.4f}  → 保留 {keep}")
    print(f"\n共 {len(redundant_pairs)} 对冗余")

    # 9) 选有效因子: IC >= 阈值, 且 (粗筛) 跟最高 IC 因子不冗余
    sorted_by_ic = sorted(ics.items(), key=lambda x: abs(x[1]), reverse=True)
    print("\n" + "=" * 80)
    print(f"  有效因子集 (abs_IC >= {IC_THRESHOLD}, 且去冗余)")
    print("-" * 80)
    selected = []
    for name, ic in sorted_by_ic:
        if abs(ic) < IC_THRESHOLD:
            print(f"  {name:<18s}  IC={ic:+.4f}  → 剔除 (IC 太低)")
            continue
        # 检查跟已选的相关性
        is_redundant = False
        for sel in selected:
            i = factor_names.index(name)
            j = factor_names.index(sel)
            if abs(corr[i, j]) > CORR_THRESHOLD:
                is_redundant = True
                print(f"  {name:<18s}  IC={ic:+.4f}  → 剔除 (与 {sel} 相关 {corr[i, j]:+.3f})")
                break
        if not is_redundant:
            selected.append(name)
            print(f"  {name:<18s}  IC={ic:+.4f}  → 保留")
    print(f"\n有效因子: {len(selected)} 个 → {selected}")

    # 10) PCA
    if len(selected) >= 2:
        X_sel = np.column_stack([engine._factor_cache[n][common_mask] for n in selected])
        means_sel = X_sel.mean(axis=0)
        stds_sel = X_sel.std(axis=0)
        stds_sel[stds_sel == 0] = 1.0
        X_sel_n = (X_sel - means_sel) / stds_sel

        # 协方差矩阵 → 特征分解
        cov = np.cov(X_sel_n.T)
        eigvals, eigvecs = np.linalg.eigh(cov)
        # 降序
        idx = np.argsort(eigvals)[::-1]
        eigvals = eigvals[idx]
        eigvecs = eigvecs[:, idx]

        # 解释方差比
        total_var = eigvals.sum()
        var_ratio = eigvals / total_var
        cum_var = np.cumsum(var_ratio)

        print("\n" + "=" * 80)
        print(f"  PCA on {len(selected)} 有效因子 (z-score 协方差)")
        print("=" * 80)
        print(f"  {'PC':<5s}  {'var_ratio':>10s}  {'cum_var':>10s}  {'top-3 贡献因子'}")
        print("-" * 80)
        n_show = min(8, len(selected))
        for k in range(n_show):
            loadings = eigvecs[:, k]
            top3_idx = np.argsort(np.abs(loadings))[::-1][:3]
            top3 = ", ".join(f"{selected[i]}({loadings[i]:+.2f})" for i in top3_idx)
            print(f"  PC{k+1:<3d}  {var_ratio[k]:>10.4f}  {cum_var[k]:>10.4f}  {top3}")

        n_90 = int(np.searchsorted(cum_var, 0.90) + 1)
        n_95 = int(np.searchsorted(cum_var, 0.95) + 1)
        print(f"\n  累计 90% 方差需 {n_90} 个 PC, 95% 需 {n_95} 个 PC (共 {len(selected)} 维)")

        # 落盘 loadings 供后续 XGBoost / factor_engine 使用
        np.save("data/charts/factor_pca_loadings.npy", {
            "selected_factors": selected,
            "eigvals": eigvals,
            "eigvecs": eigvecs,
            "var_ratio": var_ratio,
            "cum_var": cum_var,
            "means": means_sel,
            "stds": stds_sel,
        })
        print("  → 落盘: data/charts/factor_pca_loadings.npy")
    else:
        print("\n  ⚠ 有效因子 < 2, 跳过 PCA")
        n_90 = n_95 = 0

    # 11) 落盘文字报告
    out_path = Path("data/charts/factor_pca_report.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"因子结构分析 — XAUUSD+ M15 {len(df)} bar\n")
        f.write(f"Period: {df.index[0]} → {df.index[-1]}\n")
        f.write(f"Total factors: {len(factor_names)}, valid IC: {len(ics)}\n\n")

        f.write("== 相关矩阵 ==\n")
        f.write(header + "\n")
        for i, n in enumerate(factor_names):
            f.write(f"{n:<18s}" + "".join(f"{corr[i, j]:>+{col_w}.2f}" for j in range(len(factor_names))) + "\n")
        f.write(f"\n冗余对: {len(redundant_pairs)}\n")
        for a, b, c in redundant_pairs:
            f.write(f"  {a} ↔ {b}: {c:+.3f}\n")

        f.write(f"\n== 有效因子集 (去冗余后) ==\n{selected}\n")
        f.write(f"维数: {len(selected)}, 90% 方差需 {n_90} PC, 95% 需 {n_95} PC\n")
    print(f"\n→ 落盘: {out_path}")
    print("=" * 80)


if __name__ == "__main__":
    main()
