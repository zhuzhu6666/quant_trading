#!/usr/bin/env python
"""Standalone cTrader L2 order-book collector.

This process deliberately stays outside the live trading loop. It connects to
cTrader, subscribes to depth events, lets CTraderBridge persist change events to
data/l2.duckdb, and periodically stores a top-of-book snapshot for research.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from backend.core.db import DUCKDB_L2, connect_duckdb
from execution._env import load_env
from execution.ctrader_bridge import CTraderBridge


LOG = logging.getLogger("l2_collector")


def _bool_env(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


def _load_ctrader_kwargs() -> dict:
    load_env()
    try:
        from config import load_config

        cfg = load_config()
        ctrader_cfg = cfg.get("ctrader", {}) if isinstance(cfg, dict) else {}
    except Exception:
        ctrader_cfg = {}
    return {
        "client_id": os.getenv("CTRADER_CLIENT_ID", ""),
        "client_secret": os.getenv("CTRADER_CLIENT_SECRET", ""),
        "access_token": os.getenv("CTRADER_ACCESS_TOKEN", ""),
        "account_id": int(os.getenv("CTRADER_ACCOUNT_ID", "0") or 0),
        "host": str(os.getenv("CTRADER_HOST", ctrader_cfg.get("host", "demo.ctraderapi.com")) or "demo.ctraderapi.com"),
        "port": int(os.getenv("CTRADER_PORT", ctrader_cfg.get("port", 5035)) or 5035),
        "symbol": str(os.getenv("CTRADER_SYMBOL", ctrader_cfg.get("symbol", "XAUUSD")) or "XAUUSD"),
        "request_timeout_sec": float(
            os.getenv("CTRADER_REQUEST_TIMEOUT_SEC", ctrader_cfg.get("request_timeout_sec", 10)) or 10
        ),
        "proxy_url": str(os.getenv("CTRADER_PROXY_URL", ctrader_cfg.get("proxy_url", "")) or ""),
        "proxy_rdns": _bool_env(os.getenv("CTRADER_PROXY_RDNS", ctrader_cfg.get("proxy_rdns", True))),
        "send_orders": False,
    }


def _ensure_snapshot_schema(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id INTEGER PRIMARY KEY,
            symbol VARCHAR NOT NULL DEFAULT 'XAUUSD+',
            ts DOUBLE NOT NULL,
            bid1_price DOUBLE, bid1_size DOUBLE,
            bid2_price DOUBLE, bid2_size DOUBLE,
            bid3_price DOUBLE, bid3_size DOUBLE,
            bid4_price DOUBLE, bid4_size DOUBLE,
            bid5_price DOUBLE, bid5_size DOUBLE,
            ask1_price DOUBLE, ask1_size DOUBLE,
            ask2_price DOUBLE, ask2_size DOUBLE,
            ask3_price DOUBLE, ask3_size DOUBLE,
            ask4_price DOUBLE, ask4_size DOUBLE,
            ask5_price DOUBLE, ask5_size DOUBLE,
            spread DOUBLE,
            imbalance DOUBLE,
            total_bid DOUBLE,
            total_ask DOUBLE,
            created_at DOUBLE
        )
        """
    )


def _top_levels(quotes: list[dict], side: str, limit: int = 5) -> list[tuple[float, float]]:
    key = "bid" if side == "bid" else "ask"
    rows: dict[float, float] = {}
    for item in quotes:
        try:
            price = float(item.get(key) or 0.0)
            size = float(item.get("size") or 0.0)
        except Exception:
            continue
        if price <= 0 or size <= 0:
            continue
        rows[price] = rows.get(price, 0.0) + size
    reverse = side == "bid"
    return sorted(rows.items(), key=lambda item: item[0], reverse=reverse)[:limit]


