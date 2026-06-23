"""Shadow factor virtual performance evaluator.

This module gives Canary a real shadow-performance source without routing
shadow factors into live votes. It evaluates registered shadow/discovered
factors on recent bars, builds a simple one-bar-ahead virtual PnL stream, and
persists aggregate OOS metrics to state.db.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ShadowPerf:
    factor: str
    source: str
    symbol: str
    timeframe: str
    oos_bars: int
    cumulative_pnl: float
    hit_rate: float
    max_drawdown: float
    last_signal: float
    n_valid: int
    n_active: int

    def to_metrics(self) -> dict:
        data = asdict(self)
        return data


def _ensure_shadow_tables() -> None:
    """Create shadow performance tables if an older state.db is already present."""
    from backend.core.db import get_state_conn

    conn = get_state_conn()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS shadow_factor_perf (
                factor TEXT PRIMARY KEY,
                source TEXT DEFAULT 'shadow',
                symbol TEXT DEFAULT '',
                timeframe TEXT DEFAULT '',
                oos_bars INTEGER DEFAULT 0,
                cumulative_pnl REAL DEFAULT 0.0,
                hit_rate REAL DEFAULT 0.0,
                max_drawdown REAL DEFAULT 0.0,
                last_signal REAL DEFAULT 0.0,
                metrics_json TEXT DEFAULT '{}',
                updated_at REAL DEFAULT 0.0
            );
            CREATE TABLE IF NOT EXISTS shadow_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                factor TEXT NOT NULL,
                symbol TEXT DEFAULT '',
                timeframe TEXT DEFAULT '',
                ts REAL,
                signal REAL DEFAULT 0.0,
                position INTEGER DEFAULT 0,
                pnl REAL DEFAULT 0.0,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_shadow_trades_factor_ts
                ON shadow_trades(factor, ts);
            CREATE INDEX IF NOT EXISTS idx_shadow_factor_perf_updated
                ON shadow_factor_perf(updated_at);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _as_array(values, n: int) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.flags.writeable is False:
        arr = arr.copy()
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    if len(arr) < n:
        arr = np.pad(arr, (n - len(arr), 0), constant_values=np.nan)
    elif len(arr) > n:
        arr = arr[-n:]
    arr[~np.isfinite(arr)] = np.nan
    return arr


def _signals_from_values(values: np.ndarray, window: int = 50) -> np.ndarray:
    """Map raw factor values to [-1, 1] using rolling z-score + tanh."""
    s = pd.Series(values)
    min_periods = max(10, min(30, window // 2))
    mean = s.rolling(window, min_periods=min_periods).mean()
    std = s.rolling(window, min_periods=min_periods).std()
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (s - mean) / std.replace(0, np.nan)
    sig = np.tanh(z.to_numpy(dtype=float))
    sig[~np.isfinite(sig)] = 0.0
    return sig


def _max_drawdown(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    return float(np.max(dd)) if len(dd) else 0.0


def evaluate_factor(
    df: pd.DataFrame,
    factor: str,
    fn,
    *,
    source: str = "shadow",
    symbol: str = "",
    timeframe: str = "",
    threshold: float = 0.3,
) -> ShadowPerf | None:
    """Evaluate one factor as a one-bar-ahead virtual strategy."""
    if df is None or len(df) < 30 or "close" not in df.columns:
        return None

    try:
        raw = fn(df)
    except Exception as exc:
        logger.debug("[shadow] factor %s failed: %s", factor, exc)
        return None

    n = len(df)
    values = _as_array(raw, n)
    signals = _signals_from_values(values)
    closes = df["close"].to_numpy(dtype=float)
    if len(closes) < 2:
        return None

    with np.errstate(divide="ignore", invalid="ignore"):
        fwd_returns = (closes[1:] - closes[:-1]) / closes[:-1]
    fwd_returns[~np.isfinite(fwd_returns)] = 0.0

    usable_signals = signals[:-1]
    positions = np.where(np.abs(usable_signals) >= threshold, np.sign(usable_signals), 0.0)
    pnl = positions * fwd_returns
    valid_mask = np.isfinite(pnl)
    active_mask = valid_mask & (positions != 0)
    active_pnl = pnl[active_mask]

    n_active = int(np.sum(active_mask))
    if n_active == 0:
        cumulative = 0.0
        hit_rate = 0.0
        max_dd = 0.0
    else:
        cumulative = float(np.sum(active_pnl))
        hit_rate = float(np.mean(active_pnl > 0))
        equity = np.cumsum(active_pnl)
        max_dd = _max_drawdown(equity)

    return ShadowPerf(
        factor=factor,
        source=source,
        symbol=symbol,
        timeframe=timeframe,
        oos_bars=n_active,
        cumulative_pnl=round(cumulative, 8),
        hit_rate=round(hit_rate, 6),
        max_drawdown=round(max_dd, 8),
        last_signal=float(signals[-1]) if len(signals) else 0.0,
        n_valid=int(np.sum(valid_mask)),
        n_active=n_active,
    )


def persist_shadow_perf(perf: ShadowPerf) -> None:
    _ensure_shadow_tables()
    from backend.core.db import get_state_conn

    conn = get_state_conn()
    try:
        conn.execute(
            """
            INSERT OR REPLACE INTO shadow_factor_perf
            (factor, source, symbol, timeframe, oos_bars, cumulative_pnl,
             hit_rate, max_drawdown, last_signal, metrics_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                perf.factor,
                perf.source,
                perf.symbol,
                perf.timeframe,
                perf.oos_bars,
                perf.cumulative_pnl,
                perf.hit_rate,
                perf.max_drawdown,
                perf.last_signal,
                json.dumps(perf.to_metrics(), ensure_ascii=False),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_shadow_perf(factor: str) -> ShadowPerf | None:
    _ensure_shadow_tables()
    from backend.core.db import get_state_conn

    conn = get_state_conn()
    try:
        row = conn.execute(
            "SELECT * FROM shadow_factor_perf WHERE factor = ?",
            (factor,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    metrics = {}
    try:
        metrics = json.loads(row["metrics_json"] or "{}")
    except Exception:
        metrics = {}
    return ShadowPerf(
        factor=row["factor"],
        source=row["source"] or metrics.get("source", "shadow"),
        symbol=row["symbol"] or metrics.get("symbol", ""),
        timeframe=row["timeframe"] or metrics.get("timeframe", ""),
        oos_bars=int(row["oos_bars"] or 0),
        cumulative_pnl=float(row["cumulative_pnl"] or 0.0),
        hit_rate=float(row["hit_rate"] or 0.0),
        max_drawdown=float(row["max_drawdown"] or 0.0),
        last_signal=float(row["last_signal"] or 0.0),
        n_valid=int(metrics.get("n_valid", row["oos_bars"] or 0)),
        n_active=int(metrics.get("n_active", row["oos_bars"] or 0)),
    )


def evaluate_shadow_factors(
    df: pd.DataFrame,
    *,
    symbol: str = "",
    timeframe: str = "",
    sources: Iterable[str] = ("shadow", "discovered"),
    persist: bool = True,
) -> dict[str, ShadowPerf]:
    """Evaluate all registered shadow/discovered factors and optionally persist."""
    from alpha.registry import factor_registry
    from alpha.registry_adapter import RegistryAdapter

    allowed = set(sources)
    adapter = RegistryAdapter.shared()
    results: dict[str, ShadowPerf] = {}

    for name, meta in list(adapter._meta.items()):
        source = str(meta.get("source", ""))
        if source not in allowed:
            continue
        fn = factor_registry.get(name)
        if fn is None:
            continue
        perf = evaluate_factor(
            df,
            name,
            fn,
            source=source,
            symbol=symbol,
            timeframe=timeframe,
        )
        if perf is None:
            continue
        results[name] = perf
        if persist:
            persist_shadow_perf(perf)

    return results
