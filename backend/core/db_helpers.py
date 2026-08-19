"""Shared DB/JSON helpers consolidated from module-local copies (2026-08-18).

Source: S2.1 (final-execution-checklist) — single authority for the helper
functions that were duplicated across ~22-35 files (_conn_is_pg/_sql/_execute/
_loads/_json/_row_value).  New code should import from here instead of
copying a local variant.

Semantics chosen as the union of the most common module-local variants:
- ``conn_is_pg``: psycopg connection detection (``conn.__class__.__module__``
  starts with ``psycopg``).
- ``pg_sql``: convert ``?`` placeholders to ``%s`` and escape literal ``%``
  for PostgreSQL connections (``%`` -> ``%%`` then ``?`` -> ``%s``); returns
  the SQL unchanged for other (SQLite) connections.
- ``execute``: run ``pg_sql`` conversion then ``conn.execute(sql, params)``.
- ``load_json``: ``None`` -> default; ``dict``/``list`` returned as-is;
  otherwise ``json.loads(str(raw))``.
- ``dump_json``: compact ``json.dumps(value, ensure_ascii=False,
  sort_keys=True, separators=(",", ":"), default=str)``.
- ``row_value``: Mapping -> ``row.get(key, default)``; otherwise ``row[index]``
  (index defaults to 0).  Returns ``default`` for ``None`` rows/failures.
"""

from __future__ import annotations

import json
from typing import Any, Mapping


def conn_is_pg(conn: Any) -> bool:
    """Return True for a psycopg (PostgreSQL) connection object."""
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def pg_sql(conn: Any, sql: str) -> str:
    """Convert ``?`` placeholders to ``%s`` for PostgreSQL connections.

    Literal ``%`` is escaped to ``%%`` first so existing SQL stays valid for
    psycopg parameter substitution.  Non-PG connections return SQL unchanged.
    """
    if not conn_is_pg(conn):
        return sql
    return sql.replace("%", "%%").replace("?", "%s")


def execute(conn: Any, sql: str, params: Any = None):
    """Run a query through :func:`pg_sql` so callers can write ``?``-style SQL."""
    converted = pg_sql(conn, sql)
    if params is None:
        return conn.execute(converted)
    return conn.execute(converted, params)


def load_json(raw: Any, default: Any = None) -> Any:
    """Parse ``raw`` as JSON with a safe fallback.

    ``None`` -> ``default``; ``dict``/``list`` returned unchanged; otherwise
    ``json.loads(str(raw))`` (exceptions fall through to the caller's handler
    or propagate, matching the original module-local variants).
    """
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def dump_json(value: Any) -> str:
    """Compact, deterministic JSON serialization with ``default=str``."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def row_value(row: Any, key: str, index: int = 0, default: Any = None) -> Any:
    """Read a field from a row, supporting both key and positional access.

    Preference order matches the module-local variants this consolidates:
    1. ``Mapping`` -> ``row.get(key, default)``
    2. any row supporting key access (``sqlite3.Row``, psycopg dict rows,
       sequences with string keys) -> ``row[key]``
    3. positional rows (tuples/lists) -> ``row[index]``
    Returns ``default`` for ``None`` rows or any lookup failure.
    """
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        pass
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return default
