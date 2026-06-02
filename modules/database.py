"""modules/database.py — 兼容 shim, 2026-06-02 恢复

历史: 误删原 modules/database.py 后重建, 函数签名保持向后兼容。
被 scripts/{fetch_mt5_data,factor_mining,gp_interpret}.py 引用。

新代码请直接用 data.store.DataStore (OOP, 异步友好, 单实例)。
本 shim 仅用于过渡, 后续会重写 3 个老 scripts 去掉这个依赖。
"""
from __future__ import annotations

import pandas as pd
from data.store import DataStore

# 单例, 整个进程共享一个 sqlite 连接
_store = DataStore(db_path="data/market_data.db")


def init_database() -> None:
    """建表 (老 API, 实际 DataStore __init__ 已自动 init)。"""
    _store._init_db()


def insert_candles(df: pd.DataFrame, symbol_name: str, timeframe: str) -> int:
    """老 API: 接受 DataFrame, 转 list[dict] 后调 DataStore.insert_bars。"""
    bars = df.to_dict("records")
    if bars and "time" in bars[0]:
        # 老格式 'time' 列 (int epoch or str), DataStore 期望 dict 带 'time' 字段
        pass
    _store.insert_bars(bars, symbol_name, timeframe)
    return len(bars)


def load_candles(symbol: str, timeframe: str, start=None, end=None) -> pd.DataFrame:
    """老 API: 包装 DataStore.load_bars, 暂不支持 start/end 过滤 (DataStore 也没实现)。"""
    df = _store.load_bars(symbol, timeframe)
    if df.empty:
        return df
    if start is not None:
        df = df[df.index >= pd.Timestamp(start)]
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    return df


def get_time_range(symbol: str, timeframe: str) -> tuple:
    """返回 (min_time, max_time) — 新 DataStore 没原生实现, 从 load_bars 拿。"""
    df = _store.load_bars(symbol, timeframe)
    if df.empty:
        return (None, None)
    return (df.index.min(), df.index.max())


def candle_count(symbol: str, timeframe: str) -> int:
    """老 API: 包装 DataStore.bar_count。"""
    return _store.bar_count(symbol, timeframe)


def table_summary() -> list[tuple]:
    """老 API: 包装 DataStore.summary。"""
    return _store.summary()
