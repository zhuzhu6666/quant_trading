"""
data/external_loader.py
=======================

把日度宏观/事件数据 forward-fill 到 M15 bar 级别, 供 alpha/registry 因子调用。

数据源 (data/market_data.db):
  - macro_daily: DFII10 / DTWEXBGS (DXY 代理) / GVZCLS / VIXCLS
  - etf_daily:   GLD / SLV / TLT
  - events:      FOMC / NFP / CPI / PCE, 共 105 条

输出 (对齐到 bar_df 的 DatetimeIndex):
  DataFrame 含列: dxy / real_yield_10y / gvz / vix / gld / slv / tlt
  + 事件距离列: days_to_fomc / days_to_nfp / days_to_cpi / days_to_pce
  + 时段列: hour_utc / day_of_week

用法:
    loader = ExternalDataLoader("data/market_data.db")
    df_ext = loader.load_aligned(bar_df)  # bar_df 必须是 M15 DatetimeIndex
    # 之后 df = bar_df.join(df_ext) 即可让因子访问到
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


class ExternalDataLoader:
    """外部数据 (宏观 + 事件 + ETF) 对齐到 bar 级别"""

    def __init__(self, db_path: str = "data/market_data.db"):
        self.db_path = db_path

    def _load_macro(self) -> pd.DataFrame:
        """加载 macro_daily, 列: date / dfii10 / dxy / gvz / vix"""
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT date, series, value FROM macro_daily ORDER BY date"
        ).fetchall()
        con.close()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["date", "series", "value"])
        df = df.pivot(index="date", columns="series", values="value")
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        return df

    def _load_etf(self) -> pd.DataFrame:
        """加载 etf_daily, 列: gld / slv / tlt"""
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT date, symbol, close FROM etf_daily ORDER BY date"
        ).fetchall()
        con.close()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["date", "symbol", "close"])
        df = df.pivot(index="date", columns="symbol", values="close")
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()
        return df

    def _load_events(self) -> pd.DataFrame:
        """加载 events, 列: date / fomc / nfp / cpi / pce (1=是事件日)"""
        con = sqlite3.connect(self.db_path)
        rows = con.execute(
            "SELECT date, type FROM events ORDER BY date"
        ).fetchall()
        con.close()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["date", "type"])
        df["date"] = pd.to_datetime(df["date"])
        df["flag"] = 1
        pivot = df.pivot(index="date", columns="type", values="flag").fillna(0)
        pivot = pivot.reindex(columns=["FOMC", "NFP", "CPI", "PCE"], fill_value=0)
        return pivot

    def align_to_bars(self, bar_df: pd.DataFrame) -> pd.DataFrame:
        """
        把所有外部数据 forward-fill 到 bar_df 的 DatetimeIndex。

        Args:
            bar_df: 必须是 M15 频率, DatetimeIndex

        Returns:
            DataFrame (index=bar_df.index), 列:
                dxy / real_yield_10y / gvz / vix / gld / slv / tlt /
                evt_fomc / evt_nfp / evt_cpi / evt_pce
        """
        if not isinstance(bar_df.index, pd.DatetimeIndex):
            raise ValueError("bar_df must have DatetimeIndex")

        macro = self._load_macro()
        etf = self._load_etf()
        events = self._load_events()

        # 列重命名, 标准化
        col_map = {
            "DFII10": "real_yield_10y",
            "DTWEXBGS": "dxy",
            "GVZCLS": "gvz",
            "VIXCLS": "vix",
        }
        macro = macro.rename(columns=col_map)

        # 事件 0/1 化
        evt_cols = {}
        for c in ["FOMC", "NFP", "CPI", "PCE"]:
            if c in events.columns:
                evt_cols[f"evt_{c.lower()}"] = events[c]
        events_df = pd.DataFrame(evt_cols) if evt_cols else pd.DataFrame()

        # 合并到一个 df (按日 index)
        ext = macro.join(etf, how="outer").join(events_df, how="outer")
        ext = ext.sort_index()

        # Reindex 到 bar 级别 (含周末/假日的 bar index), forward-fill
        ext = ext.reindex(bar_df.index, method="ffill")

        # 边界处理: 头部 bfill (取最早已知值), 尾部若全 NaN 则保留
        ext = ext.bfill(axis=0)

        # 事件列 NaN → 0 (非事件日)
        for c in ext.columns:
            if c.startswith("evt_"):
                ext[c] = ext[c].fillna(0).astype(np.int8)

        return ext


if __name__ == "__main__":
    # 简单 sanity check
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from data.store import DataStore
    store = DataStore("data/market_data.db")
    bars = store.load_bars("XAUUSD+", "M15")
    print(f"Loaded {len(bars)} bars, range: {bars.index[0]} → {bars.index[-1]}")

    loader = ExternalDataLoader("data/market_data.db")
    ext = loader.align_to_bars(bars)
    print(f"\nExternal df shape: {ext.shape}")
    print(f"Columns: {list(ext.columns)}")
    print(f"\n最近 5 行:")
    print(ext.tail())
    print(f"\n覆盖率 (非 NaN):")
    for c in ext.columns:
        pct = (1 - ext[c].isna().mean()) * 100
        print(f"  {c:<18s}  {pct:>6.1f}%")
