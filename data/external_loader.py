"""
data/external_loader.py
=======================

把日度宏观/事件数据 forward-fill 到 M15 bar 级别, 供 alpha/registry 因子调用。

数据源:
  - external_data.duckdb: macro_daily / etf_daily / etf_holdings / cb_gold / cot_gold
  - events.duckdb: FOMC / NFP / CPI / PCE 事件日
  - macro_daily: DFII10 / DTWEXBGS (DXY 代理) / GVZCLS / VIXCLS
  - etf_daily:   GLD / SLV / TLT  (收盘价)
  - etf_holdings: GLD / SLV 持仓量 (吨) + shares outstanding  (P0-ETF 2026-06-03)
  - cb_gold:     央行黄金月度/季度净买入 (吨)  (P0-CB 2026-06-03)
  - events:      FOMC / NFP / CPI / PCE, 共 105 条

输出 (对齐到 bar_df 的 DatetimeIndex):
  DataFrame 含列: dxy / real_yield_10y / gvz / vix / gld / slv / tlt
  + 事件距离列: days_to_fomc / days_to_nfp / days_to_cpi / days_to_pce
  + 时段列: hour_utc / day_of_week
  + ETF 持仓列: gld_tonnes / gld_tonnes_chg_5d / gld_tonnes_chg_20d
                / slv_tonnes / slv_tonnes_chg_5d / slv_tonnes_chg_20d
  + 央行列:     cb_total_chg_3m  (全球央行 3 月累计净买入, 吨)

用法:
    loader = ExternalDataLoader()
    df_ext = loader.load_aligned(bar_df)  # bar_df 必须是 M15 DatetimeIndex
    # 之后 df = bar_df.join(df_ext) 即可让因子访问到
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import time as _time

import numpy as np
import pandas as pd

from backend.core.db import DUCKDB_EVENTS, DUCKDB_EXTERNAL, duckdb_readonly_connection


DEFAULT_EVENT_TIMES: dict[str, str] = {
    "FOMC": "19:00",
    "NFP": "13:30",
    "CPI": "13:30",
    "PCE": "13:30",
}


class ExternalDataLoader:
    """外部数据 (宏观 + 事件 + ETF + 央行) 对齐到 bar 级别"""

    _CACHE_TTL_SEC = 300.0
    _SOURCE_BUNDLE_CACHE_MAX = 16
    _LOAD_CACHE: dict[tuple[str, str, int, int], tuple[float, pd.DataFrame]] = {}
    _SOURCE_BUNDLE_CACHE: dict[tuple[object, ...], tuple[float, dict[str, pd.DataFrame]]] = {}

    def __init__(
        self,
        db_path: str | Path = DUCKDB_EXTERNAL,
        events_db_path: str | Path = DUCKDB_EVENTS,
        event_times: dict[str, str] | None = None,
    ):
        self.db_path = Path(db_path)
        self.events_db_path = Path(events_db_path)
        self.event_times = event_times or DEFAULT_EVENT_TIMES

    @classmethod
    def _cache_key(cls, name: str, path: Path) -> tuple[str, str, int, int]:
        try:
            stat = path.stat()
            return (name, str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
        except OSError:
            return (name, str(path), 0, 0)

    @classmethod
    def _get_cached(cls, name: str, path: Path) -> pd.DataFrame | None:
        key = cls._cache_key(name, path)
        cached = cls._LOAD_CACHE.get(key)
        if cached is None:
            return None
        created_at, frame = cached
        if _time.time() - created_at > cls._CACHE_TTL_SEC:
            cls._LOAD_CACHE.pop(key, None)
            return None
        return frame.copy()

    @classmethod
    def _set_cached(cls, name: str, path: Path, frame: pd.DataFrame) -> pd.DataFrame:
        key = cls._cache_key(name, path)
        cls._LOAD_CACHE[key] = (_time.time(), frame.copy())
        return frame

    def _source_bundle_key(self, as_of_epoch: float | None) -> tuple[object, ...]:
        return (
            self._cache_key("external", self.db_path),
            self._cache_key("events", self.events_db_path),
            None if as_of_epoch is None else float(as_of_epoch),
        )

    def _get_source_bundle(self, as_of_epoch: float | None) -> dict[str, pd.DataFrame]:
        key = self._source_bundle_key(as_of_epoch)
        now = _time.time()
        self._prune_source_bundle_cache(now)
        cached = self._SOURCE_BUNDLE_CACHE.get(key)
        if cached is not None:
            created_at, bundle = cached
            if now - created_at <= self._CACHE_TTL_SEC:
                return {name: frame.copy() for name, frame in bundle.items()}
            self._SOURCE_BUNDLE_CACHE.pop(key, None)

        macro = self._limit_as_of(self._load_macro(), as_of_epoch)
        etf = self._limit_as_of(self._load_etf(), as_of_epoch)
        etf_holdings_raw = self._limit_as_of(self._load_etf_holdings(), as_of_epoch)
        cb_raw = self._limit_as_of(self._load_cb_gold(), as_of_epoch)
        cot_gold = self._limit_as_of(self._load_cot_gold(), as_of_epoch)
        events = self._load_events()

        macro = macro.rename(
            columns={
                "DFII10": "real_yield_10y",
                "DTWEXBGS": "dxy",
                "GVZCLS": "gvz",
                "VIXCLS": "vix",
            }
        )
        bundle = {
            "macro": self._compute_macro_derived(macro),
            "etf": self._compute_etf_price_derived(etf),
            "etf_holdings": self._compute_etf_derived(etf_holdings_raw),
            "cb_gold": self._compute_cb_derived(cb_raw),
            "cot_gold": cot_gold,
            "events": events,
        }
        self._SOURCE_BUNDLE_CACHE[key] = (now, {name: frame.copy() for name, frame in bundle.items()})
        self._prune_source_bundle_cache(now)
        return bundle

    @classmethod
    def _prune_source_bundle_cache(cls, now: float | None = None) -> None:
        current = _time.time() if now is None else float(now)
        expired = [
            key
            for key, (created_at, _) in cls._SOURCE_BUNDLE_CACHE.items()
            if current - created_at > cls._CACHE_TTL_SEC
        ]
        for key in expired:
            cls._SOURCE_BUNDLE_CACHE.pop(key, None)
        overflow = len(cls._SOURCE_BUNDLE_CACHE) - cls._SOURCE_BUNDLE_CACHE_MAX
        if overflow > 0:
            oldest = sorted(cls._SOURCE_BUNDLE_CACHE.items(), key=lambda item: item[1][0])
            for key, _ in oldest[:overflow]:
                cls._SOURCE_BUNDLE_CACHE.pop(key, None)

    @staticmethod
    def _release_index(values) -> pd.DatetimeIndex:
        idx = pd.to_datetime(list(values), unit="s", utc=True)
        return pd.DatetimeIndex(idx).tz_convert(None)

    @staticmethod
    def _as_epoch(as_of: datetime | str | int | float | None) -> float | None:
        if as_of is None:
            return None
        if isinstance(as_of, (int, float)):
            return float(as_of)
        return float(pd.Timestamp(as_of).timestamp())

    def _load_macro(self) -> pd.DataFrame:
        """加载 macro_daily, 列: date / dfii10 / dxy / gvz / vix"""
        cached = self._get_cached("macro", self.db_path)
        if cached is not None:
            return cached
        with duckdb_readonly_connection(self.db_path) as con:
            rows = con.execute(
                "SELECT date, series, value, release_at FROM macro_daily ORDER BY release_at, date"
            ).fetchall()
        if not rows:
            return self._set_cached("macro", self.db_path, pd.DataFrame())
        df = pd.DataFrame(rows, columns=["date", "series", "value", "release_at"])
        df["release_dt"] = self._release_index(df["release_at"])
        df = df.pivot_table(index="release_dt", columns="series", values="value", aggfunc="last")
        df = df.sort_index()
        return self._set_cached("macro", self.db_path, df)

    def _load_etf(self) -> pd.DataFrame:
        """加载 etf_daily, 列: gld / slv / tlt"""
        cached = self._get_cached("etf", self.db_path)
        if cached is not None:
            return cached
        with duckdb_readonly_connection(self.db_path) as con:
            rows = con.execute(
                "SELECT date, symbol, close, release_at FROM etf_daily ORDER BY release_at, date"
            ).fetchall()
        if not rows:
            return self._set_cached("etf", self.db_path, pd.DataFrame())
        df = pd.DataFrame(rows, columns=["date", "symbol", "close", "release_at"])
        df["release_dt"] = self._release_index(df["release_at"])
        df = df.pivot_table(index="release_dt", columns="symbol", values="close", aggfunc="last")
        df = df.sort_index()
        return self._set_cached("etf", self.db_path, df)

    def _load_etf_holdings(self) -> pd.DataFrame:
        """加载 etf_holdings (P0-ETF 2026-06-03).

        返回列: gld_tonnes / slv_tonnes / gld_shares / slv_shares / gld_aum
        """
        cached = self._get_cached("etf_holdings", self.db_path)
        if cached is not None:
            return cached
        try:
            with duckdb_readonly_connection(self.db_path) as con:
                rows = con.execute(
                    "SELECT symbol, date, total_tonnes, total_shares, aum_usd, release_at "
                    "FROM etf_holdings ORDER BY date"
                ).fetchall()
        except Exception:
            # 表不存在 (旧库) → 返空
            return self._set_cached("etf_holdings", self.db_path, pd.DataFrame())
        if not rows:
            return self._set_cached("etf_holdings", self.db_path, pd.DataFrame())
        df = pd.DataFrame(rows, columns=["symbol", "date", "tonnes", "shares", "aum", "release_at"])
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["symbol", "date"])
        df["release_dt"] = self._release_index(df["release_at"])
        # pivot: 每个 symbol → 独立列
        tonnes = df.pivot_table(index="release_dt", columns="symbol", values="tonnes", aggfunc="last")
        tonnes.columns = [f"{c}_tonnes" for c in tonnes.columns]
        shares = df.pivot_table(index="release_dt", columns="symbol", values="shares", aggfunc="last")
        shares.columns = [f"{c}_shares" for c in shares.columns]
        aum = df.pivot_table(index="release_dt", columns="symbol", values="aum", aggfunc="last")
        aum.columns = [f"{c}_aum" for c in aum.columns]
        out = tonnes.join(shares, how="outer").join(aum, how="outer")
        out = out.sort_index()
        return self._set_cached("etf_holdings", self.db_path, out)

    def _load_cb_gold(self) -> pd.DataFrame:
        """加载 cb_gold (P0-CB 2026-06-03).

        返回列: cb_total_total / cb_total_chg_monthly
                cb_china_total / cb_china_chg_monthly
                ...
        """
        cached = self._get_cached("cb_gold", self.db_path)
        if cached is not None:
            return cached
        try:
            with duckdb_readonly_connection(self.db_path) as con:
                rows = con.execute(
                    "SELECT country, date, total_tonnes, monthly_chg_tonnes, release_at "
                    "FROM cb_gold ORDER BY date"
                ).fetchall()
        except Exception:
            return self._set_cached("cb_gold", self.db_path, pd.DataFrame())
        if not rows:
            return self._set_cached("cb_gold", self.db_path, pd.DataFrame())
        df = pd.DataFrame(rows, columns=["country", "date", "total", "chg", "release_at"])
        df["date"] = pd.to_datetime(df["date"])
        df["release_dt"] = self._release_index(df["release_at"])
        # 国家 → 前缀
        total = df.pivot_table(index="release_dt", columns="country", values="total", aggfunc="last")
        total.columns = [f"cb_{c.lower()}_total" for c in total.columns]
        chg = df.pivot_table(index="release_dt", columns="country", values="chg", aggfunc="last")
        chg.columns = [f"cb_{c.lower()}_chg" for c in chg.columns]
        out = total.join(chg, how="outer")
        out = out.sort_index()
        return self._set_cached("cb_gold", self.db_path, out)

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
            cot_extreme_signal = mm_z - pm_z 的离散反转信号
        """
        cached = self._get_cached("cot_gold", self.db_path)
        if cached is not None:
            return cached
        try:
            with duckdb_readonly_connection(self.db_path) as con:
                rows = con.execute(
                    "SELECT report_date, open_interest, mm_long, mm_short, mm_spread, "
                    "pm_long, pm_short, swap_long, swap_short, other_long, other_short, release_at "
                    "FROM cot_gold ORDER BY report_date"
                ).fetchall()
        except Exception:
            return self._set_cached("cot_gold", self.db_path, pd.DataFrame())
        if not rows:
            return self._set_cached("cot_gold", self.db_path, pd.DataFrame())
        df = pd.DataFrame(rows, columns=[
            "report_date", "open_interest", "mm_long", "mm_short", "mm_spread",
            "pm_long", "pm_short", "swap_long", "swap_short", "other_long", "other_short", "release_at",
        ])
        df["report_date"] = pd.to_datetime(df["report_date"])
        df = df.sort_values("report_date")
        # 派生
        df["cot_mm_net"] = df["mm_long"] - df["mm_short"]
        df["cot_pm_net"] = df["pm_long"] - df["pm_short"]
        df["cot_mm_net_pct_oi"] = np.divide(
            df["cot_mm_net"],
            df["open_interest"],
            out=np.full(len(df), np.nan, dtype=float),
            where=df["open_interest"].to_numpy(dtype=float) != 0,
        )
        df["cot_mm_net_chg_4w"] = df["cot_mm_net_pct_oi"].diff(4)
        roll52 = df["cot_mm_net_pct_oi"].rolling(52, min_periods=20)
        df["cot_mm_net_zscore_52w"] = (
            (df["cot_mm_net_pct_oi"] - roll52.mean()) / roll52.std()
        )
        # COT 是周度来源。先在来源时间线上计算标准列，再向 M15 对齐，
        # 避免因子在 M15 帧上拒绝周度原始列而始终没有健康证据。
        pm_roll = df["cot_pm_net"].rolling(52, min_periods=12)
        raw_extreme = (
            (df["cot_mm_net_pct_oi"] - roll52.mean()) / roll52.std()
            - (df["cot_pm_net"] - pm_roll.mean()) / pm_roll.std()
        )
        extreme = np.zeros(len(df), dtype=float)
        extreme[raw_extreme > 1.5] = -1.0
        extreme[raw_extreme < -1.5] = 1.0
        extreme[raw_extreme.isna().to_numpy()] = np.nan
        df["cot_extreme_signal"] = extreme
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
        df.index = self._release_index(df["release_at"])
        df = df.drop(columns=["release_at"])
        return self._set_cached("cot_gold", self.db_path, df)

    def _load_events(self) -> pd.DataFrame:
        """加载 events, 列: date / fomc / nfp / cpi / pce (1=是事件日)"""
        cached = self._get_cached("events", self.events_db_path)
        if cached is not None:
            return cached
        with duckdb_readonly_connection(self.events_db_path) as con:
            rows = con.execute(
                "SELECT date, type FROM events ORDER BY date"
            ).fetchall()
        if not rows:
            return self._set_cached("events", self.events_db_path, pd.DataFrame())
        df = pd.DataFrame(rows, columns=["date", "type"])
        df["date"] = pd.to_datetime(df["date"])
        df["flag"] = 1
        pivot = df.pivot(index="date", columns="type", values="flag").fillna(0)
        pivot = pivot.reindex(columns=["FOMC", "NFP", "CPI", "PCE"], fill_value=0)
        return self._set_cached("events", self.events_db_path, pivot)

    def _compute_etf_derived(self, etf_holdings: pd.DataFrame) -> pd.DataFrame:
        """从原始 holdings 计算派生列: chg_5d / chg_20d / chg_pct."""
        if etf_holdings.empty:
            return pd.DataFrame()
        out = etf_holdings.copy()
        for sym in ["GLD", "SLV"]:
            tonnes_col = f"{sym}_tonnes"
            if tonnes_col in out.columns:
                # GLD 月度和 SLV 季度披露共用一条 release 时间轴，不能直接
                # 对含 NaN 的联合索引做 diff(20)，否则 20 代表混合披露行而
                # 且几乎总会落在另一个 ETF 的空值上。先在各自真实观测上
                # 计算，再回到联合时间轴供 PIT 对齐。
                source = out[tonnes_col].dropna()
                # 5d 变化 (吨)
                out[f"{sym}_tonnes_chg_5d"] = source.diff(5).reindex(out.index)
                # 20d 变化 (吨)
                out[f"{sym}_tonnes_chg_20d"] = source.diff(20).reindex(out.index)
                # 5d 变化百分比
                out[f"{sym}_tonnes_pct_5d"] = source.pct_change(5) * 100
                out[f"{sym}_tonnes_pct_5d"] = out[f"{sym}_tonnes_pct_5d"].reindex(out.index)
                # 20d 变化百分比
                out[f"{sym}_tonnes_pct_20d"] = source.pct_change(20) * 100
                out[f"{sym}_tonnes_pct_20d"] = out[f"{sym}_tonnes_pct_20d"].reindex(out.index)
                # 60d z-score (跟历史比较)
                roll60 = source.rolling(60, min_periods=20)
                zscore = (source - roll60.mean()) / roll60.std()
                out[f"{sym}_tonnes_zscore_60d"] = zscore.reindex(out.index)
        if {"GLD_tonnes", "SLV_tonnes"}.issubset(out.columns):
            # 银金持仓比只在两个来源都已经披露后计算。这里的 5 表示
            # 联合披露时间线上的 5 个有效观察，不把 M15 前向填充当成
            # 新的原始观察。
            holdings = out[["GLD_tonnes", "SLV_tonnes"]].sort_index().ffill()
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = holdings["SLV_tonnes"].div(holdings["GLD_tonnes"].replace(0, np.nan))
            ratio_change = ratio.pct_change(5, fill_method=None) * 100
            out["silver_gold_holdings_ratio"] = ratio_change.reindex(out.index)
        return out

    def _compute_etf_price_derived(self, etf: pd.DataFrame) -> pd.DataFrame:
        """Compute ETF price-derived columns on the daily release timeline."""
        if etf.empty:
            return pd.DataFrame()
        out = etf.copy()
        if "SLV" in out.columns and "GLD" in out.columns:
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = np.where(out["GLD"].to_numpy(dtype=float) != 0,
                                 out["SLV"].to_numpy(dtype=float) / out["GLD"].to_numpy(dtype=float),
                                 np.nan)
            ratio_s = pd.Series(ratio, index=out.index)
            out["slv_gld_ratio_5d"] = ratio_s.pct_change(5)
            out["slv_gld_ratio"] = out["slv_gld_ratio_5d"]
        return out

    def _compute_macro_derived(self, macro: pd.DataFrame) -> pd.DataFrame:
        """从日级宏观数据计算标准派生列，再 forward-fill 到 bar 级别。"""
        if macro.empty:
            return pd.DataFrame()
        out = macro.copy()
        if "real_yield_10y" not in out.columns:
            return out
        ry = out["real_yield_10y"]
        out["real_yield_chg_5d"] = ry.diff(5) * 100.0
        out["real_yield_chg"] = out["real_yield_chg_5d"]
        ranks: list[float] = []
        for i in range(len(ry)):
            hist = ry.iloc[max(0, i - 1260 + 1): i + 1].dropna()
            cur = ry.iloc[i]
            if pd.isna(cur) or len(hist) < 60:
                ranks.append(np.nan)
            else:
                ranks.append(float((hist <= cur).mean()))
        out["real_yield_pct_rank_5y"] = ranks
        out["real_yield_pct_rank"] = out["real_yield_pct_rank_5y"]
        return out

    def _compute_cb_derived(self, cb_gold: pd.DataFrame) -> pd.DataFrame:
        """从央行月度或季度数据派生统一的 3m/6m/12m 净买入."""
        if cb_gold.empty:
            return pd.DataFrame()
        out = cb_gold.copy()
        # WGC 的官方持有量序列按季度发布；旧适配器按月滚动会把一个
        # 季度变化错误地再累计三次。按观测/发布时间间隔选择窗口，
        # 让季度数据的 3m 直接对应单个季度变化。
        dates = pd.DatetimeIndex(out.index).sort_values()
        median_gap_days = 0.0
        if len(dates) > 1:
            gaps = pd.Series(dates).diff().dt.total_seconds().dropna() / 86_400.0
            median_gap_days = float(gaps.median()) if not gaps.empty else 0.0
        quarterly = median_gap_days >= 60.0
        windows = {"3m": 1, "6m": 2, "12m": 4} if quarterly else {"3m": 3, "6m": 6, "12m": 12}
        for country in ["china", "russia", "turkey", "india", "total"]:
            chg_col = f"cb_{country}_chg"
            if chg_col in out.columns:
                for label, window in windows.items():
                    out[f"cb_{country}_chg_{label}"] = out[chg_col].rolling(window, min_periods=1).sum()
        if "cb_china_chg_3m" in out.columns:
            roll = out["cb_china_chg_3m"].rolling(60, min_periods=10)
            out["cb_china_3m_zscore"] = (
                (out["cb_china_chg_3m"] - roll.mean()) / roll.std()
            )
        return out

    def _compute_event_hour_buckets(
        self,
        bar_index: pd.DatetimeIndex,
        events: pd.DataFrame,
    ) -> pd.DataFrame:
        """Compute signed event buckets: negative before event, positive after."""
        out = pd.DataFrame(index=bar_index)
        if events.empty or len(bar_index) == 0:
            return out
        event_index = pd.DatetimeIndex(events.index).tz_localize(None)
        for event_type in ["FOMC", "NFP", "CPI", "PCE"]:
            if event_type not in events.columns:
                continue
            mask = events[event_type].fillna(0).astype(int).to_numpy() == 1
            event_datetimes: list[pd.Timestamp] = []
            for date in event_index[mask]:
                time_str = self.event_times.get(event_type, "13:30")
                try:
                    event_datetimes.append(pd.Timestamp(f"{date.date()} {time_str}"))
                except Exception:
                    continue
            values = np.full(len(bar_index), np.nan)
            for i, ts in enumerate(bar_index):
                if not event_datetimes:
                    continue
                signed = [
                    (pd.Timestamp(ts) - event_dt).total_seconds() / 3600.0
                    for event_dt in event_datetimes
                ]
                values[i] = self._bucket_signed_event_hours(min(signed, key=lambda h: abs(h)))
            out[f"hours_to_{event_type.lower()}"] = values
        return out

    @staticmethod
    def _bucket_signed_event_hours(signed_hours: float) -> float:
        if not np.isfinite(signed_hours) or signed_hours < -48.0 or signed_hours > 48.0:
            return np.nan
        if -4.0 <= signed_hours <= 4.0:
            return 0.0
        if signed_hours < -24.0:
            return -48.0
        if signed_hours < 0.0:
            return -24.0
        if signed_hours <= 24.0:
            return 24.0
        return 48.0

    @staticmethod
    def _limit_as_of(df: pd.DataFrame, as_of_epoch: float | None) -> pd.DataFrame:
        if df.empty or as_of_epoch is None:
            return df
        as_of_dt = pd.to_datetime(as_of_epoch, unit="s", utc=True).tz_convert(None)
        return df[df.index <= as_of_dt]

    def align_to_bars(self, bar_df: pd.DataFrame, as_of: datetime | str | int | float | None = None) -> pd.DataFrame:
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

        as_of_epoch = self._as_epoch(as_of)
        sources = self._get_source_bundle(as_of_epoch)
        macro = sources["macro"]
        etf = sources["etf"]
        etf_holdings = sources["etf_holdings"]
        cb_gold = sources["cb_gold"]
        cot_gold = sources["cot_gold"]
        events = sources["events"]

        # 事件 0/1 化
        evt_cols = {}
        for c in ["FOMC", "NFP", "CPI", "PCE"]:
            if c in events.columns:
                evt_cols[f"evt_{c.lower()}"] = events[c]
        events_df = pd.DataFrame(evt_cols) if evt_cols else pd.DataFrame()

        # 合并到一个 df (按 release_at index for external data). Event flags
        # are aligned separately so they do not forward-fill beyond event day.
        ext = (macro
               .join(etf, how="outer")
               .join(etf_holdings, how="outer")
               .join(cb_gold, how="outer")
               .join(cot_gold, how="outer"))
        ext = ext.sort_index()

        bar_index = pd.DatetimeIndex(bar_df.index).tz_localize(None)
        out = pd.DataFrame(index=bar_df.index)

        if not ext.empty:
            # Reindex 到 bar 级别, 只能 forward-fill release_at 已经发生的数据。
            ext = ext.ffill()
            ext.index = pd.DatetimeIndex(ext.index).tz_localize(None)
            ext = ext.reindex(bar_index, method="ffill")
            ext.index = bar_df.index
            out = out.join(ext, how="left")

        if not events_df.empty:
            events_df.index = pd.DatetimeIndex(events_df.index).tz_localize(None).normalize()
            event_aligned = events_df.reindex(bar_index.normalize()).fillna(0)
            event_aligned.index = bar_df.index
            out = out.join(event_aligned, how="left")
            event_buckets = self._compute_event_hour_buckets(bar_index, events)
            if not event_buckets.empty:
                event_buckets.index = bar_df.index
                out = out.join(event_buckets, how="left")

        for c in out.columns:
            if c.startswith("evt_"):
                out[c] = out[c].fillna(0).astype(np.int8)

        return out


def align_external_to_bars(
    bar_df: pd.DataFrame,
    as_of: datetime | str | int | float | None = None,
    db_path: str | Path = DUCKDB_EXTERNAL,
    events_db_path: str | Path = DUCKDB_EVENTS,
) -> pd.DataFrame:
    return ExternalDataLoader(db_path=db_path, events_db_path=events_db_path).align_to_bars(bar_df, as_of=as_of)


if __name__ == "__main__":
    # 简单 sanity check
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from data.store import DataStore
    store = DataStore("data/ctrader_data.duckdb")
    bars = store.load_bars("XAUUSD+", "M15")
    print(f"Loaded {len(bars)} bars, range: {bars.index[0]} → {bars.index[-1]}")

    loader = ExternalDataLoader()
    ext = loader.align_to_bars(bars)
    print(f"\nExternal df shape: {ext.shape}")
    print(f"Columns ({len(ext.columns)}): {list(ext.columns)}")
    print(f"\n最近 5 行:")
    print(ext.tail())
    print(f"\n覆盖率 (非 NaN):")
    for c in ext.columns:
        pct = (1 - ext[c].isna().mean()) * 100
        print(f"  {c:<30s}  {pct:>6.1f}%")

