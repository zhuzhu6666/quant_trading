"""scripts/seed_etf_holdings_proxy.py — 用 etf_daily 价格反推 holdings 代理 (P0-ETF 2026-06-03)

现实: yfinance/SPDR/WGC 都被限流/反爬, 真实 GLD/SLV 总持仓数据无法实时拉取
本脚本: 用 GLD/SLV 收盘价 × 假设初始总吨数 模拟 持仓时间序列

逻辑:
  GLD price ≈ 1/100 oz gold (1 share ≈ 0.0096 oz 后调)
  实际 GLD 1 share ≈ 0.0096 oz (1/100 oz 设计), 但实际 NAV 是 LBMA Gold Price PM / 100
  GLD holdings (tonnes) = GLD shares outstanding × 0.0096 / 32150.7

本 proxy: 直接把 GLD 收盘价作为"持仓量代理"传入 etf_holdings.total_tonnes
  跟价格相关性 = 1, 但 5d/20d diff 跟金价的 5d/20d diff 抵消, 残差才是真资金流

之后改进: 用 yfinance.Ticker('GLD').get_shares_full() 拉真实 shares outstanding
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlite3
import pandas as pd

DB = "data/market_data.db"


def main():
    con = sqlite3.connect(DB)
    # 拉 etf_daily
    rows = con.execute(
        "SELECT date, symbol, close FROM etf_daily WHERE symbol IN ('GLD', 'SLV') ORDER BY date"
    ).fetchall()
    con.close()
    if not rows:
        print("ERROR: etf_daily 表无数据")
        return
    df = pd.DataFrame(rows, columns=["date", "symbol", "close"])
    pivot = df.pivot(index="date", columns="symbol", values="close").sort_index()
    print(f"etf_daily range: {pivot.index[0]} → {pivot.index[-1]}")
    print(f"rows: {len(pivot)}")

    # 计算代理: GLD price 直接作为 total_tonnes (单位 USD, 解释为"GLD 收盘价 = 持仓代理")
    # 这样 diff_5d/20d = 资金流代理
    # 同时计算 SLV
    con = sqlite3.connect(DB)
    inserted = 0
    for date, row in pivot.iterrows():
        if not pd.isna(row.get("GLD")):
            # price-as-tonnes proxy (后面 replace)
            con.execute(
                "INSERT OR REPLACE INTO etf_holdings (symbol, date, total_tonnes, total_shares, aum_usd) "
                "VALUES (?, ?, ?, ?, ?)",
                ("GLD", date, float(row["GLD"]), None, None),
            )
            inserted += 1
        if not pd.isna(row.get("SLV")):
            con.execute(
                "INSERT OR REPLACE INTO etf_holdings (symbol, date, total_tonnes, total_shares, aum_usd) "
                "VALUES (?, ?, ?, ?, ?)",
                ("SLV", date, float(row["SLV"]), None, None),
            )
            inserted += 1
    con.commit()
    con.close()
    print(f"Inserted {inserted} proxy rows into etf_holdings (GLD/SLV price-as-tonnes)")


if __name__ == "__main__":
    main()
