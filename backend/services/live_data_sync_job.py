"""Data sync scheduler job builder for the live backend."""

from __future__ import annotations

import time
from typing import Any, Callable

from backend.services.live_data_sync_helpers import (
    BAR_FRESHNESS_THRESHOLDS,
    classify_bar_freshness,
    dataframe_to_store_bars,
    timeframe_seconds,
)


def _default_health_factory():
    from data.live_sync.health import SyncHealth

    return SyncHealth.shared()


def _default_config_factory():
    from config.runtime_config import shared as runtime_config_shared

    return runtime_config_shared()


def _default_duckdb_runtime():
    from backend.core.db import DUCKDB_BARS, duckdb_readonly_connection

    return DUCKDB_BARS, duckdb_readonly_connection


def _default_data_store_factory():
    from data.store import DataStore

    return DataStore()


def _enabled_symbols(cfg) -> list[str]:
    return list(cfg.enabled_symbols) if hasattr(cfg, "enabled_symbols") else ["XAUUSD+"]


def _reconcile_live_trendbars(bridge: Any, symbol: str, timeframe: str, bars: list[dict[str, Any]], now_ts: float) -> int:
    """Feed broker-history rows back into the bridge's live trendbar cache.

    Spot-stream frames can leave a just-closed bar with close==low (observed
    2026-08-24).  The history rows written to DuckDB here are authoritative,
    so replaying the fully-closed ones into memory repairs decisions and
    review context without extra broker requests.
    """
    reconciler = getattr(bridge, "reconcile_live_bars", None)
    if not callable(reconciler):
        return 0
    period_seconds = max(1, timeframe_seconds(timeframe))
    closed_rows = [
        bar
        for bar in bars
        if float(bar.get("time") or 0.0) + period_seconds <= now_ts
    ]
    if not closed_rows:
        return 0
    try:
        return int(reconciler(timeframe, closed_rows) or 0)
    except Exception as exc:
        logger.debug("[data_sync] {} {} live trendbar reconcile failed: {}", symbol, timeframe, exc)
        return 0


