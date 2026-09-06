"""
从 DuckDB 加载 events 进 strategy 用的内存 cache
"""
import duckdb
from datetime import datetime, timedelta

from backend.core.db import connect_duckdb


def load_nfp_dates(db_path: str = "data/events.duckdb") -> set[str]:
    """NFP 日期集合（±2 天窗口已在调用方处理，这里只返回原始日期）"""
    conn = connect_duckdb(db_path, read_only=True)
    cur = conn.cursor()
    cur.execute("SELECT date FROM events WHERE type='NFP'")
    dates = {r[0] for r in cur.fetchall()}
    conn.close()
    return dates


def load_event_dates(db_path: str, event_type: str) -> set[str]:
    conn = connect_duckdb(db_path, read_only=True)
    cur = conn.cursor()
    cur.execute("SELECT date FROM events WHERE type=?", (event_type,))
    dates = {r[0] for r in cur.fetchall()}
    conn.close()
    return dates


def expand_to_window(dates: set[str], days: int = 1) -> set[str]:
    """把日期集合扩展成 ±days 窗口"""
    out = set()
    for d in dates:
        try:
            base = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        for delta in range(-days, days + 1):
            out.add((base + timedelta(days=delta)).strftime("%Y-%m-%d"))
    return out


def load_events_with_importance(
    db_path: str = "data/ctrader_data.duckdb",
    min_importance: int = 2,
) -> list[dict]:
    """加载 importance >= min_importance 的事件。

    Returns:
        list of dicts: [{date, type, description, importance}, ...]
    """
    conn = connect_duckdb(db_path, read_only=True)
    cur = conn.cursor()
    cur.execute(
        "SELECT date, type, description, importance "
        "FROM events WHERE importance >= ?",
        (min_importance,),
    )
    rows = [
        {"date": r[0], "type": r[1], "description": r[2], "importance": r[3]}
        for r in cur.fetchall()
    ]
    conn.close()
    return rows

