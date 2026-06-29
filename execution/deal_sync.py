"""execution/deal_sync.py — cTrader 成交同步模块.

职责:
  1. 从 cTrader get_deals() 拉原始成交记录
  2. 写入 state.db ctrader_deals 表 (原始数据锚点)
  3. 按 position_id 匹配平仓成交, 提取真实 PnL (gross_profit + swap - commission)
  4. 供 live_service.py 平仓检测后调用

用法:
    from execution.deal_sync import sync_close_deal, fetch_deals_since

    # 在 live_service 平仓检测后:
    real_pnl = sync_close_deal(bridge, position_ids, state_conn)
    # real_pnl = {position_id: {"gross": ..., "swap": ..., "commission": ..., "net": ...}}
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── 常量 ─────────────────────────────────────────────────────────

_DEAL_WINDOW_SEC = 3600  # 每次拉最近 1 小时成交（足够覆盖平仓检测间隔）
_MAX_ROWS = 100          # 每批最多 100 条


def fetch_deals_since(
    bridge: Any,
    from_ts: int | None = None,
    to_ts: int | None = None,
    max_rows: int = _MAX_ROWS,
) -> list[dict]:
    """从 cTrader 拉成交记录。

    Args:
        bridge: CTraderBridge 实例 (已连接).
        from_ts: 起始时间戳 (秒), 默认 now - _DEAL_WINDOW_SEC.
        to_ts:   结束时间戳 (秒), 默认 now.
        max_rows: 最大条数.

    Returns:
        get_deals() 返回的 list[dict].
    """
    now = int(time.time())
    from_ts = from_ts or (now - _DEAL_WINDOW_SEC)
    to_ts = to_ts or now
    if not bridge.is_connected:
        logger.warning("[DealSync] bridge not connected, cannot fetch deals")
        return []
    try:
        deals = bridge.get_deals(from_ts=from_ts, to_ts=to_ts, max_rows=max_rows)
        logger.info("[DealSync] fetched %d deals (from_ts=%s)", len(deals),
                    time.strftime("%Y-%m-%d %H:%M", time.gmtime(from_ts)))
        return deals
    except Exception as e:
        logger.error("[DealSync] fetch_deals failed: %s", e)
        return []


def store_deals(
    conn: sqlite3.Connection,
    deals: list[dict],
) -> int:
    """将成交记录写入 state.db ctrader_deals 表 (INSERT OR IGNORE, 幂等).

    Args:
        conn: state.db 连接.
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
            conn.execute("""
                INSERT OR IGNORE INTO ctrader_deals
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
) -> dict | None:
    """按 position_id 查找已存储的平仓成交记录.

    Args:
        conn: state.db 连接.
        position_id: cTrader 仓位 ID.

    Returns:
        close_detail dict 或 None (找不到 / 只有开仓腿).
    """
    row = conn.execute(
        """
        SELECT *
        FROM ctrader_deals
        WHERE position_id=? AND (is_close=1 OR closed_volume > 0)
        ORDER BY exec_timestamp DESC, deal_id DESC
        LIMIT 1
        """,
        (position_id,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_close_detail(row)


def _row_to_close_detail(row: sqlite3.Row) -> dict:
    """将 ctrader_deals 行转为 close_detail dict."""
    return {
        "gross_profit": row["gross_profit"],
        "swap": row["swap"],
        "close_commission": row["close_commission"],
        "balance": row["balance"],
        "entry_price": row["entry_price"],
        "exec_price": row["exec_price"],
        "volume": row["volume"],
        "closed_volume": row["closed_volume"],
        "exec_timestamp": row["exec_timestamp"],
        "trade_side": row["trade_side"],
        "deal_id": row["deal_id"],
    }


def sync_close_deal(
    bridge: Any,
    conn: sqlite3.Connection,
    position_id: int,
    *,
    from_ts: int | None = None,
    to_ts: int | None = None,
    max_rows: int = _MAX_ROWS,
) -> dict | None:
    """一站式: 为单个平仓 position_id 获取真实 PnL.

    流程:
      1. 先查 state.db 有没有存过该 position_id 的平仓成交
      2. 如果有 → 直接返回
      3. 如果没有 → 拉最近成交写入 DB, 再查

    Args:
        bridge: CTraderBridge 实例 (已连接).
        conn: state.db 连接.
        position_id: 刚消失的仓位 ID.

    Returns:
        {"gross": gross_profit, "swap": swap, "commission": close_commission,
         "net": gross_profit + swap - close_commission,
         "entry_price": ..., "exec_price": ..., "balance": ...}
        或 None (获取失败).
    """
    # 先查本地
    cd = find_close_deal(conn, position_id)
    if cd is not None:
        return _cd_to_real_pnl(cd)

    # 本地没有 → 拉最近成交
    deals = fetch_deals_since(bridge, from_ts=from_ts, to_ts=to_ts, max_rows=max_rows)
    if deals:
        store_deals(conn, deals)
        cd = find_close_deal(conn, position_id)
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
) -> dict[int, dict]:
    """批量版 sync_close_deal.

    Args:
        bridge: CTraderBridge 实例.
        conn: state.db 连接.
        position_ids: 刚消失的仓位 ID 集合.

    Returns:
        {position_id: {"gross": ..., "swap": ..., "net": ...}}
    """
    if not position_ids:
        return {}

    # 1. 查本地已有
    result: dict[int, dict] = {}
    need_fetch: set[int] = set()

    for pid in position_ids:
        cd = find_close_deal(conn, pid)
        if cd is not None:
            result[pid] = _cd_to_real_pnl(cd)
        else:
            need_fetch.add(pid)

    # 2. 缺失的拉取
    if need_fetch:
        deals = fetch_deals_since(bridge, from_ts=from_ts, to_ts=to_ts, max_rows=max_rows)
        if deals:
            store_deals(conn, deals)
        for pid in list(need_fetch):
            cd = find_close_deal(conn, pid)
            if cd is not None:
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
        "net": gross + swap - commission,
        "entry_price": cd.get("entry_price", 0.0),
        "exec_price": cd.get("exec_price", 0.0),
        "exec_timestamp": cd.get("exec_timestamp", 0.0),
        "balance": cd.get("balance", 0.0),
        "closed_volume": cd.get("closed_volume", 0),
        "deal_id": cd.get("deal_id", 0),
        "source": "ctrader_deals",
    }
