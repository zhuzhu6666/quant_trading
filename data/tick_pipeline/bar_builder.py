"""
data/tick_pipeline/bar_builder.py — Tick → Bar 构建器 (Phase 6.3)

从 DuckDB ticks 表读取原始 tick 数据，构建 OHLCV bar。
支持多周期输出 (M1/M5/M15/M30/H1)。

用法:
    builder = TickBarBuilder(timeframe="M1")
    bars = builder.build_from_ticks(ticks_df, base_time=...)
    # 或从 DuckDB 直接读取:
    bars = builder.build_from_db("XAUUSD+", start_time, end_time)
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from backend.core.db import connect_duckdb

logger = logging.getLogger(__name__)

# 周期 → 秒
TF_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}


class TickBarBuilder:
    """Tick → Bar 构建器

    将原始 tick 数据聚合为 OHLCV bar。
    支持从 DataFrame 或 DuckDB 直接读取。
    """

    def __init__(self, timeframe: str = "M1"):
        self.timeframe = timeframe
        self.bar_seconds = TF_SECONDS.get(timeframe, 60)

    # ── 从 DataFrame 构建 ────────────────────────────────

    def build(self, ticks: pd.DataFrame) -> pd.DataFrame:
        """从 tick DataFrame 构建 OHLCV bar

        Args:
            ticks: DataFrame with columns [time, bid, ask, last, volume, flags]
                   time 列应为 epoch 秒 (float 或 int)

        Returns:
            DataFrame with columns [open, high, low, close, volume] indexed by bar time (epoch)
        """
        if ticks.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        df = ticks.copy()

        # 确保时间列为数值
        if not pd.api.types.is_numeric_dtype(df["time"]):
            df["time"] = pd.to_datetime(df["time"]).astype("int64") // 10**9

        # 计算 mid 价 (MT5 ticks 没有 last 字段，用 (bid+ask)/2)
        has_last = "last" in df.columns and (df["last"] > 0).any()
        has_bid_ask = "bid" in df.columns and "ask" in df.columns

        if has_last:
            df["price"] = df["last"]
        elif has_bid_ask:
            df["price"] = (df["bid"] + df["ask"]) / 2
        else:
            logger.warning("No usable price column in tick data")
            return pd.DataFrame()

        df["price"] = df["price"].astype(float)

        # bar 时间桶 (向下取整到 bar 边界，支持亚秒精度)
        bar_sec = float(self.bar_seconds)
        df["bar_time"] = (df["time"] // bar_sec) * bar_sec

        # 按 bar_time 分组聚合
        bars = df.groupby("bar_time").agg(
            open=("price", "first"),
            high=("price", "max"),
            low=("price", "min"),
            close=("price", "last"),
            volume=("volume", "sum"),
            tick_count=("price", "count"),
        )

        bars.index.name = "time"
        return bars

    # ── 从 DuckDB 读取并构建 ─────────────────────────────

    def build_from_db(self, symbol: str,
                      start_time: float | None = None,
                      end_time: float | None = None,
                      limit: int = 500000) -> pd.DataFrame:
        """从 DuckDB ticks 表读取并构建 bar

        Args:
            symbol: 品种
            start_time: 起始 epoch 秒
            end_time: 结束 epoch 秒
            limit: 最多读取 tick 数
        """
        from data.store import DataStore
        ds = DataStore()

        # load_ticks 需要 DuckDB store 支持
        # 直接用 duckdb 读取
        conn = connect_duckdb(ds._backend.db_path, read_only=True)
        try:
            query = "SELECT * FROM ticks WHERE symbol = ?"
            params = [symbol]
            if start_time is not None:
                query += " AND time >= ?"
                params.append(start_time)
            if end_time is not None:
                query += " AND time <= ?"
                params.append(end_time)
            query += " ORDER BY time ASC LIMIT ?"
            params.append(limit)

            ticks_df = conn.execute(query, params).df()
        finally:
            conn.close()

        if ticks_df.empty:
            return pd.DataFrame()

        return self.build(ticks_df)

    # ── 多周期构建 ──────────────────────────────────────

    @staticmethod
    def build_multi_timeframe(ticks: pd.DataFrame,
                              timeframes: list[str] = None) -> dict[str, pd.DataFrame]:
        """一次构建多个周期的 bar"""
        if timeframes is None:
            timeframes = ["M1", "M5", "M15", "M30", "H1"]

        results = {}
        for tf in timeframes:
            builder = TickBarBuilder(tf)
            bars = builder.build(ticks)
            if not bars.empty:
                results[tf] = bars

        return results

    # ── 存储 bar 到 DB ──────────────────────────────────

    def store_bars(self, bars: pd.DataFrame, symbol: str):
        """将构建的 bar 存入 DataStore"""
        from data.store import DataStore
        ds = DataStore()

        bar_dicts = []
        for idx, row in bars.iterrows():
            bar_dicts.append({
                "time": int(idx),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })

        if bar_dicts:
            ds.insert_bars(bar_dicts, symbol, self.timeframe)
            logger.info(
                f"[BarBuilder] {symbol} {self.timeframe}: "
                f"built {len(bar_dicts)} bars from ticks"
            )

