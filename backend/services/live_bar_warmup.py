"""Bar warmup, cache and decision-bar freshness outside the live-service facade.

Extracted from backend.services.live_service (2026-09-12 structural repair).
Shared loop-owned state resolves lazily via _live_service().
"""
from __future__ import annotations

_ls_module = None


def _live_service():
    """Lazy handle to the live loop module (import-order-safe)."""
    global _ls_module
    if _ls_module is None:
        from backend.services import live_service as _module
        _ls_module = _module
    return _ls_module


from backend.core.retry import TransientError, retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from backend.services.live_data_sync_helpers import (
    classify_decision_bar_freshness as _sync_classify_decision_bar_freshness,
)
from typing import Any
import pandas as pd
import time


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=4),
    retry=retry_if_exception_type(Exception),
    reraise=False,
    before_sleep=lambda a, e, d: _live_service().logger.warning(f"warmup_from_local_db attempt {a}/3 failed: {e}, retrying in {d:.1f}s"),
)
def warmup_from_local_db(symbol: str = "XAUUSD+", timeframe: str = "M15", n_bars: int = 200) -> "pd.DataFrame | None":
    from backend.core.db import (
        bars_monthly_read_paths,
        duckdb_readonly_connection,
    )
    db_paths = bars_monthly_read_paths(newest_first=True)
    target_bars = int(n_bars)
    remaining = target_bars
    frames = []
    for db_path in db_paths:
        if remaining <= 0:
            break
        with duckdb_readonly_connection(
            str(db_path), snapshot_first=True
        ) as conn:
            frame = conn.execute(
                "SELECT time, open, high, low, close, volume "
                "FROM bars WHERE symbol=? AND timeframe=? "
                "ORDER BY time DESC LIMIT ?",
                [symbol, timeframe, remaining],
            ).df()
        if frame is not None and len(frame) > 0:
            frames.append(frame)
            remaining -= len(frame)
    if not frames:
        _live_service().logger.warning(f"DuckDB has no bars for {symbol} {timeframe}")
        return None
    df = pd.concat(frames, ignore_index=True)
    df = (
        df.drop_duplicates(subset=["time"], keep="first")
        .sort_values("time")
        .tail(target_bars)
    )
    # time 是 epoch 秒, 转 datetime index
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=8),
    retry=retry_if_exception_type(Exception),
    reraise=False,
    before_sleep=lambda a, e, d: _live_service().logger.warning(f"fetch_bars attempt {a}/3 failed: {e}, retrying in {d:.1f}s"),
)
def fetch_bars_with_retry(bridge, timeframe: str, n_bars: int, max_retries: int = 3) -> "pd.DataFrame | None":
    """fetch_bars 重试 wrapper. 失败 1 次不致命, 指数 backoff 2s/4s/8s.
    返 None 表示彻底失败 (调用方决定是否继续).
    Startup-only historical seed.  Runtime ticks consume the bridge's
    in-memory live trendbar feed and never call this wrapper.
    保留 idempotency 边界：单次 fetch 幂等，重试仅对 TransientError。
    """
    df = bridge.fetch_bars(timeframe=timeframe, n_bars=n_bars)
    if df is not None and len(df) >= 30:
        return df
    # 数据不足视为瞬时失败，触发重试
    if df is None or len(df) < 30:
        raise TransientError(f"insufficient bars: got {0 if df is None else len(df)}")
    return df


def df_latest_epoch(df: "pd.DataFrame | None") -> float:
    if df is None or len(df) == 0:
        return 0.0
    try:
        idx = df.index[-1]
        return float(idx.timestamp()) if hasattr(idx, "timestamp") else float(idx)
    except Exception:
        return 0.0


def closed_decision_bar_frame(
    df: "pd.DataFrame | None",
    *,
    timeframe: str,
    now_ts: float,
) -> "pd.DataFrame | None":
    if df is None or len(df) == 0:
        return df
    freshness = _sync_classify_decision_bar_freshness(
        latest_ts=df_latest_epoch(df),
        timeframe=timeframe,
        now=now_ts,
    )
    expected_ts = float(freshness.get("expected_closed_bar_ts", 0.0) or 0.0)
    if expected_ts <= 0:
        return df
    try:
        keep = []
        for idx in df.index:
            ts = float(idx.timestamp()) if hasattr(idx, "timestamp") else float(idx)
            keep.append(ts <= expected_ts)
        filtered = df.loc[keep]
        return filtered if filtered is not None and len(filtered) > 0 else df.iloc[0:0]
    except Exception:
        return df


