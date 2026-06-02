"""scripts/discover_factors.py — 因子发现 CLI (T15.5, 2026-06-02)

用法:
    python scripts/discover_factors.py \\
        --n-candidates 1000 \\
        --top-k 50 \\
        --max-depth 4 \\
        --forward-periods 1,5,20 \\
        --auto-register

跟 L1 配合:
- 用 alpha/factor_discovery.FactorDiscovery
- 自动 register 到 alpha/registry_adapter.RegistryAdapter
- 报告落盘 data/charts/factor_discovery/run_<timestamp>.json
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.store import DataStore
from data.external_loader import ExternalDataLoader
from alpha.factor_discovery import FactorDiscovery, DiscoveryConfig


def main():
    import argparse
    parser = argparse.ArgumentParser(description="L2 因子发现 (DSL 搜索 + IC 评分 + 自动 register)")
    parser.add_argument("--symbol", default="XAUUSD+")
    parser.add_argument("--timeframe", default="M15", choices=["M5", "M15", "M30", "H1", "H4", "D1"])
    parser.add_argument("--n-candidates", type=int, default=500)
    parser.add_argument("--top-k", type=int, default=30)
    parser.add_argument("--max-depth", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--pca-threshold", type=float, default=0.5)
    parser.add_argument("--min-score", type=float, default=30.0)
    parser.add_argument("--forward-periods", default="1,5,20",
                        help="逗号分隔 forward periods (用于 cross-validation)")
    parser.add_argument("--auto-register", action="store_true",
                        help="自动 register 候选到 RegistryAdapter (默认 dry-run)")
    parser.add_argument("--db-path", default="data/market_data.db")
    args = parser.parse_args()

    print(f"=== L2 因子发现 ===")
    print(f"  symbol={args.symbol} timeframe={args.timeframe}")
    print(f"  n_candidates={args.n_candidates} top_k={args.top_k} max_depth={args.max_depth}")
    print(f"  forward_periods={args.forward_periods}")
    print(f"  auto_register={args.auto_register}")
    print()

    # 加载数据
    store = DataStore(args.db_path)
    df = store.load_bars(args.symbol, args.timeframe)
    if df.empty:
        print(f"无 {args.timeframe} 数据")
        return
    print(f"加载 {len(df)} bar (range: {df.index[0]} → {df.index[-1]})")

    # 注入跨资产列
    ext = ExternalDataLoader(args.db_path)
    ext_df = ext.align_to_bars(df)
    df = df.join(ext_df, how="left")
    print(f"注入 {len(ext_df.columns)} 跨资产列 (dxy/real_yield/gvz/...)")

    # 跑发现
    config = DiscoveryConfig(
        n_candidates=args.n_candidates,
        top_k=args.top_k,
        max_depth=args.max_depth,
        seed=args.seed,
        pca_correlation_threshold=args.pca_threshold,
        min_score_to_keep=args.min_score,
        forward_periods=[int(x) for x in args.forward_periods.split(",")],
    )
    discovery = FactorDiscovery(df, db_path=args.db_path)
    run = discovery.run(config=config, auto_register=args.auto_register)

    # 打印总结
    print()
    print("=" * 72)
    print(f"  发现结果总结")
    print("=" * 72)
    print(f"  总候选: {run.search_result.n_total}")
    print(f"  有效:   {run.search_result.n_valid}")
    print(f"  HEALTHY: {run.search_result.n_healthy}")
    print(f"  WATCH:   {run.search_result.n_watch}")
    print(f"  DECAYING: {run.search_result.n_decaying}")
    print(f"  去重后:  {len(run.after_dedup)}")
    print(f"  promoted: {len(run.promoted)} (shadow register 到 RegistryAdapter)")
    print(f"  报告:    {run.log_path}")
    print()
    print(f"  耗时: {run.search_result.elapsed_sec:.1f}s "
          f"({run.search_result.avg_time_per_expr*1000:.1f}ms/expr)")
    print("=" * 72)

    if run.promoted:
        print(f"\n  promoted 候选 (需要影子测试后再升 ACTIVE):")
        for name in run.promoted:
            meta = discovery.adapter.get_meta(name)
            print(f"    {name}: {meta.get('description', '')[:60]}")


if __name__ == "__main__":
    main()
