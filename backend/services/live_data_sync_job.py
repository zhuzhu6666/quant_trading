"""Data sync scheduler job builder for the live backend."""

from __future__ import annotations

import time
from typing import Any, Callable

from backend.services.live_data_sync_helpers import (
    BAR_FRESHNESS_THRESHOLDS,
    classify_bar_freshness,
    classify_tick_freshness,
    dataframe_to_store_bars,
)


def _default_health_factory():
    from data.live_sync.health import SyncHealth

    return SyncHealth.shared()


def _default_config_factory():
    from config.runtime_config import shared as runtime_config_shared

    return runtime_config_shared()


def _default_duckdb_runtime():
    from backend.core.db import DUCKDB_BARS, DUCKDB_TICKS, duckdb_readonly_connection

    return DUCKDB_BARS, DUCKDB_TICKS, duckdb_readonly_connection


def _default_data_store_factory():
    from data.store import DataStore

    return DataStore()


def _enabled_symbols(cfg) -> list[str]:
    return list(cfg.enabled_symbols) if hasattr(cfg, "enabled_symbols") else ["XAUUSD+"]


def make_data_sync_job(
    *,
    lock,
    logger,
    get_ctrader: Callable[[], tuple[Any, str | None, bool]],
    market_session_snapshot: Callable[[Any], dict[str, Any] | None],
    health_factory: Callable[[], Any] = _default_health_factory,
    config_factory: Callable[[], Any] = _default_config_factory,
    duckdb_runtime_factory: Callable[[], tuple[Any, Any, Callable[..., Any]]] = _default_duckdb_runtime,
    data_store_factory: Callable[[], Any] = _default_data_store_factory,
    now_fn: Callable[[], float] = time.time,
):
    """Build the legacy data_sync job with injectable IO dependencies."""

    def _data_sync():
        """先检查 bars + ticks 新鲜度, 有缺口才回补, 不缺就跳过。"""
        if not lock.acquire(blocking=False):
            logger.warning("[data_sync] previous run still active, skip overlapping trigger")
            return
        t0 = now_fn()
        health = health_factory()
        try:
            cfg = config_factory()
            symbols = _enabled_symbols(cfg)
            now = now_fn()
            duckdb_bars, duckdb_ticks, duckdb_readonly_connection = duckdb_runtime_factory()

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

            # 2. 检查 tick 新鲜度 (advisory only; never gates trading/bar sync)
            tick_query_error = ""
            try:
                with duckdb_readonly_connection(duckdb_ticks, snapshot_first=True) as tick_conn:
                    tick_row = tick_conn.execute(
                        "SELECT MAX(time) FROM ticks WHERE symbol=?",
                        [symbols[0]],
                    ).fetchone()
                    tick_latest = float(tick_row[0]) if tick_row and tick_row[0] else 0
                    if tick_latest <= 0:
                        tick_row = tick_conn.execute("SELECT MAX(time) FROM ticks").fetchone()
                        tick_latest = float(tick_row[0]) if tick_row and tick_row[0] else 0
                tick_freshness = classify_tick_freshness(tick_latest, now=now)
                tick_stale = tick_freshness["stale"]
                tick_age = tick_freshness["age_seconds"]
            except Exception as e:
                tick_stale = True
                tick_age = float("inf")
                tick_query_error = str(e)[:120]

            # 3. 日志: 数据健康摘要
            bar_status = f"{len(fresh_tfs)}/{len(BAR_FRESHNESS_THRESHOLDS)} fresh"
            if stale_tfs:
                logger.info("[data_sync] stale: bars={} tick_age={:.0f}m → pulling", stale_tfs, tick_age / 60)
            else:
                # Tick data is research/advisory only; it must not trigger live bar pulls
                # or become a trading gate. The hourly dukascopy_tick job owns tick catch-up.
                if tick_stale:
                    if tick_query_error:
                        logger.info(
                            "[data_sync] bars ok, tick advisory unavailable ({}) → skip bar pull",
                            tick_query_error,
                        )
                    else:
                        logger.info(
                            "[data_sync] bars ok, tick advisory stale (age={:.0f}m) → skip bar pull",
                            tick_age / 60,
                        )
                else:
                    logger.debug("[data_sync] all fresh ({}), tick age={:.0f}m, skip pull", bar_status, tick_age / 60)
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
            except Exception as exc:
                logger.debug("[data_sync] market session check failed before pull: {}", exc)

            # 4. 回补 bars (用主 bridge 直接拉, 不再开第二连接)
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
                                df = bridge.fetch_bars(tf, n_bars=200)
                                if df is None or df.empty:
                                    continue
                                bars = dataframe_to_store_bars(df)
                                data_store_factory().insert_bars(bars, sym, tf)
                                total_bars += len(bars)
                                if bars:
                                    observed_bar_ts_by_tf[tf] = float(bars[-1]["time"])
                                logger.info("[data_sync] pulled {} {} bars: {} bars", sym, tf, len(bars))
                            except Exception as e:
                                logger.warning("[data_sync] {} {} pull failed: {}", sym, tf, e)

            # 5. 记录健康状态
            elapsed = now_fn() - t0
            health.record_success(last_bar_ts_by_tf=observed_bar_ts_by_tf or None)
            if total_bars > 0 or tick_stale:
                logger.info("[data_sync] done ({:.1f}s): +{} bars, tick_gap={:.0f}m", elapsed, total_bars, tick_age / 60)
        except Exception as e:
            logger.warning("[data_sync] failed: {}", e)
            try:
                health.record_failure(str(e)[:200])
            except Exception as inner_exc:
                logger.debug("[data_sync] health.record_failure failed: %s", inner_exc)
        finally:
            lock.release()

    return _data_sync