def _clamp_last_closed_bar_close_to_spot(
    df: "pd.DataFrame | None",
    *,
    timeframe: str,
    now_ts: float,
    quote_provider: Any = None,
) -> tuple["pd.DataFrame | None", dict[str, Any]]:
    """Clamp the just-closed bar's stream close toward the live spot mid.

    Spot-stream trendbar frames report ``close`` as the frame-moment price,
    so when a bar's final stream update never arrives the cached close
    freezes wherever the last frame caught the tape (observed close==low on
    2026-08-24).  Within seconds of the bar close the continuous bid/ask
    mid is the best in-memory estimate of that bar's true close, so this
    clamps only the newest closed bar and only while the estimate is fresh
    and sane.
    """
    info: dict[str, Any] = {}
    if df is None or getattr(df, "empty", False) or len(df) == 0:
        return df, info
    if not callable(quote_provider):
        return df, info
    freshness = _sync_classify_decision_bar_freshness(
        latest_ts=df_latest_epoch(df),
        timeframe=timeframe,
        now=now_ts,
    )
    expected_ts = float(freshness.get("expected_closed_bar_ts", 0.0) or 0.0)
    if expected_ts <= 0:
        return df, info
    try:
        last_idx = df.index[-1]
        last_ts = (
            float(last_idx.timestamp())
            if hasattr(last_idx, "timestamp")
            else float(last_idx)
        )
    except Exception:
        return df, info
    if abs(last_ts - expected_ts) > 1e-6:
        # Newest row is not the just-closed bar; nothing to clamp.
        return df, info
    try:
        last_low = float(df["low"].iloc[-1])
        last_high = float(df["high"].iloc[-1])
        last_close = float(df["close"].iloc[-1])
    except Exception:
        return df, info
    degenerate = abs(last_close - last_low) < 1e-9 and last_high > last_low
    if not degenerate:
        return df, info
    period_seconds = max(1, _live_service()._timeframe_seconds(timeframe))
    try:
        quote = quote_provider() or {}
    except Exception:
        return df, info
    bid = float(quote.get("bid") or 0.0)
    ask = float(quote.get("ask") or 0.0)
    quote_ts = float(quote.get("ts") or 0.0)
    if bid <= 0.0 or ask <= 0.0 or ask < bid:
        info["close_clamp_skipped"] = "quote_unusable"
        return df, info
    bar_close_time = expected_ts + period_seconds
    if quote_ts > 0.0 and quote_ts < bar_close_time - 2.0:
        info["close_clamp_skipped"] = "quote_predates_close"
        return df, info
    mid = round((bid + ask) / 2.0, 5)
    span = max(1e-9, last_high - last_low)
    tolerance = max(0.5 * span, 10.0 * (last_high + last_low) * 1e-6)
    if not (last_low - tolerance <= mid <= last_high + tolerance):
        info["close_clamp_skipped"] = "mid_outside_bar_range"
        return df, info
    out = df.copy()
    out.loc[last_idx, "close"] = mid
    info.update(
        {
            "close_clamp_applied": True,
            "close_clamp_from": last_close,
            "close_clamp_price": mid,
            "close_clamp_bar_ts": last_ts,
        }
    )
    return out, info


def _decision_bar_freshness_snapshot(
    df: "pd.DataFrame | None",
    *,
    timeframe: str,
    now_ts: float,
) -> dict[str, Any]:
    snapshot = _sync_classify_decision_bar_freshness(
        latest_ts=df_latest_epoch(df),
        timeframe=timeframe,
        now=now_ts,
    )
    snapshot.setdefault("source", "live_decision_bar")
    return snapshot


def _record_decision_bar_freshness(snapshot: dict[str, Any]) -> None:
    try:
        _live_service().live_state_update(decision_bar_freshness=dict(snapshot or {}))
    except Exception:
        _live_service().logger.debug("[live] decision bar freshness snapshot update failed", exc_info=True)


