"""Shadow factor virtual performance evaluator.

This module gives Canary a real shadow-performance source without routing
shadow factors into live votes. It evaluates registered shadow/discovered
factors on recent bars, builds a simple one-bar-ahead virtual PnL stream, and
persists aggregate OOS metrics to the PostgreSQL state store.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import time
from dataclasses import asdict, dataclass
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

INVALID_FACTOR_ERROR_THRESHOLD = 3


def _connect_state(*, read_only: bool = False):
    from backend.core.db import get_state_pg_conn

    return get_state_pg_conn(read_only=read_only)


def _p(sql: str) -> str:
    return sql.replace("?", "%s")


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
    evidence_hash: str = ""
    dataset_hash: str = ""
    evidence_start_at: str = ""
    evidence_end_at: str = ""
    input_bars: int = 0
    new_evidence_bars: int = 0

    def to_metrics(self) -> dict:
        data = asdict(self)
        return data


def _ensure_shadow_tables() -> None:
    """Shadow tables are created by the PostgreSQL state migration."""
    return


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


def _canonical_marker(value) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return str(value)


def _evidence_markers(df: pd.DataFrame) -> list[str]:
    for column in ("timestamp", "time", "datetime", "open_time", "ts"):
        if column in df.columns:
            return [_canonical_marker(value) for value in df[column].tolist()]
    return [_canonical_marker(value) for value in df.index.tolist()]


def _count_new_markers(markers: list[str], previous_end: str) -> int:
    if not markers:
        return 0
    if not previous_end:
        return len(markers)
    positions = [idx for idx, marker in enumerate(markers) if marker == previous_end]
    if positions:
        return max(0, len(markers) - positions[-1] - 1)
    try:
        previous_ts = pd.Timestamp(previous_end)
        parsed = pd.to_datetime(pd.Series(markers), errors="coerce", utc=True)
        if not pd.isna(previous_ts):
            if previous_ts.tzinfo is None:
                previous_ts = previous_ts.tz_localize("UTC")
            else:
                previous_ts = previous_ts.tz_convert("UTC")
            count = int((parsed > previous_ts).sum())
            if count:
                return count
    except Exception:
        pass
    # Changed evidence without comparable timestamps still counts as one new
    # observation, never as an entire recycled historical window.
    return 1


def _record_shadow_error(
    factor: str,
    *,
    source: str = "shadow",
    symbol: str = "",
    timeframe: str = "",
    error: str = "",
) -> None:
    _ensure_shadow_tables()

    conn = _connect_state()
    try:
        row = conn.execute(
            _p("SELECT metrics_json FROM shadow_factor_perf WHERE factor = ?"),
            (factor,),
        ).fetchone()
        metrics = {}
        if row is not None:
            try:
                metrics = json.loads(row["metrics_json"] or "{}")
            except Exception:
                metrics = {}
        error_count = int(metrics.get("error_count", 0) or 0) + 1
        metrics.update({
            "factor": factor,
            "source": source,
            "symbol": symbol,
            "timeframe": timeframe,
            "status": "invalid",
            "error_count": error_count,
            "last_error": str(error or "")[:500],
            "last_error_at": time.time(),
        })
        conn.execute(
            _p("""
            INSERT INTO shadow_factor_perf
            (factor, source, symbol, timeframe, oos_bars, cumulative_pnl,
             hit_rate, max_drawdown, last_signal, metrics_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(factor) DO UPDATE SET
                source=excluded.source,
                symbol=excluded.symbol,
                timeframe=excluded.timeframe,
                oos_bars=excluded.oos_bars,
                cumulative_pnl=excluded.cumulative_pnl,
                hit_rate=excluded.hit_rate,
                max_drawdown=excluded.max_drawdown,
                last_signal=excluded.last_signal,
                metrics_json=excluded.metrics_json,
                updated_at=excluded.updated_at
            """),
            (
                factor,
                source,
                symbol,
                timeframe,
                0,
                0.0,
                0.0,
                0.0,
                0.0,
                json.dumps(metrics, ensure_ascii=False),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_invalid_shadow_factors(max_errors: int = INVALID_FACTOR_ERROR_THRESHOLD) -> list[dict]:
    _ensure_shadow_tables()

    conn = _connect_state(read_only=True)
    try:
        rows = conn.execute(
            "SELECT factor, source, symbol, timeframe, metrics_json, updated_at FROM shadow_factor_perf"
        ).fetchall()
    finally:
        conn.close()

    invalid = []
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except Exception:
            metrics = {}
        error_count = int(metrics.get("error_count", 0) or 0)
        if metrics.get("status") == "invalid" and error_count >= max_errors:
            invalid.append({
                "factor": row["factor"],
                "source": row["source"] or metrics.get("source", "shadow"),
                "symbol": row["symbol"] or metrics.get("symbol", ""),
                "timeframe": row["timeframe"] or metrics.get("timeframe", ""),
                "error_count": error_count,
                "last_error": str(metrics.get("last_error", "") or ""),
                "updated_at": float(row["updated_at"] or 0.0),
            })
    return invalid


def evaluate_factor(
    df: pd.DataFrame,
    factor: str,
    fn,
    *,
    source: str = "shadow",
    symbol: str = "",
    timeframe: str = "",
    threshold: float = 0.3,
    previous_perf: ShadowPerf | None = None,
) -> ShadowPerf | None:
    """Evaluate one factor as a one-bar-ahead virtual strategy."""
    if df is None or len(df) < 30 or "close" not in df.columns:
        return None

    try:
        raw = fn(df)
    except Exception as exc:
        logger.warning("[shadow] factor %s failed: %s", factor, exc)
        _record_shadow_error(
            factor,
            source=source,
            symbol=symbol,
            timeframe=timeframe,
            error=str(exc),
        )
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
    markers = _evidence_markers(df)
    dataset_hasher = hashlib.sha256()
    dataset_hasher.update(str(symbol).encode("utf-8"))
    dataset_hasher.update(str(timeframe).encode("utf-8"))
    dataset_hasher.update("\x1f".join(markers).encode("utf-8"))
    dataset_hasher.update(np.nan_to_num(closes, nan=0.0, posinf=0.0, neginf=0.0).tobytes())
    dataset_hash = dataset_hasher.hexdigest()
    evidence_hasher = hashlib.sha256()
    evidence_hasher.update(dataset_hash.encode("ascii"))
    evidence_hasher.update(str(factor).encode("utf-8"))
    evidence_hasher.update(np.nan_to_num(positions, nan=0.0).tobytes())
    evidence_hasher.update(np.nan_to_num(pnl, nan=0.0, posinf=0.0, neginf=0.0).tobytes())
    evidence_hash = evidence_hasher.hexdigest()
    if previous_perf is not None and previous_perf.evidence_hash == evidence_hash:
        new_evidence_bars = 0
    else:
        new_evidence_bars = _count_new_markers(
            markers,
            previous_perf.evidence_end_at if previous_perf is not None else "",
        )
        if (
            previous_perf is not None
            and new_evidence_bars == 0
            and previous_perf.dataset_hash != dataset_hash
            and isinstance(df.index, pd.RangeIndex)
        ):
            new_evidence_bars = 1

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
        evidence_hash=evidence_hash,
        dataset_hash=dataset_hash,
        evidence_start_at=markers[0] if markers else "",
        evidence_end_at=markers[-1] if markers else "",
        input_bars=n,
        new_evidence_bars=new_evidence_bars,
    )


def persist_shadow_perf(perf: ShadowPerf) -> None:
    _ensure_shadow_tables()

    conn = _connect_state()
    try:
        conn.execute(
            _p("""
            INSERT INTO shadow_factor_perf
            (factor, source, symbol, timeframe, oos_bars, cumulative_pnl,
             hit_rate, max_drawdown, last_signal, metrics_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(factor) DO UPDATE SET
                source=excluded.source,
                symbol=excluded.symbol,
                timeframe=excluded.timeframe,
                oos_bars=excluded.oos_bars,
                cumulative_pnl=excluded.cumulative_pnl,
                hit_rate=excluded.hit_rate,
                max_drawdown=excluded.max_drawdown,
                last_signal=excluded.last_signal,
                metrics_json=excluded.metrics_json,
                updated_at=excluded.updated_at
            """),
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

    conn = _connect_state(read_only=True)
    try:
        row = conn.execute(
            _p("SELECT * FROM shadow_factor_perf WHERE factor = ?"),
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
        evidence_hash=str(metrics.get("evidence_hash") or ""),
        dataset_hash=str(metrics.get("dataset_hash") or ""),
        evidence_start_at=str(metrics.get("evidence_start_at") or ""),
        evidence_end_at=str(metrics.get("evidence_end_at") or ""),
        input_bars=int(metrics.get("input_bars") or 0),
        new_evidence_bars=int(metrics.get("new_evidence_bars") or 0),
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
        previous_perf = None
        if persist:
            try:
                previous_perf = load_shadow_perf(name)
            except Exception:
                logger.debug("[shadow] previous evidence unavailable for %s", name, exc_info=True)
        perf = evaluate_factor(
            df,
            name,
            fn,
            source=source,
            symbol=symbol,
            timeframe=timeframe,
            previous_perf=previous_perf,
        )
        if perf is None:
            continue
        results[name] = perf
        if persist:
            persist_shadow_perf(perf)

    return results
