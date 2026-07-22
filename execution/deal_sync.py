"""execution/deal_sync.py — cTrader 成交同步模块.

职责:
  1. 从 cTrader get_deals() 拉原始成交记录
  2. 写入 PostgreSQL state_v1.ctrader_deals 表 (原始数据锚点)
  3. 按 position_id 匹配平仓成交, 提取真实 PnL (gross_profit + swap + signed commission)
  4. 供 live_service.py 平仓检测后调用

用法:
    from execution.deal_sync import sync_close_deal, fetch_deals_since

    # 在 live_service 平仓检测后:
    real_pnl = sync_close_deal(bridge, position_ids, state_conn)
    # real_pnl = {position_id: {"gross": ..., "swap": ..., "commission": ..., "net": ...}}
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import sqlite3
import time
from typing import Any, Mapping, MutableMapping

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────

_DEAL_WINDOW_SEC = 3600  # 每次拉最近 1 小时成交（足够覆盖平仓检测间隔）
_MAX_ROWS = 100          # 每批最多 100 条


@dataclass(frozen=True)
class DealFetchResult:
    """Explicit broker-deal fetch outcome used by safety/recovery callers.

    The compatibility ``fetch_deals_since`` API still returns a list, but an
    empty list cannot distinguish an authoritative empty broker response from
    transport failure.  Recovery code must use this contract whenever that
    distinction affects diagnostics or latch release.
    """

    status: str
    deals: tuple[dict, ...]
    observed_at: float
    error_code: str = ""
    error_message: str = ""

    @property
    def success(self) -> bool:
        return self.status == "success"

    @property
    def empty(self) -> bool:
        return self.success and not self.deals


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params=None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


def fetch_deals_since_result(
    bridge: Any,
    from_ts: int | None = None,
    to_ts: int | None = None,
    max_rows: int = _MAX_ROWS,
) -> DealFetchResult:
    """Fetch broker deals without collapsing failure into valid-empty."""

    now = int(time.time())
    from_ts = from_ts or (now - _DEAL_WINDOW_SEC)
    to_ts = to_ts or now
    observed_at = time.time()
    if bridge is None or not bool(getattr(bridge, "is_connected", False)):
        logger.warning("[DealSync] bridge not connected, cannot fetch deals")
        return DealFetchResult(
            status="failed",
            deals=(),
            observed_at=observed_at,
            error_code="bridge_not_connected",
            error_message="cTrader bridge is not connected",
        )
    try:
        raw_deals = bridge.get_deals(
            from_ts=from_ts,
            to_ts=to_ts,
            max_rows=max_rows,
        )
        if raw_deals is None:
            raise RuntimeError("broker_deal_response_missing")
        if not raw_deals and getattr(bridge, "_last_deals_fetch_ok", None) is False:
            # CTraderBridge keeps the compatibility list API and therefore
            # returns [] after an RPC failure.  Preserve the explicit fetch
            # contract here so recovery cannot mistake transport failure for
            # an authoritative empty history response.
            raise RuntimeError("broker_deal_fetch_failed")
        deals = tuple(dict(item) for item in raw_deals)
        logger.info(
            "[DealSync] fetched %d deals (from_ts=%s)",
            len(deals),
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(from_ts)),
        )
        return DealFetchResult(
            status="success",
            deals=deals,
            observed_at=time.time(),
        )
    except Exception as exc:
        logger.error("[DealSync] fetch_deals failed: %s", exc)
        return DealFetchResult(
            status="failed",
            deals=(),
            observed_at=time.time(),
            error_code="broker_deal_fetch_failed",
            error_message=f"{type(exc).__name__}: {exc}",
        )


def fetch_deals_since(
    bridge: Any,
    from_ts: int | None = None,
    to_ts: int | None = None,
    max_rows: int = _MAX_ROWS,
) -> list[dict]:
    """Compatibility list API for callers that do not need fetch authority.

    Args:
        bridge: CTraderBridge 实例 (已连接).
        from_ts: 起始时间戳 (秒), 默认 now - _DEAL_WINDOW_SEC.
        to_ts:   结束时间戳 (秒), 默认 now.
        max_rows: 最大条数.

    Returns:
        get_deals() 返回的 list[dict].
    """
    return list(
        fetch_deals_since_result(
            bridge,
            from_ts=from_ts,
            to_ts=to_ts,
            max_rows=max_rows,
        ).deals
    )


def store_deals(
    conn: sqlite3.Connection,
    deals: list[dict],
) -> int:
    """将成交记录幂等写入 state store 的 ctrader_deals 表.

    Args:
        conn: PostgreSQL state store 连接.
        deals: get_deals() 返回的 list[dict].

    Returns:
        写入条数.
    """
    if not deals:
        return 0
    count = 0
    now = time.time()
    for d in deals:
        cd = d.get("close_detail", {}) or {}
        # 判断是否为平仓腿: 有余额更新说明是真实平仓
        _is_close = 0
        if cd and (cd.get("balance", 0) != 0 or cd.get("gross_profit", 0) != 0):
            _is_close = 1
        try:
            _execute(conn, """
                INSERT INTO ctrader_deals
                (deal_id, position_id, order_id, symbol_id,
                 volume, filled_volume, exec_price, trade_side,
                 deal_status, exec_timestamp, commission,
                 entry_price, gross_profit, swap,
                 close_commission, balance, closed_volume,
                 is_close, fetched_at)
                VALUES (?, ?, ?, ?,
                        ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?)
                ON CONFLICT(deal_id) DO NOTHING
            """, [
                d["deal_id"],
                d.get("position_id", 0),
                d.get("order_id", 0),
                d.get("symbol_id", 0),
                d.get("volume", 0),
                d.get("filled_volume", 0),
                d.get("execution_price", 0.0),
                d.get("trade_side", ""),
                d.get("deal_status", 0),
                d.get("execution_timestamp", 0.0),
                d.get("commission", 0.0),
                cd.get("entry_price", 0.0),
                cd.get("gross_profit", 0.0),
                cd.get("swap", 0.0),
                cd.get("commission", 0.0),
                cd.get("balance", 0.0),
                cd.get("closed_volume", 0),
                _is_close,
                now,
            ])
            count += 1
        except Exception as e:
            logger.warning("[DealSync] store deal_id=%s failed: %s",
                           d.get("deal_id"), e)
    conn.commit()
    logger.info("[DealSync] stored %d / %d deals", count, len(deals))
    return count


def find_close_deal(
    conn: sqlite3.Connection,
    position_id: int,
    *,
    min_exec_timestamp: float = 0.0,
) -> dict | None:
    """按 position_id 聚合已存储的平仓成交记录.

    Args:
        conn: PostgreSQL state store 连接.
        position_id: cTrader 仓位 ID.

    Returns:
        close_detail dict 或 None (找不到 / 只有开仓腿).
    """
    rows = _execute(
        conn,
        """
        SELECT *
        FROM ctrader_deals
        WHERE position_id=? AND (is_close=1 OR closed_volume > 0)
        ORDER BY exec_timestamp ASC, deal_id ASC
        """,
        (position_id,),
    ).fetchall()
    if not rows:
        return None
    detail = _aggregate_close_details(rows)
    if float(detail.get("exec_timestamp") or 0.0) < max(
        0.0,
        float(min_exec_timestamp or 0.0),
    ):
        # Existing rows may represent an earlier partial close.  They do not
        # prove the final close observed after the position's last open broker
        # snapshot, so force a fresh deal fetch instead of returning stale PnL.
        return None
    return detail


def _aggregate_close_details(rows: list[sqlite3.Row]) -> dict:
    """将同一仓位的所有 close legs 合并为一个 close_detail.

    cTrader 对部分平仓和最终平仓分别产生 deal。只取最新一笔会漏掉
    supervisor reduce 的已实现盈亏；金额字段应求和，价格/余额/时间等
    快照字段取最后一笔。
    """
    latest = rows[-1]
    return {
        "gross_profit": sum(float(row["gross_profit"] or 0.0) for row in rows),
        "swap": sum(float(row["swap"] or 0.0) for row in rows),
        "close_commission": sum(float(row["close_commission"] or 0.0) for row in rows),
        "balance": latest["balance"],
        "entry_price": latest["entry_price"],
        "exec_price": latest["exec_price"],
        "volume": sum(float(row["volume"] or 0.0) for row in rows),
        "closed_volume": sum(float(row["closed_volume"] or 0.0) for row in rows),
        "exec_timestamp": latest["exec_timestamp"],
        "trade_side": latest["trade_side"],
        "deal_id": latest["deal_id"],
        "deal_ids": [row["deal_id"] for row in rows],
        "close_deals_count": len(rows),
    }


def sync_close_deal(
    bridge: Any,
    conn: sqlite3.Connection,
    position_id: int,
    *,
    from_ts: int | None = None,
    to_ts: int | None = None,
    max_rows: int = _MAX_ROWS,
    min_exec_timestamp: float = 0.0,
) -> dict | None:
    """一站式: 为单个平仓 position_id 获取真实 PnL.

    流程:
      1. 先查 PostgreSQL state store 有没有存过该 position_id 的平仓成交
      2. 如果有 → 直接返回
      3. 如果没有 → 拉最近成交写入 DB, 再查

    Args:
        bridge: CTraderBridge 实例 (已连接).
        conn: PostgreSQL state store 连接.
        position_id: 刚消失的仓位 ID.

    Returns:
        {"gross": gross_profit, "swap": swap, "commission": close_commission,
         "net": gross_profit + swap + close_commission,
         "entry_price": ..., "exec_price": ..., "balance": ...}
        或 None (获取失败).
    """
    # 先查本地
    cd = find_close_deal(
        conn,
        position_id,
        min_exec_timestamp=min_exec_timestamp,
    )
    if cd is not None:
        return _cd_to_real_pnl(cd)

    # 本地没有 → 拉最近成交
    fetch_result = fetch_deals_since_result(
        bridge,
        from_ts=from_ts,
        to_ts=to_ts,
        max_rows=max_rows,
    )
    if fetch_result.deals:
        store_deals(conn, list(fetch_result.deals))
        cd = find_close_deal(
            conn,
            position_id,
            min_exec_timestamp=min_exec_timestamp,
        )
        if cd is not None:
            return _cd_to_real_pnl(cd)

    logger.warning("[DealSync] position_id=%s: no close deal found", position_id)
    return None


def sync_close_deals_batch(
    bridge: Any,
    conn: sqlite3.Connection,
    position_ids: set[int],
    *,
    from_ts: int | None = None,
    to_ts: int | None = None,
    max_rows: int = _MAX_ROWS,
    min_exec_timestamp_by_position: Mapping[int, float] | None = None,
    required_closed_volume_delta_by_position: Mapping[int, float] | None = None,
    baseline_close_cursor_by_position: Mapping[int, Mapping[str, Any]] | None = None,
    observed_close_cursor_out: MutableMapping[int, dict[str, Any]] | None = None,
) -> dict[int, dict]:
    """批量版 sync_close_deal.

    Args:
        bridge: CTraderBridge 实例.
        conn: PostgreSQL state store 连接.
        position_ids: 刚消失的仓位 ID 集合.

    Returns:
        {position_id: {"gross": ..., "swap": ..., "net": ...}}
    """
    if not position_ids:
        return {}

    # 1. 查本地已有
    result: dict[int, dict] = {}
    need_fetch: set[int] = set()

    minimums = {
        int(pid): max(0.0, float(value or 0.0))
        for pid, value in dict(min_exec_timestamp_by_position or {}).items()
    }
    required_deltas = {
        int(pid): max(0.0, float(value or 0.0))
        for pid, value in dict(
            required_closed_volume_delta_by_position or {}
        ).items()
    }
    supplied_cursors = {
        int(pid): dict(cursor or {})
        for pid, cursor in dict(
            baseline_close_cursor_by_position or {}
        ).items()
    }
    baselines: dict[int, dict[str, Any]] = {}
    for pid in position_ids:
        observed_baseline = find_close_deal(conn, pid) or {}
        if observed_close_cursor_out is not None:
            observed_close_cursor_out[int(pid)] = {
                "baseline_cursor_available": True,
                "baseline_deal_ids": list(
                    observed_baseline.get("deal_ids") or []
                ),
                "baseline_closed_volume": float(
                    observed_baseline.get("closed_volume") or 0.0
                ),
                "required_closed_volume_delta": float(
                    required_deltas.get(int(pid), 0.0)
                ),
            }
        supplied = supplied_cursors.get(int(pid))
        baseline = (
            {
                "deal_ids": list(
                    supplied.get("baseline_deal_ids")
                    or supplied.get("deal_ids")
                    or []
                ),
                "closed_volume": float(
                    supplied.get("baseline_closed_volume")
                    or supplied.get("closed_volume")
                    or 0.0
                ),
            }
            if supplied is not None
            and supplied.get("baseline_cursor_available", True) is not False
            else observed_baseline
        )
        baselines[int(pid)] = baseline
        cd = find_close_deal(
            conn,
            pid,
            min_exec_timestamp=minimums.get(int(pid), 0.0),
        )
        if cd is not None and required_deltas.get(int(pid), 0.0) <= 0.0:
            result[pid] = _cd_to_real_pnl(cd)
        else:
            need_fetch.add(pid)

    # 2. 缺失的拉取
    if need_fetch:
        fetch_result = fetch_deals_since_result(
            bridge,
            from_ts=from_ts,
            to_ts=to_ts,
            max_rows=max_rows,
        )
        if fetch_result.deals:
            store_deals(conn, list(fetch_result.deals))
        if not fetch_result.success:
            logger.warning(
                "[DealSync] batch broker fetch failed code=%s error=%s",
                fetch_result.error_code,
                fetch_result.error_message,
            )
        for pid in list(need_fetch):
            cd = find_close_deal(
                conn,
                pid,
                min_exec_timestamp=minimums.get(int(pid), 0.0),
            )
            required_delta = required_deltas.get(int(pid), 0.0)
            baseline = baselines.get(int(pid)) or {}
            baseline_ids = {
                int(item)
                for item in list(baseline.get("deal_ids") or [])
                if int(item or 0) > 0
            }
            observed_ids = {
                int(item)
                for item in list((cd or {}).get("deal_ids") or [])
                if int(item or 0) > 0
            }
            closed_volume_delta = max(
                0.0,
                float((cd or {}).get("closed_volume") or 0.0)
                - float(baseline.get("closed_volume") or 0.0),
            )
            delta_proven = bool(
                required_delta <= 0.0
                or (
                    observed_ids - baseline_ids
                    and closed_volume_delta + 1e-9 >= required_delta
                )
            )
            if cd is not None and delta_proven:
                result[pid] = _cd_to_real_pnl(cd)
                need_fetch.discard(pid)

    if need_fetch:
        logger.warning("[DealSync] batch: %d positions still missing close deals: %s",
                       len(need_fetch), need_fetch)

    return result


def _cd_to_real_pnl(cd: dict) -> dict:
    """从 close_detail dict 合成 real_pnl dict."""
    gross = cd.get("gross_profit", 0.0) or 0.0
    swap = cd.get("swap", 0.0) or 0.0
    commission = cd.get("close_commission", 0.0) or 0.0
    return {
        "gross": gross,
        "swap": swap,
        "commission": commission,
        "net": gross + swap + commission,
        "entry_price": cd.get("entry_price", 0.0),
        "exec_price": cd.get("exec_price", 0.0),
        "exec_timestamp": cd.get("exec_timestamp", 0.0),
        "balance": cd.get("balance", 0.0),
        "closed_volume": cd.get("closed_volume", 0),
        "deal_id": cd.get("deal_id", 0),
        "deal_ids": list(cd.get("deal_ids") or ([cd.get("deal_id")] if cd.get("deal_id") else [])),
        "close_deals_count": int(cd.get("close_deals_count") or 1),
        "source": "ctrader_deals",
    }