class L2CollectorBridge(CTraderBridge):
    def __init__(self, *args, snapshot_interval_sec: float = 5.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.snapshot_interval_sec = float(snapshot_interval_sec or 0.0)
        self._snapshot_last_ts = 0.0
        self._snapshot_counter = self._load_snapshot_counter()

    def _load_snapshot_counter(self) -> int:
        try:
            conn = connect_duckdb(DUCKDB_L2)
            try:
                _ensure_snapshot_schema(conn)
                row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM orderbook_snapshots").fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()
        except Exception as exc:
            LOG.warning("load snapshot counter failed: %s", exc)
            return 0

    def _handle_depth_event(self, payload):
        try:
            self._handle_depth_event_with_retry(payload)
            if self.snapshot_interval_sec <= 0:
                return
            now = time.time()
            if now - self._snapshot_last_ts < self.snapshot_interval_sec:
                return
            self._snapshot_last_ts = now
            self._persist_snapshot(now)
        finally:
            self._release_l2_db()

    def _handle_depth_event_with_retry(self, payload) -> None:
        if not hasattr(self, "_depth_quotes"):
            self._depth_quotes = []
            self._depth_lock = threading.Lock()
            self._depth_counter = self._load_depth_counter()
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOADepthEvent

        if not isinstance(payload, ProtoOADepthEvent):
            return

        new_quotes = []
        for quote in payload.newQuotes:
            bid = quote.bid / 100000.0
            ask = quote.ask / 100000.0
            size = quote.size / 100.0
            new_quotes.append({
                "id": quote.id,
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "size": size,
            })
        deleted_ids = list(payload.deletedQuotes)

        with self._depth_lock:
            if deleted_ids:
                deleted = set(deleted_ids)
                self._depth_quotes = [quote for quote in self._depth_quotes if quote["id"] not in deleted]
            for quote in new_quotes:
                for existing in self._depth_quotes:
                    if existing["id"] == quote["id"]:
                        existing.update(quote)
                        break
                else:
                    self._depth_quotes.append(quote)

        self._depth_log_accum_new += len(new_quotes)
        self._depth_log_accum_deleted += len(deleted_ids)
        now = time.time()
        if now - self._depth_log_last_ts >= 5.0:
            LOG.info(
                "depth events (5s): +%d / -%d, book=%d",
                self._depth_log_accum_new,
                self._depth_log_accum_deleted,
                len(self._depth_quotes),
            )
            self._depth_log_last_ts = now
            self._depth_log_accum_new = 0
            self._depth_log_accum_deleted = 0

        rows_new = []
        rows_delete = []
        for quote in new_quotes:
            self._depth_counter += 1
            side = "bid" if quote.get("bid", 0) > 0 else "ask"
            price = quote.get("bid", 0) or quote.get("ask", 0)
            rows_new.append((self._depth_counter, now, quote["id"], side, price, quote["size"], now))
        for quote_id in deleted_ids:
            self._depth_counter += 1
            rows_delete.append((self._depth_counter, now, quote_id, now))

        self._write_depth_rows(rows_new, rows_delete)

    def _write_depth_rows(self, rows_new: list[tuple], rows_delete: list[tuple]) -> None:
        if not rows_new and not rows_delete:
            return
        last_error: Exception | None = None
        for attempt in range(10):
            try:
                with self._l2_db_lock:
                    if self._l2_db is None:
                        self._l2_db = connect_duckdb(DUCKDB_L2)
                        self._ensure_l2_schema(self._l2_db)
                    if rows_new:
                        self._l2_db.executemany(
                            """
                            INSERT INTO orderbook_changes
                            (id, symbol, ts, quote_id, side, price, size, change_type, created_at)
                            VALUES (?, 'XAUUSD+', ?, ?, ?, ?, ?, 'new', ?)
                            """,
                            rows_new,
                        )
                    if rows_delete:
                        self._l2_db.executemany(
                            """
                            INSERT INTO orderbook_changes
                            (id, symbol, ts, quote_id, side, price, size, change_type, created_at)
                            VALUES (?, 'XAUUSD+', ?, ?, '', 0, 0, 'delete', ?)
                            """,
                            rows_delete,
                        )
                if attempt:
                    LOG.info("l2 db write recovered after %d retries", attempt)
                return
            except Exception as exc:
                last_error = exc
                self._release_l2_db()
                time.sleep(0.2)
        LOG.warning("l2 db write failed after retries: %s", last_error)

    def _release_l2_db(self) -> None:
        with self._l2_db_lock:
            if self._l2_db is None:
                return
            try:
                self._l2_db.close()
            except Exception:
                pass
            self._l2_db = None

    def _persist_snapshot(self, ts: float) -> None:
        quotes = self.get_depth_quotes()
        bids = _top_levels(quotes, "bid", 5)
        asks = _top_levels(quotes, "ask", 5)
        if not bids or not asks:
            return
        total_bid = sum(size for _, size in bids)
        total_ask = sum(size for _, size in asks)
        spread = asks[0][0] - bids[0][0]
        denom = total_bid + total_ask
        imbalance = (total_bid - total_ask) / denom if denom > 0 else 0.0

        bid_values = [value for level in bids for value in level]
        ask_values = [value for level in asks for value in level]
        bid_values.extend([None] * (10 - len(bid_values)))
        ask_values.extend([None] * (10 - len(ask_values)))
        self._snapshot_counter += 1

        try:
            with self._l2_db_lock:
                if self._l2_db is None:
                    self._l2_db = connect_duckdb(DUCKDB_L2)
                    self._ensure_l2_schema(self._l2_db)
                _ensure_snapshot_schema(self._l2_db)
                self._l2_db.execute(
                    """
                    INSERT INTO orderbook_snapshots (
                        id, symbol, ts,
                        bid1_price, bid1_size, bid2_price, bid2_size, bid3_price, bid3_size,
                        bid4_price, bid4_size, bid5_price, bid5_size,
                        ask1_price, ask1_size, ask2_price, ask2_size, ask3_price, ask3_size,
                        ask4_price, ask4_size, ask5_price, ask5_size,
                        spread, imbalance, total_bid, total_ask, created_at
                    )
                    VALUES (
                        ?, 'XAUUSD+', ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?, ?, ?
                    )
                    """,
                    [
                        self._snapshot_counter,
                        ts,
                        *bid_values[:10],
                        *ask_values[:10],
                        spread,
                        imbalance,
                        total_bid,
                        total_ask,
                        ts,
                    ],
                )
        except Exception as exc:
            LOG.warning("snapshot write failed: %s", exc)


def _build_bridge(snapshot_interval: float) -> L2CollectorBridge:
    kwargs = _load_ctrader_kwargs()
    missing = [name for name in ("client_id", "client_secret", "access_token", "account_id") if not kwargs.get(name)]
    if missing:
        raise RuntimeError(f"missing cTrader env: {', '.join(missing)}")
    return L2CollectorBridge(**kwargs, snapshot_interval_sec=snapshot_interval)


def run(snapshot_interval: float, reconnect_delay: float, health_interval: float) -> int:
    stop = False

    def _stop(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    bridge: L2CollectorBridge | None = None
    last_health = 0.0
    while not stop:
        try:
            if bridge is None or not bridge.is_connected:
                if bridge is not None:
                    try:
                        bridge.disconnect()
                    except Exception:
                        pass
                bridge = _build_bridge(snapshot_interval)
                LOG.info("connecting cTrader L2 collector")
                if not bridge.connect():
                    raise RuntimeError("cTrader connect failed")
                if not bridge.subscribe_depth():
                    raise RuntimeError("subscribe_depth failed")
                LOG.info("L2 depth collection started")

            now = time.time()
            if now - last_health >= health_interval:
                depth_count = len(bridge.get_depth_quotes()) if bridge else 0
                LOG.info("collector heartbeat connected=%s book=%d", bridge.is_connected, depth_count)
                last_health = now
            time.sleep(1.0)
        except Exception as exc:
            LOG.warning("collector loop error: %s", exc)
            if bridge is not None:
                try:
                    bridge.disconnect()
                except Exception:
                    pass
                bridge = None
            time.sleep(reconnect_delay)

    if bridge is not None:
        bridge.disconnect()
    LOG.info("L2 depth collection stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run standalone cTrader L2 collector")
    parser.add_argument("--snapshot-interval", type=float, default=5.0)
    parser.add_argument("--reconnect-delay", type=float, default=15.0)
    parser.add_argument("--health-interval", type=float, default=30.0)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(args.snapshot_interval, args.reconnect_delay, args.health_interval)


if __name__ == "__main__":
    raise SystemExit(main())
