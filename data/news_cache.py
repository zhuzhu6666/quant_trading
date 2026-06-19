"""
从 DuckDB 加载 events + GVZ 进 strategy 用的内存 cache
"""
import duckdb
from datetime import datetime, timedelta
from typing import Optional


def load_nfp_dates(db_path: str = "data/events.duckdb") -> set[str]:
    """NFP 日期集合（±2 天窗口已在调用方处理，这里只返回原始日期）"""
    conn = duckdb.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT date FROM events WHERE type='NFP'")
    dates = {r[0] for r in cur.fetchall()}
    conn.close()
    return dates


def load_event_dates(db_path: str, event_type: str) -> set[str]:
    conn = duckdb.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT date FROM events WHERE type=?", (event_type,))
    dates = {r[0] for r in cur.fetchall()}
    conn.close()
    return dates


def load_gvz_series(db_path: str = "data/ctrader_data.duckdb") -> dict[str, float]:
    """GVZ 日度值"""
    conn = duckdb.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT date, value FROM macro_daily WHERE series='GVZCLS' ORDER BY date")
    rows = dict(cur.fetchall())
    conn.close()
    return rows


def daily_change_pct(series: dict[str, float], target_date: str) -> Optional[float]:
    """target_date 当天相对前一个交易日的日度变化 %"""
    sorted_dates = sorted(series.keys())
    if target_date not in series:
        return None
    cur = series[target_date]
    prev = None
    for d in sorted_dates:
        if d < target_date:
            prev = series[d]
        else:
            break
    if prev is not None and prev > 0:
        return (cur - prev) / prev * 100
    return None


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
    conn = duckdb.connect(db_path)
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