def ensure_live_decision_bars_fresh(
    *,
    bridge: Any,
    symbol: str,
    timeframe: str,
    df_new: "pd.DataFrame",
    tick: int,
    log,
    market_session: dict[str, Any] | None = None,
) -> "pd.DataFrame":
    # The serial live owner consumes only the bridge's in-memory live
    # trendbar frame.  Historical RPCs and durable DuckDB writes stay outside
    # this boundary so a slow broker history call cannot starve Safety.
    now_ts = time.time()
    closed_df = closed_decision_bar_frame(df_new, timeframe=timeframe, now_ts=now_ts)
    clamp_info: dict[str, Any] = {}
    try:
        # Snapshot the spot quote before the bridge reference is dropped; the
        # clamp itself stays in-memory (no broker/history RPC in this path).
        quote_snapshot: Any = None
        if bridge is not None and hasattr(bridge, "get_spot_quote"):
            try:
                quote_snapshot = bridge.get_spot_quote()
            except Exception:
                quote_snapshot = None
        del bridge
        closed_df, clamp_info = _clamp_last_closed_bar_close_to_spot(
            closed_df,
            timeframe=timeframe,
            now_ts=now_ts,
            quote_provider=(lambda q=quote_snapshot: q) if quote_snapshot else None,
        )
    except Exception:
        _live_service().logger.debug("[live] decision bar close clamp failed", exc_info=True)
        clamp_info = {}
    snapshot = _decision_bar_freshness_snapshot(closed_df, timeframe=timeframe, now_ts=now_ts)
    if bool(snapshot.get("fresh", False)):
        snapshot.update(
            {
                "repair_attempted": False,
                "repair_status": "fresh",
                "source": "ctrader_live_trendbar",
            }
        )
        snapshot.update(clamp_info)
        _record_decision_bar_freshness(snapshot)
        return closed_df if closed_df is not None and len(closed_df) > 0 else df_new

    repair_suppressed = ""
    try:
        from backend.services.market_session import maintenance_wait_evidence
        from config.runtime_config import shared as _runtime_cfg

        # Production passes the already computed session snapshot.  Do not
        # perform another broker/history read when the online bar is stale.
        session = dict(market_session or {})
        session_status = str(session.get("status") or "")
        if session_status in {
            "closed_confirmed",
            "closed_pending_confirmation",
            "closed_pending_positions",
        }:
            repair_suppressed = "market_closed"
        else:
            maintenance = maintenance_wait_evidence(
                session,
                latest_market_data_ts=float(snapshot.get("latest_bar_ts", 0.0) or 0.0),
                now_ts=now_ts,
                grace_seconds=float(_runtime_cfg().market_open_pending_quote_grace_seconds),
            )
            if maintenance["active"]:
                repair_suppressed = "maintenance_wait"
    except Exception:
        _live_service().logger.debug("[live] decision bar repair market-session check failed", exc_info=True)

    snapshot.update(
        {
            "repair_attempted": False,
            "repair_status": repair_suppressed or "stale_waiting_for_live_trendbar",
            "repair_inserted_bars": 0,
            "repair_latest_bar_ts": 0.0,
            "repair_error": "",
            "source": (
                "live_trendbar_repair_suppressed"
                if repair_suppressed
                else "ctrader_live_trendbar_read_only"
            ),
        }
    )
    _record_decision_bar_freshness(snapshot)
    try:
        log(
            f"tick {tick}: online trendbars stale; alpha waits for cTrader live feed "
            f"{symbol} {timeframe} latest={snapshot.get('latest_bar_ts', 0):.0f} "
            f"expected={snapshot.get('expected_closed_bar_ts', 0):.0f} "
            f"status={snapshot.get('repair_status')}"
        )
    except Exception:
        pass
    for frame in (closed_df, df_new):
        if frame is not None:
            try:
                return frame.iloc[0:0]
            except Exception:
                break
    return None


def save_bar_cache(df: "pd.DataFrame") -> None:
    """将 warmup 成功的 bar 缓存到 pickle 文件, 供下次启动 fallback."""
    try:
        _live_service()._BAR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        df.to_pickle(str(_live_service()._BAR_CACHE_PATH))
        _live_service().logger.info(f"[bar_cache] saved {len(df)} bars to {_live_service()._BAR_CACHE_PATH.name}")
    except Exception as e:
        _live_service().logger.warning(f"[bar_cache] save failed: {e}")


def load_bar_cache() -> "pd.DataFrame | None":
    """从 pickle 读取备份 bar 缓存."""
    try:
        if not _live_service()._BAR_CACHE_PATH.exists():
            return None
        df = pd.read_pickle(str(_live_service()._BAR_CACHE_PATH))
        if df is not None and len(df) >= 30:
            age_hours = (time.time() - _live_service()._BAR_CACHE_PATH.stat().st_mtime) / 3600
            _live_service().logger.info(f"[bar_cache] loaded {len(df)} bars (age={age_hours:.1f}h) "
                        f"last close={df['close'].iloc[-1]:.2f}")
            return df
    except Exception as e:
        _live_service().logger.warning(f"[bar_cache] load failed: {e}")
    return None
