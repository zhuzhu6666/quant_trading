"""Unit tests for backend.core.db_helpers (S2.1 consolidation)."""

import json

from backend.core import db_helpers


class _FakePgConn:
    __module__ = "psycopg.connection"


class _FakeSqliteConn:
    __module__ = "sqlite3"


class _RecorderConn:
    """Records executed SQL/params for assertions."""

    __module__ = "psycopg.connection"

    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return self


def test_conn_is_pg():
    assert db_helpers.conn_is_pg(_FakePgConn()) is True
    assert db_helpers.conn_is_pg(_FakeSqliteConn()) is False


def test_pg_sql_escapes_and_converts():
    conn = _FakePgConn()
    assert db_helpers.pg_sql(conn, "SELECT * FROM t WHERE x=?") == "SELECT * FROM t WHERE x=%s"
    # literal % is escaped before ?->%s so psycopg substitution stays valid
    assert db_helpers.pg_sql(conn, "SELECT '100%' WHERE x=?") == "SELECT '100%%' WHERE x=%s"
    # non-PG connection returns SQL unchanged (SQLite keeps ? placeholders)
    assert db_helpers.pg_sql(_FakeSqliteConn(), "SELECT * FROM t WHERE x=?") == "SELECT * FROM t WHERE x=?"


def test_execute_passes_params():
    conn = _RecorderConn()
    db_helpers.execute(conn, "SELECT 1 WHERE a=?", (1,))
    assert conn.calls == [("SELECT 1 WHERE a=%s", (1,))]
    db_helpers.execute(conn, "SELECT 2")
    assert conn.calls[-1] == ("SELECT 2", None)


def test_load_json_basics():
    assert db_helpers.load_json(None, {"d": 1}) == {"d": 1}
    assert db_helpers.load_json({"a": 1}, {}) == {"a": 1}
    assert db_helpers.load_json([1, 2], []) == [1, 2]
    assert db_helpers.load_json('{"k": 1}', {}) == {"k": 1}
    assert db_helpers.load_json("not-json", "fallback") == "fallback"


def test_dump_json_compact_sorted():
    out = db_helpers.dump_json({"b": 1, "a": [2, 1]})
    assert json.loads(out) == {"a": [2, 1], "b": 1}
    # compact separators (no spaces)
    assert " " not in out


def test_row_value_mapping_key_index_and_rows():
    assert db_helpers.row_value({"x": 5}, "x") == 5
    assert db_helpers.row_value({"x": 5}, "missing", default=9) == 9
    assert db_helpers.row_value(None, "x", default=7) == 7

    class _KeyIndexRow:
        """Mimics sqlite3.Row: supports str key access, not a Mapping."""

        def __init__(self, mapping):
            self._m = mapping

        def keys(self):
            return self._m.keys()

        def __getitem__(self, key):
            return self._m[key]

    row = _KeyIndexRow({"col_a": 11, "col_b": 22})
    assert db_helpers.row_value(row, "col_b") == 22

    # positional rows fall back to index
    assert db_helpers.row_value((10, 20), "anything", index=1) == 20
