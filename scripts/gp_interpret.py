"""
GP因子解读 + 样本外验证
功能：
  1. 将GP公式的X0~X25索引映射回真实因子名
  2. Train/Test分割（70%/30%）
  3. 在样本外验证每个GP因子的IC稳定性
  4. 筛选出有效的（样本外IC>0.03且符号一致）因子

Usage:
    python scripts/gp_interpret.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
from loguru import logger

from modules.database import load_candles
from scripts.factor_mining import compute_factors, compute_ic


# ── 索引 → 因子名 映射 ──────────────────────────────
FEATURE_NAMES = [
    "returns",       # X0
    "log_return",    # X1
    "SMA_20",        # X2
    "EMA_10",        # X3
    "EMA_30",        # X4
    "BB_STD",        # X5
    "BB_WIDTH",      # X6
    "BB_POS",        # X7
    "RSI_14",        # X8
    "MACD",          # X9
    "MACD_HIST",     # X10
    "MACD_CROSS",    # X11
    "ADX_14",        # X12
    "DI_SPREAD_14",  # X13
    "STOCH_K_14",    # X14
    "CCI_14",        # X15
    "atr_14",        # X16
    "std_14",        # X17
    "vol_ratio",     # X18
    "obv_momentum",  # X19
    "momentum_5",    # X20
    "momentum_10",   # X21
    "momentum_20",   # X22
    "price_to_sma_20",# X23
    "high_ratio_20", # X24
    "low_ratio_20",  # X25
]


def decode_gp_formula(formula: str) -> str:
    """将X0~X25替换为真实因子名"""
    for i, name in enumerate(FEATURE_NAMES):
        formula = formula.replace(f"X{i}", name)
    return formula


def simplify_formula(formula: str) -> str:
    """化简公式：去掉 mul(X,X) -> X^2 这类表达，更易读"""
    # 去除连续相同函数包装
    import re
    formula = formula.strip()
    return formula


def load_and_prepare_data(timeframe="H1"):
    """加载并计算所有候选因子"""
    df = load_candles("XAUUSD+", timeframe)
    df.set_index("time", inplace=True)
    df = compute_factors(df)
    return df


def out_of_sample_ic(df: pd.DataFrame, factor_col: str, target_col: str,
                     train_ratio: float = 0.70) -> dict:
    """
    计算样本内/外IC
    train_ratio: 前70%做训练集，后30%做测试集
    """
    merged = df[[factor_col, target_col]].dropna()
    if len(merged) < 60:
        return None

    n_train = int(len(merged) * train_ratio)
    train = merged.iloc[:n_train]
    test  = merged.iloc[n_train:]

    def _ic(series1, series2, method="pearson"):
        if len(series1) < 30:
            return np.nan
        return series1.corr(series2, method=method)

    ic_train = _ic(train[factor_col], train[target_col])
    ic_test  = _ic(test[factor_col],  test[target_col])

    # Rank IC
    ic_train_rank = _ic(train[factor_col], train[target_col], method="spearman")
    ic_test_rank  = _ic(test[factor_col],  test[target_col],  method="spearman")

    return {
        "factor":   factor_col,
        "IC_train": ic_train,
        "IC_test":  ic_test,
        "IC_train_rank": ic_train_rank,
        "IC_test_rank":  ic_test_rank,
        "IC_sign_consistent": (ic_train > 0) == (ic_test > 0) if not (np.isnan(ic_train) or np.isnan(ic_test)) else False,
        "N_train": len(train),
        "N_test":  len(test),
    }


def main():
    print("=" * 80)
    print("  GP因子解读 + 样本外验证")
    print("=" * 80)

    # ── 1. 加载数据并计算因子 ─────────────────────
    print("\n[1] 加载数据...")
    df = load_and_prepare_data("H1")
    n = len(df)
    n_train = int(n * 0.70)
    print(f"总数据: {n} bars")
    print(f"  样本内(Train): {n_train} bars ({df.index[0]} ~ {df.index[n_train-1]})")
    print(f"  样本外(Test):  {n - n_train} bars ({df.index[n_train]} ~ {df.index[-1]})")

    # ── 2. 生成GP因子 ───────────────────────────
    print("\n[2] 计算GP候选因子...")

    base_features = FEATURE_NAMES  # X0~X25
    available = [f for f in base_features if f in df.columns]
    X = df[available].fillna(0).replace([np.inf, -np.inf], 0)
    y = (df["close"].shift(-5) / df["close"] - 1).fillna(0).replace([np.inf, -np.inf], 0)
    valid = ~(X.isna().any(axis=1) | y.isna())
    X_v = X[valid]
    y_v = y[valid]
    print(f"有效数据: {len(X_v)}行")

    # GP
    from gplearn.genetic import SymbolicTransformer
    gp = SymbolicTransformer(
        function_set=["add", "sub", "mul", "div", "sqrt", "log", "abs", "max", "min", "sin", "cos"],
        population_size=500,
        generations=15,
        n_components=20,
        random_state=42,
        verbose=0,
        parsimony_coefficient=0.001,
    )
    gp.fit(X_v.values, y_v.values)

    # 获取GP生成的因子值
    new_factors = gp.transform(X_v.values)
    best_programs = gp._best_programs

    # ── 3. 构建GP因子DataFrame ──────────────────
    gp_df = pd.DataFrame(new_factors, index=X_v.index)
    for i in range(new_factors.shape[1]):
        gp_df.rename(columns={i: f"GP_{i+1}"}, inplace=True)

    # 合并真实收益（target）
    y_aligned = y.reindex(gp_df.index)
    gp_df["target"] = y_aligned

    # ── 4. 解读GP公式 ────────────────────────────
    print("\n[3] GP因子解读：")
    decoded = []
    for i, prog in enumerate(best_programs):
        raw = str(prog)
        readable = decode_gp_formula(raw)
        decoded.append({
            "gp_factor": f"GP_{i+1}",
            "formula_raw": raw,
            "formula_decoded": readable,
        })
        print(f"\n  GP_{i+1}:")
        print(f"    公式: {readable[:120]}{'...' if len(readable)>120 else ''}")

    # ── 5. 样本外验证 ───────────────────────────
    print("\n" + "=" * 80)
    print("  样本外验证（Train=70% / Test=30%）")
    print("=" * 80)

    horizons = [1, 5, 10, 20, 60]
    all_results = []

    # 传统Top因子验证
    top_factors = [
        "BB_STD", "std_20", "std_14", "std_30", "BB_WIDTH",
        "MACD_HIST", "MINUS_DI_14", "atr_10", "DI_SPREAD_14", "STOCH_D_20",
    ]

    print("\n【传统技术因子】")
    print(f"{'因子':<20} {'Train_IC':>10} {'Test_IC':>10} {'符号一致':>8} {'稳定性':>8}")
    print("-" * 60)

    for fact in top_factors:
        if fact not in df.columns:
            continue
        h = 10  # 用10周期作为代表
        df[f"target_{h}"] = df["close"].shift(-h) / df["close"] - 1
        result = out_of_sample_ic(df, fact, f"target_{h}")
        if result:
            flag = "✓" if result["IC_sign_consistent"] else "✗"
            stability = abs(result["IC_test"]) / abs(result["IC_train"]) if result["IC_train"] != 0 else 0
            print(f"  {fact:<18} {result['IC_train']:>10.4f} {result['IC_test']:>10.4f} {flag:>8} {stability:>8.2f}")
            all_results.append({**result, "horizon": h, "source": "traditional", "horizon": h})

    # GP因子验证
    print("\n【GP遗传编程因子】")
    print(f"{'因子':<20} {'Train_IC':>10} {'Test_IC':>10} {'符号一致':>8} {'Test>0.03':>10}")
    print("-" * 65)

    gp_validated = []
    for i in range(new_factors.shape[1]):
        col = f"GP_{i+1}"
        h = 5
        result = out_of_sample_ic(gp_df, col, "target")
        if result is None:
            continue

        flag = "✓" if result["IC_sign_consistent"] else "✗"
        test_ok = "✓" if abs(result["IC_test"]) > 0.03 else " "
        readable = decoded[i]["formula_decoded"] if i < len(decoded) else ""
        stability = abs(result["IC_test"]) / abs(result["IC_train"]) if result["IC_train"] != 0 else 0

        print(f"  {col:<18} {result['IC_train']:>10.4f} {result['IC_test']:>10.4f} "
              f"{flag:>8} {test_ok:>10}")
        print(f"    公式: {readable[:90]}{'...' if len(readable)>90 else ''}")

        gp_validated.append({
            **result,
            "source": "gp",
            "formula": readable,
        })
        all_results.append({**result, "source": "gp", "formula": readable})

    # ── 6. 总结：有效因子 ──────────────────────
    print("\n" + "=" * 80)
    print("  样本外验证总结")
    print("=" * 80)

    # GP因子筛选条件：Test IC > 0.03 且符号一致
    gp_passed = [r for r in gp_validated
                 if abs(r["IC_test"]) > 0.03 and r["IC_sign_consistent"]]
    trad_passed = [r for r in all_results
                  if r["source"] == "traditional"
                  and abs(r["IC_test"]) > 0.03
                  and r["IC_sign_consistent"]]

    print(f"\n传统因子（样本外IC>0.03且符号一致）: {len(trad_passed)}个")
    for r in sorted(trad_passed, key=lambda x: abs(x["IC_test"]), reverse=True):
        print(f"  ✓ {r['factor']}: Train={r['IC_train']:.4f}  Test={r['IC_test']:.4f}")

    print(f"\nGP因子（样本外IC>0.03且符号一致）: {len(gp_passed)}个")
    for r in sorted(gp_passed, key=lambda x: abs(x["IC_test"]), reverse=True):
        factor_name = r.get("factor", f"GP_{r.get('col', '?')}")
        print(f"  ✓ {factor_name}: Train={r['IC_train']:.4f}  Test={r['IC_test']:.4f}")
        formula = r.get("formula", "")
        if formula:
            print(f"    {formula[:90]}")

    # 保存
    summary_df = pd.DataFrame(all_results)
    summary_df.to_csv("data/gp_oos_validation.csv", index=False)
    logger.info("验证报告已保存: data/gp_oos_validation.csv")

    print("\n完成!")


if __name__ == "__main__":
    main()