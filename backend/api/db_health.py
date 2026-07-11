"""GET /api/system/db-health — 数据库健康状态（大小/行数/最新数据时间）

启动时自动预热缓存，避免首次请求阻塞线程池导致其他接口超时。
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import APIRouter
from backend.core.auth import RequireUser
from backend.core.db import connect_sqlite, duckdb_readonly_connection, get_state_pg_conn, state_pg_enabled, state_table_columns

router = APIRouter(prefix="/api/system", tags=["system"])

# 缓存: 后台线程每 55s 自动刷新, 避免请求阻塞线程池
_cache: dict | None = None
_cache_ts: float = 0
_cache_lock = threading.Lock()
_CACHE_REFRESH_INTERVAL = 55  # 秒 (早于旧 TTL 5s, 确保永不过期)
_refresh_thread: threading.Thread | None = None
_stop_refresh = threading.Event()

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# 需要报告的数据库列表
_DB_LIST = [
    # (文件名, 显示名, 类型)
    ("bars.duckdb", "当前月K线", "duckdb"),
    ("external_data.duckdb", "外部数据(COT/ETF/宏观)", "duckdb"),
    ("ctrader_data.duckdb", "旧K线/外部数据兼容库", "duckdb"),
    ("trades.duckdb", "交易记录", "duckdb"),
    ("events.duckdb", "事件日历", "duckdb"),
    ("state_v1", "统一状态库(PostgreSQL)", "postgres_state"),
    ("experiments.db", "实验记录", "sqlite"),
]

_ARCHIVED_DATABASES = {
    "ctrader_data.duckdb": "旧 K 线冷备/兼容库；当前 live K 线写入 data/bars_monthly/bars_YYYY_MM.duckdb，data/bars.duckdb 指向当前月库",
}

_IDLE_DATABASES = {
    "experiments.db": "独立实验记录库；没有近期实验不代表运行异常",
}

_FRESHNESS_THRESHOLDS = {
    # 外部研究数据含周度 COT、季度 ETF 和日频 FRED；用 3 天作为正常窗口，避免把周末/假日延迟误报为过期。
    "external_data.duckdb": (3600, 3 * 86400, 7 * 86400),
}

_TS_CANDIDATES = [
    "time", "ts", "timestamp", "bar_ts", "open_ts", "close_ts",
    "created_at", "updated_at", "tick_ts", "exec_ts",
    "date", "datetime", "event_ts", "bar_date",
]


def _fmt_size(size_bytes: int) -> str:
    """字节转可读大小"""
    if size_bytes >= 1_073_741_824:
        return f"{size_bytes / 1_073_741_824:.1f}G"
    if size_bytes >= 1_048_576:
        return f"{size_bytes / 1_048_576:.0f}M"
    if size_bytes >= 1_024:
        return f"{size_bytes / 1_024:.0f}K"
    return f"{size_bytes}B"


def _try_parse_ts(val) -> float | None:
    """尝试把值转成 Unix timestamp。支持数字戳和 ISO 日期字符串"""
    if val is None:
        return None
    # 数字
    if isinstance(val, (int, float)):
        if val > 1_000_000_000:
            return float(val)
        if val > 40_000:
            return float(val)
        return None
    # 字符串日期：2026-06-17 或 2026-06-17T12:00:00
    s = str(val).strip()[:19]
    if not s or s[0] not in '0123456789':
        return None
    try:
        from datetime import datetime, timezone
        if 'T' in s or ' ' in s:
            dt = datetime.fromisoformat(s.replace(' ', 'T'))
        else:
            dt = datetime.strptime(s[:10], '%Y-%m-%d')
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _duckdb_stats(path: Path) -> dict:
    """查询 DuckDB 数据库的表统计"""
    tables = []
    total_rows = 0
    latest_ts = None
    errors = []

    try:
        with duckdb_readonly_connection(path, snapshot_first=True) as con:
            for t in con.execute("SHOW TABLES").fetchall():
                tname = t[0]
                try:
                    cnt = con.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
                    total_rows += cnt
                    cols = [c[0] for c in con.execute(f'DESCRIBE "{tname}"').fetchall()]
                    tbl_latest = None

                    # 找时间列并取最新值
                    for tc in _TS_CANDIDATES:
                        if tc in cols:
                            try:
                                res = con.execute(
                                    f'SELECT MAX("{tc}") FROM "{tname}" WHERE "{tc}" IS NOT NULL'
                                ).fetchone()
                                if res and res[0]:
                                    tbl_latest = _try_parse_ts(res[0])
                                    if tbl_latest:
                                        break
                            except Exception:
                                continue

                    tables.append({
                        "name": tname,
                        "rows": cnt,
                        "latest_ts": tbl_latest,
                    })
                    if tbl_latest and isinstance(tbl_latest, (int, float)) and tbl_latest > (latest_ts or 0):
                        latest_ts = tbl_latest
                except Exception as e:
                    errors.append(f"{tname}: {e}")
    except Exception as e:
        errors.append(f"connect: {e}")

    return {"tables": tables, "total_rows": total_rows, "latest_ts": latest_ts, "errors": errors}


def _sqlite_stats(path: Path) -> dict:
    """查询 SQLite 数据库的表统计"""
    tables = []
    total_rows = 0
    latest_ts = None
    errors = []

    try:
        con = connect_sqlite(path, read_only=True)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            for row in cur.fetchall():
                tname = row["name"]
                try:
                    cnt = con.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
                    total_rows += cnt
                    # 获取列名
                    col_cur = con.execute(f'PRAGMA table_info("{tname}")')
                    cols = [r["name"] for r in col_cur.fetchall()]
                    tbl_latest = None

                    for tc in _TS_CANDIDATES:
                        if tc in cols:
                            try:
                                res = con.execute(
                                    f'SELECT MAX("{tc}") FROM "{tname}" WHERE "{tc}" IS NOT NULL'
                                ).fetchone()
                                if res and res[0]:
                                    tbl_latest = _try_parse_ts(res[0])
                                    if tbl_latest:
                                        break
                            except Exception:
                                continue

                    tables.append({
                        "name": tname,
                        "rows": cnt,
                        "latest_ts": tbl_latest,
                    })
                    if tbl_latest and tbl_latest > (latest_ts or 0):
                        latest_ts = tbl_latest
                except Exception as e:
                    errors.append(f"{tname}: {e}")
        finally:
            con.close()
    except Exception as e:
        errors.append(f"connect: {e}")

    return {"tables": tables, "total_rows": total_rows, "latest_ts": latest_ts, "errors": errors}


def _postgres_state_stats() -> dict:
    tables = []
    total_rows = 0
    latest_ts = None
    errors = []
    try:
        con = get_state_pg_conn(read_only=True)
        try:
            rows = con.execute(
                """
                SELECT table_name AS name
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                ORDER BY table_name
                """
            ).fetchall()
            for row in rows:
                tname = row["name"]
                try:
                    cnt_row = con.execute(f'SELECT COUNT(*) AS n FROM "{tname}"').fetchone()
                    cnt = int((cnt_row or {}).get("n") or 0)
                    total_rows += cnt
                    cols = state_table_columns(con, tname)
                    tbl_latest = None
                    for tc in _TS_CANDIDATES:
                        if tc in cols:
                            try:
                                res = con.execute(
                                    f'SELECT MAX("{tc}") AS latest FROM "{tname}" WHERE "{tc}" IS NOT NULL'
                                ).fetchone()
                                raw_latest = (res or {}).get("latest")
                                if raw_latest:
                                    tbl_latest = _try_parse_ts(raw_latest)
                                    if tbl_latest:
                                        break
                            except Exception:
                                continue
                    tables.append({"name": tname, "rows": cnt, "latest_ts": tbl_latest})
                    if tbl_latest and tbl_latest > (latest_ts or 0):
                        latest_ts = tbl_latest
                except Exception as e:
                    errors.append(f"{tname}: {e}")
        finally:
            con.close()
    except Exception as e:
        errors.append(f"connect: {e}")
    return {"tables": tables, "total_rows": total_rows, "latest_ts": latest_ts, "errors": errors}


def _compute_db_health() -> dict:
    """计算所有数据库的健康状态（阻塞，约 20s）"""
    databases = []
    now = time.time()

    for filename, label, db_type in _DB_LIST:
        path = _DATA_DIR / filename
        if db_type == "postgres_state":
            stats = _postgres_state_stats()
            exists = state_pg_enabled() and not any(str(e).startswith("connect:") for e in stats["errors"])
            latest = stats["latest_ts"]
            freshness = "unknown"
            if latest and isinstance(latest, (int, float)):
                age_sec = now - latest
                if age_sec < 3600:
                    freshness = "fresh"
                elif age_sec < 86400:
                    freshness = "recent"
                elif age_sec < 259200:
                    freshness = "stale"
                else:
                    freshness = "old"
            databases.append({
                "name": label,
                "file": filename,
                "type": "postgres",
                "exists": exists,
                "size": "server",
                "size_bytes": 0,
                "tables": stats["tables"],
                "total_rows": stats["total_rows"],
                "latest_ts": latest,
                "freshness": freshness if exists else "missing",
                "errors": stats["errors"],
            })
            continue
        if not path.exists():
            databases.append({
                "name": label,
                "file": filename,
                "type": db_type,
                "exists": False,
                "size": "\u2014",
                "size_bytes": 0,
                "tables": [],
                "total_rows": 0,
                "latest_ts": None,
                "freshness": "missing",
                "errors": ["file not found"],
            })
            continue

        size_bytes = path.stat().st_size
        stats = _duckdb_stats(path) if db_type == "duckdb" else _sqlite_stats(path)

        latest = stats["latest_ts"]
        freshness = "unknown"
        if latest and isinstance(latest, (int, float)):
            age_sec = now - latest
            fresh_limit, recent_limit, stale_limit = _FRESHNESS_THRESHOLDS.get(
                filename,
                (3600, 86400, 259200),
            )
            if age_sec < fresh_limit:
                freshness = "fresh"
            elif age_sec < recent_limit:
                freshness = "recent"
            elif age_sec < stale_limit:
                freshness = "stale"
            else:
                freshness = "old"

        health_note = ""
        if filename in _ARCHIVED_DATABASES:
            freshness = "archived"
            health_note = _ARCHIVED_DATABASES[filename]
        elif filename in _IDLE_DATABASES and stats["total_rows"] > 0 and not stats["errors"]:
            freshness = "idle"
            health_note = _IDLE_DATABASES[filename]

        databases.append({
            "name": label,
            "file": filename,
            "type": db_type,
            "exists": True,
            "size": _fmt_size(size_bytes),
            "size_bytes": size_bytes,
            "tables": stats["tables"],
            "total_rows": stats["total_rows"],
            "latest_ts": latest,
            "freshness": freshness,
            "errors": stats["errors"],
            "health_note": health_note,
        })

    # 整体健康分
    fresh_count = sum(1 for d in databases if d.get("freshness") == "fresh")
    missing_count = sum(1 for d in databases if not d.get("exists"))
    stale_count = sum(1 for d in databases if d.get("freshness") in ("stale", "old"))

    if missing_count == 0 and stale_count == 0:
        overall = "healthy"
    elif missing_count > 0:
        overall = "degraded"
    else:
        overall = "stale"

    return {
        "ok": True,
        "overall": overall,
        "checked_at": now,
        "summary": {
            "total": len(databases),
            "fresh": fresh_count,
            "stale": stale_count,
            "missing": missing_count,
        },
        "databases": databases,
    }


def _refresh_cache():
    """后台线程: 每 55s 自动刷新缓存, 确保请求永远走缓存秒返。"""
    global _cache, _cache_ts
    while not _stop_refresh.is_set():
        try:
            result = _compute_db_health()
            with _cache_lock:
                _cache = result
                _cache_ts = time.time()
        except Exception:
            pass  # 刷新失败保留旧缓存, 下次重试
        _stop_refresh.wait(_CACHE_REFRESH_INTERVAL)


def _start_background_refresh():
    """启动后台缓存刷新线程 (daemon, 随进程退出)。"""
    global _refresh_thread, _stop_refresh
    if _refresh_thread is not None and _refresh_thread.is_alive():
        return
    _stop_refresh.clear()
    _refresh_thread = threading.Thread(
        target=_refresh_cache, daemon=True, name="db-health-refresh"
    )
    _refresh_thread.start()


@router.get("/db-health")
def db_health(_user: RequireUser) -> dict:
    """返回所有数据库的健康状态 (后台自动刷新, 永远 <1ms)。

    后台 daemon 线程每 55s 更新缓存, 请求端永远直接读缓存。
    """
    global _cache, _cache_ts
    now = time.time()

    with _cache_lock:
        if _cache is not None:
            result = dict(_cache)
            checked_at = result.get("checked_at")
            result["served_at"] = now
            result["cache_age_sec"] = max(0.0, now - float(checked_at or now))
            return result

    # 极端情况: 缓存尚未初始化 (刚启动, 预热还没跑完)
    # 此时同步计算一次 (阻塞 ~20s), 但仅发生一次
    result = _compute_db_health()
    with _cache_lock:
        _cache = result
        _cache_ts = now
    return result


# ── 启动事件：后台预热 + 持续刷新缓存 ──

def _on_startup():
    """延迟 3s 后启动后台缓存刷新线程。

    延迟确保 ctrader reactor 等重资源先初始化完毕。
    之后每 55s 自动刷新，请求端永远走缓存（<1ms）。
    """
    def _delayed_start():
        time.sleep(3)
        _start_background_refresh()

    t = threading.Thread(target=_delayed_start, daemon=True, name="db-health-init")
    t.start()


# 注册到 FastAPI app 的 startup 事件
# (在 backend/app.py 中调用: db_health.register_startup(app))
def register_startup(app):
    app.add_event_handler("startup", _on_startup)