def make_data_sync_job(
    *,
    lock,
    logger,
    get_ctrader: Callable[[], tuple[Any, str | None, bool]],
    market_session_snapshot: Callable[[Any], dict[str, Any] | None],
    health_factory: Callable[[], Any] = _default_health_factory,
    config_factory: Callable[[], Any] = _default_config_factory,
    duckdb_runtime_factory: Callable[[], tuple[Any, Callable[..., Any]]] = _default_duckdb_runtime,
    data_store_factory: Callable[[], Any] = _default_data_store_factory,
    now_fn: Callable[[], float] = time.time,
):
    """Build the legacy data_sync job with injectable IO dependencies."""

    retry_state: dict[str, dict[str, float]] = {}

    def _data_sync():
        """检查 bars 新鲜度，有缺口时通过主 bridge 回补。"""
        if not lock.acquire(blocking=False):
            logger.warning("[data_sync] previous run still active, skip overlapping trigger")
            return
        t0 = now_fn()
        health = health_factory()
        try:
            cfg = config_factory()
            symbols = _enabled_symbols(cfg)
            now = now_fn()
            duckdb_bars, duckdb_readonly_connection = duckdb_runtime_factory()

            # 1. 检查 bar 新鲜度: 各周期最新 bar 时间 vs 预期阈值
            latest_bar_ts_by_tf: dict[str, float] = {}
            with duckdb_readonly_connection(duckdb_bars, snapshot_first=True) as bar_conn:
                for tf in BAR_FRESHNESS_THRESHOLDS:
                    try:
                        row = bar_conn.execute(
                            "SELECT MAX(time) FROM bars WHERE symbol=? AND timeframe=?",
                            [symbols[0], tf],
                        ).fetchone()
                        latest_bar_ts_by_tf[tf] = float(row[0]) if row and row[0] else 0.0
                    except Exception:
                        latest_bar_ts_by_tf[tf] = 0.0
            bar_freshness = classify_bar_freshness(latest_bar_ts_by_tf, now=now)
            stale_tfs = bar_freshness["stale_tfs"]
            fresh_tfs = bar_freshness["fresh_tfs"]
            observed_bar_ts_by_tf = bar_freshness["observed_bar_ts_by_tf"]
            missing_closed_bars_by_tf = bar_freshness[
                "missing_closed_bars_by_tf"
            ]
            deferred_tfs: list[str] = []
            eligible_stale_tfs: list[str] = []
            for tf in stale_tfs:
                retry = retry_state.get(tf) or {}
                same_observation = float(
                    retry.get("observed_bar_ts") or 0.0
                ) == float(observed_bar_ts_by_tf.get(tf) or 0.0)
                if (
                    same_observation
                    and now < float(retry.get("next_retry_at") or 0.0)
                ):
                    deferred_tfs.append(tf)
                else:
                    eligible_stale_tfs.append(tf)
            stale_tfs = eligible_stale_tfs

            # 2. 日志: 数据健康摘要
            bar_status = f"{len(fresh_tfs)}/{len(BAR_FRESHNESS_THRESHOLDS)} fresh"
            if deferred_tfs:
                logger.debug(
                    "[data_sync] stale bars deferred by retry backoff={}",
                    deferred_tfs,
                )
            if stale_tfs:
                logger.info("[data_sync] stale bars={} → pulling", stale_tfs)
            elif deferred_tfs:
                health.record_success(
                    last_bar_ts_by_tf=observed_bar_ts_by_tf or None
                )
                return
            else:
                logger.debug("[data_sync] all fresh ({}), skip pull", bar_status)
                health.record_success(last_bar_ts_by_tf=observed_bar_ts_by_tf or None)
                return

            try:
                session = market_session_snapshot(None)
                session_status = str((session or {}).get("status") or "")
                if session_status in {"closed_confirmed", "closed_pending_confirmation", "closed_pending_positions"}:
                    logger.info(
                        "[data_sync] bars stale but market is closed (status={}, reason={}); skip bar pull",
                        session_status,
                        (session or {}).get("reason") or "",
                    )
                    health.record_success(last_bar_ts_by_tf=observed_bar_ts_by_tf or None)
                    return
                if session_status == "open_pending_quote":
                    from backend.services.market_session import maintenance_wait_evidence

                    latest_market_data_ts = max(observed_bar_ts_by_tf.values(), default=0.0)
                    maintenance = maintenance_wait_evidence(
                        session,
                        latest_market_data_ts=latest_market_data_ts,
                        now_ts=now,
                        grace_seconds=float(cfg.market_open_pending_quote_grace_seconds),
                    )
                    if maintenance["active"]:
                        logger.info(
                            "[data_sync] bars stale during maintenance wait (remaining={:.0f}s, evidence={}); skip bar pull",
                            maintenance["remaining_seconds"],
                            maintenance["evidence"],
                        )
                        health.record_success(last_bar_ts_by_tf=observed_bar_ts_by_tf or None)
                        return
            except Exception as exc:
                logger.debug("[data_sync] market session check failed before pull: {}", exc)

            # 3. 回补 bars (用主 bridge 直接拉, 不再开第二连接)
            total_bars = 0
            sync_tfs = stale_tfs if stale_tfs else list(BAR_FRESHNESS_THRESHOLDS.keys())
            if sync_tfs:
                bridge, err, warming = get_ctrader()
                if err:
                    logger.warning("[data_sync] cTrader bridge unavailable: {}, skip bar pull", err)
                elif warming:
                    logger.info("[data_sync] cTrader bridge still warming up, skip bar pull")
                elif not bridge.is_connected:
                    logger.info("[data_sync] cTrader bridge not connected, skip bar pull")
                else:
                    for sym in symbols:
                        for tf in sync_tfs:
                            try:
                                fetch_count = max(
                                    5,
                                    min(
                                        200,
                                        int(
                                            missing_closed_bars_by_tf.get(tf)
                                            or 1
                                        )
                                        + 3,
                                    ),
                                )
                                before_ts = float(
                                    observed_bar_ts_by_tf.get(tf) or 0.0
                                )
                                df = bridge.fetch_bars(
                                    tf,
                                    n_bars=fetch_count,
                                )
                                if df is None or df.empty:
                                    retry_state[tf] = {
                                        "observed_bar_ts": before_ts,
                                        "next_retry_at": now
                                        + (3600.0 if tf == "D1" else 300.0),
                                    }
                                    continue
                                bars = dataframe_to_store_bars(df)
                                data_store_factory().insert_bars(bars, sym, tf)
                                total_bars += len(bars)
                                reconciled = _reconcile_live_trendbars(
                                    bridge, sym, tf, bars, now
                                )
                                if reconciled:
                                    logger.info(
                                        "[data_sync] {} {} live trendbar reconcile: {} bars corrected",
                                        sym,
                                        tf,
                                        reconciled,
                                    )
                                if bars:
                                    observed_bar_ts_by_tf[tf] = float(bars[-1]["time"])
                                after_ts = float(
                                    observed_bar_ts_by_tf.get(tf) or 0.0
                                )
                                if after_ts <= before_ts:
                                    retry_state[tf] = {
                                        "observed_bar_ts": before_ts,
                                        "next_retry_at": now
                                        + (3600.0 if tf == "D1" else 300.0),
                                    }
                                else:
                                    retry_state.pop(tf, None)
                                logger.info("[data_sync] pulled {} {} bars: {} bars", sym, tf, len(bars))
                            except Exception as e:
                                retry_state[tf] = {
                                    "observed_bar_ts": float(
                                        observed_bar_ts_by_tf.get(tf) or 0.0
                                    ),
                                    "next_retry_at": now
                                    + (3600.0 if tf == "D1" else 300.0),
                                }
                                logger.warning("[data_sync] {} {} pull failed: {}", sym, tf, e)

            # 4. 记录健康状态
            elapsed = now_fn() - t0
            health.record_success(last_bar_ts_by_tf=observed_bar_ts_by_tf or None)
            if total_bars > 0:
                logger.info("[data_sync] done ({:.1f}s): +{} bars", elapsed, total_bars)
        except Exception as e:
            logger.warning("[data_sync] failed: {}", e)
            try:
                health.record_failure(str(e)[:200])
            except Exception as inner_exc:
                logger.debug("[data_sync] health.record_failure failed: {}", inner_exc)
        finally:
            lock.release()

    return _data_sync
