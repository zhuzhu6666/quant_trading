"""
data/external_loader.py
=======================

把日度宏观/事件数据 forward-fill 到 M15 bar 级别, 供 alpha/registry 因子调用。

数据源 (data/market_data.db):
  - macro_daily: DFII10 / DTWEXBGS (DXY 代理) / GVZCLS / VIXCLS
  - etf_daily:   GLD / SLV / TLT  (收盘价)
  - etf_holdings: GLD / SLV 持仓量 (吨) + shares outstanding  (P0-ETF 2026-06-03)
  - cb_gold:     央行黄金月度净买入 (吨)  (P0-CB 2026-06-03)
  - events:      FOMC / NFP / CPI / PCE, 共 105 条

输出 (对齐到 bar_df 的 DatetimeIndex):
  DataFrame 含列: dxy / real_yield_10y / gvz / vix / gld / slv / tlt
  + 事件距离列: days_to_fomc / days_to_nfp / days_to_cpi / days_to_pce
  + 时段列: hour_utc / day_of_week
  + ETF 持仓列: gld_tonnes / gld_tonnes_chg_5d / gld_tonnes_chg_20d
                / slv_tonnes / slv_tonnes_chg_5d / slv_tonnes_chg_20d
  + 央行列:     cb_total_chg_3m  (全球央行 3 月累计净买入, 吨)

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
    """外部数据 (宏观 + 事件 + ETF + 央行) 对齐到 bar 级别"""

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

    def _load_etf_holdings(self) -> pd.DataFrame:
        """加载 etf_holdings (P0-ETF 2026-06-03).

        返回列: gld_tonnes / slv_tonnes / gld_shares / slv_shares / gld_aum
        """
        con = sqlite3.connect(self.db_path)
        try:
            rows = con.execute(
                "SELECT symbol, date, total_tonnes, total_shares, aum_usd "
                "FROM etf_holdings ORDER BY date"
            ).fetchall()
        except sqlite3.OperationalError:
            # 表不存在 (旧库) → 返空
            con.close()
            return pd.DataFrame()
        con.close()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["symbol", "date", "tonnes", "shares", "aum"])
        df["date"] = pd.to_datetime(df["date"])
        # pivot: 每个 symbol → 独立列
        tonnes = df.pivot(index="date", columns="symbol", values="tonnes")
        tonnes.columns = [f"{c}_tonnes" for c in tonnes.columns]
        shares = df.pivot(index="date", columns="symbol", values="shares")
        shares.columns = [f"{c}_shares" for c in shares.columns]
        aum = df.pivot(index="date", columns="symbol", values="aum")
        aum.columns = [f"{c}_aum" for c in aum.columns]
        out = tonnes.join(shares, how="outer").join(aum, how="outer")
        out = out.sort_index()
        return out

    def _load_cb_gold(self) -> pd.DataFrame:
        """加载 cb_gold (P0-CB 2026-06-03).

        返回列: cb_total_total / cb_total_chg_monthly
                cb_china_total / cb_china_chg_monthly
                ...
        """
        con = sqlite3.connect(self.db_path)
        try:
            rows = con.execute(
                "SELECT country, date, total_tonnes, monthly_chg_tonnes "
                "FROM cb_gold ORDER BY date"
            ).fetchall()
        except sqlite3.OperationalError:
            con.close()
            return pd.DataFrame()
        con.close()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=["country", "date", "total", "chg"])
        df["date"] = pd.to_datetime(df["date"])
        # 国家 → 前缀
        total = df.pivot(index="date", columns="country", values="total")
        total.columns = [f"cb_{c.lower()}_total" for c in total.columns]
        chg = df.pivot(index="date", columns="country", values="chg")
        chg.columns = [f"cb_{c.lower()}_chg" for c in chg.columns]
        out = total.join(chg, how="outer")
        out = out.sort_index()
        return out

    def _load_cot_gold(self) -> pd.DataFrame:
        """加载 cot_gold (P0-COT 2026-06-03, CFTC disagg 周度).

        返回列 (含派生):
            cot_open_interest / cot_mm_long / cot_mm_short / cot_mm_spread
            cot_pm_long / cot_pm_short
            cot_swap_long / cot_swap_short
            cot_other_long / cot_other_short
            cot_mm_net = mm_long - mm_short
            cot_pm_net = pm_long - pm_short
            cot_mm_net_pct_oi = mm_net / open_interest
            cot_mm_net_chg_4w = mm_net_pct_oi 4w diff
        """
        con = sqlite3.connect(self.db_path)
        try:
            rows = con.execute(
                "SELECT report_date, open_interest, mm_long, mm_short, mm_spread, "
                "pm_long, pm_short, swap_long, swap_short, other_long, other_short "
                "FROM cot_gold ORDER BY report_date"
            ).fetchall()
        except sqlite3.OperationalError:
            con.close()
            return pd.DataFrame()
        con.close()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows, columns=[
            "report_date", "open_interest", "mm_long", "mm_short", "mm_spread",
            "pm_long", "pm_short", "swap_long", "swap_short", "other_long", "other_short",
        ])
        df["report_date"] = pd.to_datetime(df["report_date"])
        df = df.set_index("report_date").sort_index()
        # 派生
        df["cot_mm_net"] = df["mm_long"] - df["mm_short"]
        df["cot_pm_net"] = df["pm_long"] - df["pm_short"]
        df["cot_mm_net_pct_oi"] = df["cot_mm_net"] / df["open_interest"]
        df["cot_mm_net_chg_4w"] = df["cot_mm_net_pct_oi"].diff(4)
        # 重命名原始列
        rename_map = {
            "open_interest": "cot_open_interest",
            "mm_long": "cot_mm_long",
            "mm_short": "cot_mm_short",
            "mm_spread": "cot_mm_spread",
            "pm_long": "cot_pm_long",
            "pm_short": "cot_pm_short",
            "swap_long": "cot_swap_long",
            "swap_short": "cot_swap_short",
            "other_long": "cot_other_long",
            "other_short": "cot_other_short",
        }
        df = df.rename(columns=rename_map)
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

    def _compute_etf_derived(self, etf_holdings: pd.DataFrame) -> pd.DataFrame:
        """从原始 holdings 计算派生列: chg_5d / chg_20d / chg_pct."""
        if etf_holdings.empty:
            return pd.DataFrame()
        out = etf_holdings.copy()
        for sym in ["GLD", "SLV"]:
            tonnes_col = f"{sym}_tonnes"
            if tonnes_col in out.columns:
                # 5d 变化 (吨)
                out[f"{sym}_tonnes_chg_5d"] = out[tonnes_col].diff(5)
                # 20d 变化 (吨)
                out[f"{sym}_tonnes_chg_20d"] = out[tonnes_col].diff(20)
                # 5d 变化百分比
                out[f"{sym}_tonnes_pct_5d"] = out[tonnes_col].pct_change(5) * 100
                # 20d 变化百分比
                out[f"{sym}_tonnes_pct_20d"] = out[tonnes_col].pct_change(20) * 100
                # 60d z-score (跟历史比较)
                roll60 = out[tonnes_col].rolling(60, min_periods=20)
                out[f"{sym}_tonnes_zscore_60d"] = (
                    (out[tonnes_col] - roll60.mean()) / roll60.std()
                )
        return out

    def _compute_cb_derived(self, cb_gold: pd.DataFrame) -> pd.DataFrame:
        """从央行月度数据派生 3m/6m 累计净买入."""
        if cb_gold.empty:
            return pd.DataFrame()
        out = cb_gold.copy()
        for country in ["china", "russia", "turkey", "india", "total"]:
            chg_col = f"cb_{country}_chg"
            if chg_col in out.columns:
                # 3 月累计 (季度)
                out[f"cb_{country}_chg_3m"] = out[chg_col].rolling(3, min_periods=1).sum()
                # 6 月累计 (半年)
                out[f"cb_{country}_chg_6m"] = out[chg_col].rolling(6, min_periods=1).sum()
                # 12 月累计 (年度)
                out[f"cb_{country}_chg_12m"] = out[chg_col].rolling(12, min_periods=1).sum()
        return out

    def align_to_bars(self, bar_df: pd.DataFrame) -> pd.DataFrame:
        """
        把所有外部数据 forward-fill 到 bar_df 的 DatetimeIndex。

        Args:
            bar_df: 必须是 M15 频率, DatetimeIndex

        Returns:
            DataFrame (index=bar_df.index), 列:
                dxy / real_yield_10y / gvz / vix / gld / slv / tlt /
                evt_fomc / evt_nfp / evt_cpi / evt_pce +
                gld_tonnes / gld_tonnes_chg_5d / gld_tonnes_chg_20d /
                slv_tonnes / slv_tonnes_chg_5d / slv_tonnes_chg_20d +
                cb_total_chg_3m / cb_china_chg_3m / ...
        """
        if not isinstance(bar_df.index, pd.DatetimeIndex):
            raise ValueError("bar_df must have DatetimeIndex")

        macro = self._load_macro()
        etf = self._load_etf()
        etf_holdings_raw = self._load_etf_holdings()
        etf_holdings = self._compute_etf_derived(etf_holdings_raw)
        cb_raw = self._load_cb_gold()
        cb_gold = self._compute_cb_derived(cb_raw)
        cot_gold = self._load_cot_gold()
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
        ext = (macro
               .join(etf, how="outer")
               .join(etf_holdings, how="outer")
               .join(cb_gold, how="outer")
               .join(cot_gold, how="outer")
               .join(events_df, how="outer"))
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
    print(f"Columns ({len(ext.columns)}): {list(ext.columns)}")
    print(f"\n最近 5 行:")
    print(ext.tail())
    print(f"\n覆盖率 (非 NaN):")
    for c in ext.columns:
        pct = (1 - ext[c].isna().mean()) * 100
        print(f"  {c:<30s}  {pct:>6.1f}%")
