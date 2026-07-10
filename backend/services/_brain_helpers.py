"""Shared DB and JSON helpers for V16 brain and agent governance modules.

Consolidates the _dumps/_loads/_connect/_execute/_safe_float pattern
that was duplicated across 7+ files (brain_state.py, brain_memory.py,
brain_action_planner.py, etc.), eliminating ~350 lines of boilerplate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
    state_table_exists,
)


def dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def sql(conn, raw_sql: str) -> str:
    return raw_sql.replace("%", "%%").replace("?", "%s") if conn_is_pg(conn) else raw_sql


def execute(conn, raw_sql: str, params: Any = None):
    if params is None:
        return conn.execute(sql(conn, raw_sql))
    return conn.execute(sql(conn, raw_sql), params)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    t = str(value).strip()
    return t if t else default


def as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if value in (None, ""):
        return []
    return [str(value)]
