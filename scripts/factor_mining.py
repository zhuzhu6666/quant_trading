"""
因子挖掘脚本 - IC分析 + 遗传编程因子发现
数据源: SQLite数据库 (H1为主)

Usage:
    python scripts/factor_mining.py                   # IC分析 + GP挖掘
    python scripts/factor_mining.py --no-gp          # 仅IC分析
    python scripts/factor_mining.py --timeframe H1   # 指定时间周期
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import warnings
warnings.filterwarnings("ignore")

from loguru import logger
from modules.database import load_candles, table_summary

# 因子计算 - 在 df 上追加所有候选技术指标
def compute_factors(df: pd.DataFrame) -> pd.DataFrame:
    """计算候选技术因子矩阵"""
    df = df.copy()

    # ── 价格类因子 ──────────────────────────────
    df["returns"]      = df["close"].pct_change()
    df["log_return"]   = np.log(df["close"] / df["close"].shift(1))

    # ── 均线类因子 ──────────────────────────────
    for period in [5, 10, 20, 30, 50, 100, 200]:
        df[f"SMA_{period}"]    = df["close"].rolling(period).mean()
        df[f"EMA_{period}"]    = df["close"].ewm(span=period, adjust=False).mean()
        df[f"price_to_sma_{period}"] = df["close"] / df[f"SMA_{period}"] - 1  # 价格相对均线偏离度

    # ── 动量类因子 ──────────────────────────────
    for period in [3, 5, 10, 14, 20, 30]:
        df[f"momentum_{period}"]  = df["close"] / df["close"].shift(period) - 1
        df[f"roc_{period}"]      = df["close"].pct_change(period)

    # EMA差值（多均线动量）
    df["ema_diff"] = df["EMA_10"] - df["EMA_30"]

    # ── 波动率类因子 ────────────────────────────
    for period in [5, 10, 14, 20, 30]:
        df[f"std_{period}"]     = df["close"].rolling(period).std()
        df[f"atr_{period}"]     = compute_atr(df, period)

    # 布林带
    df["BB_MID"]     = df["close"].rolling(20).mean()
    df["BB_STD"]     = df["close"].rolling(20).std()
    df["BB_UPPER"]   = df["BB_MID"] + 2 * df["BB_STD"]
    df["BB_LOWER"]   = df["BB_MID"] - 2 * df["BB_STD"]
    df["BB_WIDTH"]   = (df["BB_UPPER"] - df["BB_LOWER"]) / df["BB_MID"]
    df["BB_POS"]     = (df["close"] - df["BB_LOWER"]) / (df["BB_UPPER"] - df["BB_LOWER"])  # 布林带位置 0~1

    # ── RSI类因子 ───────────────────────────────
    for period in [7, 9, 14, 21]:
        df[f"RSI_{period}"]  = compute_rsi(df["close"], period)

    # ── MACD类因子 ──────────────────────────────
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"]         = ema12 - ema26
    df["MACD_SIGNAL"]  = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"]    = df["MACD"] - df["MACD_SIGNAL"]
    df["MACD_CROSS"]   = (df["MACD"] > df["MACD_SIGNAL"]).astype(int)  # 0/1信号

    # ── ADX类因子 ───────────────────────────────
    for period in [10, 14, 20]:
        adx_data = compute_adx(df, period)
        df[f"ADX_{period}"]   = adx_data["adx"]
        df[f"PLUS_DI_{period}"] = adx_data["plus_di"]
        df[f"MINUS_DI_{period}"] = adx_data["minus_di"]
        df[f"DI_SPREAD_{period}"] = df[f"PLUS_DI_{period}"] - df[f"MINUS_DI_{period}"]

    # ── Stochastic类因子 ─────────────────────
    for period in [10, 14, 20]:
        stoch = compute_stochastic(df, period)
        df[f"STOCH_K_{period}"] = stoch["k"]
        df[f"STOCH_D_{period}"] = stoch["d"]

    # ── CCI类因子 ──────────────────────────────
    for period in [14, 20]:
        df[f"CCI_{period}"] = compute_cci(df, period)

    # ── 成交量类因子 ───────────────────────────
    df["volume"]       = df["tick_volume"]
    df["vol_sma_20"]   = df["volume"].rolling(20).mean()
    df["vol_ratio"]    = df["volume"] / df["vol_sma_20"]  # 量比

    # OBV动量
    df["OBV"]          = (np.sign(df["close"].diff()) * df["volume"]).cumsum()
    df["OBV_ma"]       = df["OBV"].rolling(20).mean()
    df["obv_momentum"] = df["OBV"] / df["OBV_ma"] - 1

    # ── 时间类因子 ─────────────────────────────
    # 注意：这些因子在实盘时需要用前一根K线收盘时的"已知信息"
    # 这里仅用于历史研究，不涉及未来函数
    df["hour"]         = df.index.hour
    df["day_of_week"]  = df.index.dayofweek  # 0=周一

    # ── 价格结构类因子 ─────────────────────────
    # 最高价/最低价相对均线
    for period in [20, 50]:
        high_max = df["high"].rolling(period).max()
        low_min  = df["low"].rolling(period).min()
        df[f"high_ratio_{period}"]  = df["close"] / high_max - 1
        df[f"low_ratio_{period}"]   = df["close"] / low_min - 1

    return df


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl   = df["high"] - df["low"]
    hc   = np.abs(df["high"] - df["close"].shift())
    lc   = np.abs(df["low"]  - df["close"].shift())
    tr   = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta   = close.diff()
    gain    = delta.where(delta > 0, 0).rolling(period).mean()
    loss    = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs      = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_adx(df: pd.DataFrame, period: int = 14) -> dict:
    high  = df["high"]
    low   = df["low"]
    close = df["close"]

    plus_dm  = high.diff()
    minus_dm = -low.diff()
    plus_dm  = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

    atr = compute_atr(df, period)

    plus_di  = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr

    dx      = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    adx     = dx.rolling(period).mean()

    return {"adx": adx, "plus_di": plus_di, "minus_di": minus_di}


def compute_stochastic(df: pd.DataFrame, period: int = 14) -> dict:
    low_min  = df["low"].rolling(period).min()
    high_max = df["high"].rolling(period).max()
    k       = 100 * (df["close"] - low_min) / (high_max - low_min + 1e-10)
    d       = k.rolling(3).mean()
    return {"k": k, "d": d}


def compute_cci(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tp   = (df["high"] + df["low"] + df["close"]) / 3
    sma  = tp.rolling(period).mean()
    mad  = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    cci  = (tp - sma) / (0.015 * mad + 1e-10)
    return cci


# ── IC分析 ─────────────────────────────────────
def compute_ic(df: pd.DataFrame, factor_col: str, target_col: str) -> dict:
    """计算单个因子的IC（信息系数）"""
    merged = df[[factor_col, target_col]].dropna()
    if len(merged) < 30:
        return None

    ic_raw   = merged[factor_col].corr(merged[target_col])
    ic_rank  = merged[factor_col].corr(merged[target_col], method="spearman")

    # 切尾IC（去除极端10%）
    pct_10 = merged[factor_col].quantile(0.10)
    pct_90 = merged[factor_col].quantile(0.90)
    clipped = merged[(merged[factor_col] >= pct_10) & (merged[factor_col] <= pct_90)]
    ic_winsorized = clipped[factor_col].corr(clipped[target_col]) if len(clipped) >= 30 else ic_raw

    return {
        "factor": factor_col,
        "target": target_col,
        "IC": ic_raw,
        "IC_rank": ic_rank,
        "IC_winsorized": ic_winsorized,
        "N": len(merged),
    }


def ic_analysis(df: pd.DataFrame, factor_cols: list, horizons: list) -> pd.DataFrame:
    """对所有因子+ horizons做IC分析"""
    results = []
    for h in horizons:
        df[f"target_{h}"] = df["close"].shift(-h) / df["close"] - 1  # h周期后收益

    for col in factor_cols:
        for h in horizons:
            ic_result = compute_ic(df, col, f"target_{h}")
            if ic_result is not None:
                results.append(ic_result)

    return pd.DataFrame(results)


def rank_factors(ic_results: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
    """综合排名：IC绝对值 + IC_rank + 稳定性"""
    # 按|IC| + |IC_rank| 平均排序
    ic_results = ic_results.copy()
    ic_results["IC_abs"]  = ic_results["IC"].abs()
    ic_results["rank_ic"] = ic_results.groupby("target")["IC_abs"].rank(ascending=False)
    # rank需要用transform保持与原DataFrame index对齐
    ic_results["rank_rank"] = ic_results.groupby("target")["IC_rank"].transform(
        lambda x: x.abs().rank(ascending=False)
    )

    # 综合得分（在各horizon的排名均值越小越好）
    agg = ic_results.groupby("factor").agg({
        "IC": ["mean", "std"],
        "IC_rank": ["mean", "std"],
        "rank_ic": "mean",
        "rank_rank": "mean",
        "N": "sum",
    }).reset_index()
    agg.columns = ["factor", "IC_mean", "IC_std", "IC_rank_mean", "IC_rank_std", "rank_ic_avg", "rank_rank_avg", "N_sum"]

    # 综合得分 = -平均|IC| + 平均|IC_rank| (都是越大越好，所以取负)
    agg["score"] = agg["IC_mean"].abs() * 10 + agg["IC_rank_mean"].abs() * 5 - agg["IC_std"] * 2

    return agg.sort_values("score", ascending=False).head(top_n)


# ── 遗传编程因子挖掘 ────────────────────────────
def install_gplearn():
    """确保gplearn已安装"""
    import subprocess
    result = subprocess.run(
        ["C:/Users/zhu/AppData/Local/Programs/Python/Python312/python.exe", "-m", "pip", "install", "gplearn", "-q"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        logger.info("gplearn安装成功")
    return result.returncode == 0


def gp_factor_discovery(df: pd.DataFrame, n_components: int = 20,
                        generations: int = 15, population: int = 500) -> pd.DataFrame:
    """用遗传编程从基础因子中发现新因子"""
    try:
        from gplearn.genetic import SymbolicTransformer
    except ImportError:
        if not install_gplearn():
            logger.error("gplearn安装失败，跳过GP因子挖掘")
            return pd.DataFrame()

    # 基础输入特征
    base_features = [
        "returns", "log_return",
        "SMA_20", "EMA_10", "EMA_30",
        "BB_STD", "BB_WIDTH", "BB_POS",
        "RSI_14", "MACD", "MACD_HIST", "MACD_CROSS",
        "ADX_14", "DI_SPREAD_14",
        "STOCH_K_14", "CCI_14",
        "atr_14", "std_14",
        "vol_ratio", "obv_momentum",
        "momentum_5", "momentum_10", "momentum_20",
        "price_to_sma_20", "high_ratio_20", "low_ratio_20",
    ]

    # 只保留存在的列
    available = [f for f in base_features if f in df.columns]
    X = df[available].fillna(0).replace([np.inf, -np.inf], 0)

    # 目标：5周期后的收益
    y = (df["close"].shift(-5) / df["close"] - 1).fillna(0).replace([np.inf, -np.inf], 0)

    # 去除NaN行
    valid = ~(X.isna().any(axis=1) | y.isna())
    X_valid = X[valid]
    y_valid = y[valid]

    if len(X_valid) < 500:
        logger.warning(f"数据不足（{len(X_valid)}行），跳过GP")
        return pd.DataFrame()

    logger.info(f"GP因子挖掘：{len(X_valid)}行数据，{len(available)}个基础特征，生成{n_components}个新因子")

    function_set = [
        "add", "sub", "mul", "div",
        "sqrt", "log", "abs",
        "max", "min",
        "sin", "cos",
    ]

    gp = SymbolicTransformer(
        function_set=function_set,
        population_size=population,
        generations=generations,
        n_components=n_components,
        random_state=42,
        verbose=0,
        parsimony_coefficient=0.001,  # 惩罚复杂公式，防止过拟合
    )

    gp.fit(X_valid.values, y_valid.values)
    new_factors = gp.transform(X_valid.values)

    # 将新因子加入DataFrame并计算IC
    gp_results = []
    # gplearn 0.4.x: use _best_programs to get evolved formulas
    best_programs = getattr(gp, '_best_programs', [])
    for i in range(new_factors.shape[1]):
        col_name = f"GP_FACTOR_{i+1}"
        formula = str(best_programs[i]) if i < len(best_programs) else f"formula_{i}"
        df_gp = pd.DataFrame({
            col_name: new_factors[:, i],
            "target": y_valid.values,
        }).dropna()
        if len(df_gp) < 30:
            continue
        ic = df_gp[col_name].corr(df_gp["target"])
        ic_rank = df_gp[col_name].corr(df_gp["target"], method="spearman")
        gp_results.append({
            "factor": col_name,
            "formula": formula,
            "IC": ic,
            "IC_rank": ic_rank,
            "IC_abs": abs(ic),
        })

    result_df = pd.DataFrame(gp_results)
    if not result_df.empty:
        result_df = result_df.sort_values("IC_abs", ascending=False)
        logger.info(f"GP生成{len(result_df)}个候选因子")
        for _, row in result_df.head(5).iterrows():
            logger.info(f"  {row['factor']}: IC={row['IC']:.4f}, formula={row['formula'][:60]}")

    return result_df


# ── 主流程 ─────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframe", default="H1", choices=["M5", "M15", "M30", "H1", "H4", "D1"])
    parser.add_argument("--no-gp", action="store_true", help="跳过遗传编程")
    parser.add_argument("--top", type=int, default=30, help="IC报告展示前N个因子")
    args = parser.parse_args()

    logger.info(f"加载 {args.timeframe} 数据...")
    df = load_candles("XAUUSD+", args.timeframe)
    df.set_index("time", inplace=True)
    logger.info(f"数据: {len(df)} bars  {df.index[0]} ~ {df.index[-1]}")

    # 计算因子
    logger.info("计算候选技术因子矩阵...")
    df = compute_factors(df)

    # 因子列表（排除原始价格列）
    exclude = {"time", "open", "high", "low", "close", "tick_volume", "spread",
               "real_volume", "volume", "hour", "day_of_week", "target_1", "target_5",
               "target_10", "target_20", "target_60"}
    factor_cols = [c for c in df.columns if c not in exclude and not c.startswith("target_")]

    # IC分析 - 多个持有期
    horizons = [1, 5, 10, 20, 60]  # 1=1小时, 5=5小时, ...
    logger.info(f"IC分析: {len(factor_cols)}个因子 × {len(horizons)}个持有期")
    ic_results = ic_analysis(df, factor_cols, horizons)

    if ic_results.empty:
        logger.error("无有效IC结果")
        return

    # 综合排名
    ranked = rank_factors(ic_results, top_n=args.top)

    print("\n" + "="*80)
    print(f"  IC分析报告 - XAUUSD+ {args.timeframe} ({len(df)} bars)")
    print("="*80)
    print(f"\n{'排名':<4} {'因子':<25} {'IC均值':>8} {'IC_std':>8} {'Rank_IC':>8} {'稳定性':>8} {'得分':>6}")
    print("-"*80)
    for i, (_, row) in enumerate(ranked.iterrows(), 1):
        stability = row["IC_std"] / max(abs(row["IC_mean"]), 0.001)
        print(f"{i:<4} {row['factor']:<25} {row['IC_mean']:>8.4f} {row['IC_std']:>8.4f} "
              f"{row['IC_rank_mean']:>8.4f} {stability:>8.2f} {row['score']:>6.3f}")

    # 打印各horizon明细
    print("\n" + "="*80)
    print("  各持有期IC明细（Top10因子）")
    print("="*80)
    top_factors = ranked["factor"].head(10).tolist()
    # 为ic_results添加IC_abs以便排序
    ic_results["IC_abs"] = ic_results["IC"].abs()
    for target_h in horizons:
        col_name = f"target_{target_h}"
        subset = ic_results[ic_results["target"] == col_name]
        subset = subset[subset["factor"].isin(top_factors)].sort_values("IC_abs", ascending=False)
        print(f"\n持有期={target_h}周期:")
        for _, row in subset.head(5).iterrows():
            print(f"  {row['factor']:<25} IC={row['IC']:>7.4f}  Rank_IC={row['IC_rank']:>7.4f}  N={row['N']}")

    # 遗传编程
    if not args.no_gp:
        print("\n" + "="*80)
        print("  遗传编程因子挖掘")
        print("="*80)
        gp_results = gp_factor_discovery(df, n_components=20, generations=15)
        if not gp_results.empty:
            # 保存GP因子到数据库备用
            print(f"\nGP候选因子（按IC排序）:")
            for _, row in gp_results.head(10).iterrows():
                ic_flag = "✓" if abs(row["IC"]) > 0.03 else " "
                print(f"  [{ic_flag}] {row['factor']}: IC={row['IC']:.4f}  Rank_IC={row['IC_rank']:.4f}")
                print(f"       公式: {row['formula'][:80]}")

    # 保存IC报告
    ranked.to_csv(f"data/ic_report_{args.timeframe}.csv", index=False)
    logger.info(f"IC报告已保存: data/ic_report_{args.timeframe}.csv")

    print("\n完成!")


if __name__ == "__main__":
    main()