"""Direct PostgreSQL state-store connections with a schema-write boundary.

Application connections may execute DML, but historical service-local
``CREATE ... IF NOT EXISTS`` / ``ALTER ... ADD COLUMN`` statements are treated
as catalog assertions.  Only the explicit migration connection executes DDL.
"""

from __future__ import annotations

import re
from typing import Any, Final

import psycopg
from psycopg.rows import dict_row


STATE_SCHEMA: Final[str] = "state_v1"


class RuntimeStateSchemaError(RuntimeError):
    """Base failure for runtime schema validation/write attempts."""


class RuntimeStateSchemaWriteError(RuntimeStateSchemaError):
    """Raised when an application connection attempts unsupported DDL."""


class RuntimeStateSchemaMissingError(RuntimeStateSchemaError):
    """Raised when a compatibility ensure references a missing migrated object."""


_SCHEMA_WRITE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|;)\s*(CREATE|ALTER|DROP|COMMENT|GRANT|REVOKE|TRUNCATE|REINDEX|CLUSTER|DO|CALL)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CREATE_TABLE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?TABLE\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<name>(?:\"[^\"]+\"|[A-Za-z_][\w$]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][\w$]*))?)",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_INDEX_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>(?:\"[^\"]+\"|[A-Za-z_][\w$]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][\w$]*))?)\s+"
    r"ON\s+(?:ONLY\s+)?"
    r"(?P<table>(?:\"[^\"]+\"|[A-Za-z_][\w$]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][\w$]*))?)"
    r"(?:\s+USING\s+[A-Za-z_][\w$]*)?\s*\(",
    re.IGNORECASE | re.DOTALL,
)
_ALTER_TABLE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?"
    r"(?P<name>(?:\"[^\"]+\"|[A-Za-z_][\w$]*)(?:\.(?:\"[^\"]+\"|[A-Za-z_][\w$]*))?)",
    re.IGNORECASE | re.DOTALL,
)
_ADD_COLUMN_RE: Final[re.Pattern[str]] = re.compile(
    r"\bADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>\"[^\"]+\"|[A-Za-z_][\w$]*)",
    re.IGNORECASE,
)


def _without_sql_comments(sql: str) -> str:
    value = re.sub(r"/\*.*?\*/", " ", str(sql or ""), flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", value)


def is_state_schema_write_sql(sql: str) -> bool:
    """Return whether SQL contains a schema/object mutation statement."""

    return bool(_SCHEMA_WRITE_RE.search(_without_sql_comments(sql)))


def _query_sql(query: Any, context: Any) -> str:
    """Render every psycopg query representation before schema classification.

    psycopg accepts both text and bytes queries.  Calling ``str()`` on bytes
    adds a leading ``b'...'`` wrapper, which would hide a leading DDL keyword
    from the runtime guard.  Composed SQL keeps using psycopg's own renderer.
    """

    if hasattr(query, "as_string"):
        return str(query.as_string(context))
    if isinstance(query, (bytes, bytearray, memoryview)):
        return bytes(query).decode("utf-8", errors="replace")
    return str(query)


def _unquote_identifier(value: str) -> str:
    return ".".join(part.strip().strip('"').replace('""', '"') for part in value.split("."))


def _row_value(row: Any, key: str, index: int = 0) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return None


def _base_execute(conn: psycopg.Connection, query: Any, params: Any = None):
    if params is None:
        return psycopg.Connection.execute(conn, query)
    return psycopg.Connection.execute(conn, query, params)


def _require_regclass(conn: psycopg.Connection, object_name: str, *, kind: str) -> None:
    row = _base_execute(
        conn,
        "SELECT to_regclass(%s) AS object_name",
        (_unquote_identifier(object_name),),
    ).fetchone()
    if not _row_value(row, "object_name"):
        raise RuntimeStateSchemaMissingError(
            f"missing PostgreSQL state {kind} {_unquote_identifier(object_name)!r}; "
            "run scripts/state_schema_migrate.py --apply before starting runtime processes"
        )


def _split_top_level_csv(value: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    quote = ""
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            items.append(value[start:index])
            start = index + 1
        index += 1
    items.append(value[start:])
    return items


def _declared_table_columns(sql: str) -> set[str]:
    start = sql.find("(")
    end = sql.rfind(")")
    if start < 0 or end <= start:
        return set()
    columns: set[str] = set()
    for declaration in _split_top_level_csv(sql[start + 1:end]):
        item = declaration.strip()
        if not item or re.match(
            r"^(?:CONSTRAINT|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|EXCLUDE)\b",
            item,
            re.IGNORECASE,
        ):
            continue
        match = re.match(r'^(?:"(?P<quoted>[^"]+)"|(?P<plain>[A-Za-z_][\w$]*))\s+', item)
        if match:
            columns.add(str(match.group("quoted") or match.group("plain") or ""))
    return columns


def _state_table_columns(conn: psycopg.Connection, table_name: str) -> set[str]:
    bare_table = _unquote_identifier(table_name).rsplit(".", 1)[-1]
    rows = _base_execute(
        conn,
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=%s
        """,
        (bare_table,),
    ).fetchall()
    return {str(_row_value(row, "column_name", 0) or "") for row in rows}


def _require_columns(conn: psycopg.Connection, table_name: str, columns: set[str]) -> None:
    missing = sorted(set(columns) - _state_table_columns(conn, table_name))
    if missing:
        raise RuntimeStateSchemaMissingError(
            f"missing PostgreSQL state columns {_unquote_identifier(table_name)}: {','.join(missing)}; "
            "run scripts/state_schema_migrate.py --apply before starting runtime processes"
        )


def _matching_parenthesis(value: str, open_index: int) -> int:
    depth = 0
    quote = ""
    index = int(open_index)
    while index < len(value):
        char = value[index]
        if quote:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = ""
        elif char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return -1


def _normalize_index_key(value: str) -> str:
    normalized = str(value or "").replace('"', "").strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = re.sub(r"\s+asc$", "", normalized)
    return normalized


def _index_contract(sql: str) -> dict[str, Any] | None:
    normalized = _without_sql_comments(sql).strip().rstrip(";").strip()
    match = _CREATE_INDEX_PREFIX_RE.match(normalized)
    if not match:
        return None
    open_index = match.end() - 1
    close_index = _matching_parenthesis(normalized, open_index)
    if close_index < 0:
        return None
    keys = tuple(
        _normalize_index_key(item)
        for item in _split_top_level_csv(normalized[open_index + 1:close_index])
        if str(item or "").strip()
    )
    tail = normalized[close_index + 1:].strip()
    return {
        "name": _unquote_identifier(match.group("name")).rsplit(".", 1)[-1],
        "table": _unquote_identifier(match.group("table")).rsplit(".", 1)[-1],
        "unique": bool(match.group("unique")),
        "keys": keys,
        "has_predicate": bool(re.search(r"\bWHERE\b", tail, re.IGNORECASE)),
    }


def _require_index_contract(conn: psycopg.Connection, declaration: str) -> None:
    expected = _index_contract(declaration)
    if not expected:
        raise RuntimeStateSchemaWriteError(
            "unsupported runtime PostgreSQL index declaration; add it to migrations/state_pg"
        )
    row = _base_execute(
        conn,
        "SELECT pg_get_indexdef(to_regclass(%s)) AS index_definition",
        (str(expected["name"]),),
    ).fetchone()
    actual_sql = str(_row_value(row, "index_definition", 0) or "")
    actual = _index_contract(actual_sql)
    comparable = ("table", "unique", "keys", "has_predicate")
    if not actual or any(actual.get(key) != expected.get(key) for key in comparable):
        raise RuntimeStateSchemaMissingError(
            f"PostgreSQL state index {expected['name']!r} does not match its migrated contract; "
            "add a new additive index name in migrations/state_pg and run "
            "scripts/state_schema_migrate.py --apply before starting runtime processes"
        )


def _validate_runtime_schema_statement(conn: psycopg.Connection, sql: str) -> None:
    """Interpret legacy idempotent DDL as a non-mutating catalog assertion."""

    normalized = _without_sql_comments(sql).strip()
    table_match = _CREATE_TABLE_RE.match(normalized)
    if table_match:
        table_name = table_match.group("name")
        _require_regclass(conn, table_name, kind="table")
        _require_columns(conn, table_name, _declared_table_columns(normalized))
        return

    index_contract = _index_contract(normalized)
    if index_contract:
        _require_regclass(conn, str(index_contract["name"]), kind="index")
        _require_index_contract(conn, normalized)
        return

    alter_match = _ALTER_TABLE_RE.match(normalized)
    if alter_match:
        table_name = _unquote_identifier(alter_match.group("name"))
        _require_regclass(conn, table_name, kind="table")
        columns = [_unquote_identifier(match.group("name")) for match in _ADD_COLUMN_RE.finditer(normalized)]
        if not columns:
            raise RuntimeStateSchemaWriteError(
                "runtime PostgreSQL ALTER is prohibited; add the change to migrations/state_pg"
            )
        _require_columns(conn, table_name, set(columns))
        return

    raise RuntimeStateSchemaWriteError(
        "runtime PostgreSQL schema writes are prohibited; "
        "scripts/state_schema_migrate.py --apply is the only schema writer"
    )


def validate_runtime_state_schema(
    conn: psycopg.Connection,
    statements: str | tuple[str, ...] | list[str],
) -> dict[str, int | str | bool]:
    """Validate legacy schema declarations without executing their DDL.

    Service-local ``ensure`` functions may retain a declaration for isolated
    SQLite fixtures, but PostgreSQL runtime paths must call this function (or
    rely on the connection-level backstop below).  Every statement is reduced
    to catalog reads for the declared table/columns/index.  Missing migrated
    objects fail closed and unsupported DDL is rejected.
    """

    items = (statements,) if isinstance(statements, str) else tuple(statements)
    if not items:
        raise ValueError("at least one runtime state schema declaration is required")
    for statement in items:
        if not is_state_schema_write_sql(statement):
            raise ValueError("runtime state schema validation accepts DDL declarations only")
        _validate_runtime_schema_statement(conn, statement)
    return {
        "schema_version": "runtime_state_schema_validation.v1",
        "ok": True,
        "validated_statement_count": len(items),
    }


class RuntimeStateConnection(psycopg.Connection):
    """DML-capable application connection that cannot mutate schema objects."""

    def execute(self, query: Any, params: Any = None, **kwargs: Any):
        sql = _query_sql(query, self)
        if is_state_schema_write_sql(sql):
            validate_runtime_state_schema(self, sql)
            return _base_execute(self, "SELECT 1 WHERE FALSE")
        return super().execute(query, params, **kwargs)


class RuntimeStateCursor(psycopg.Cursor):
    """Cursor-level guard preventing callers from bypassing Connection.execute."""

    def execute(self, query: Any, params: Any = None, **kwargs: Any):
        sql = _query_sql(query, self.connection)
        if is_state_schema_write_sql(sql):
            validate_runtime_state_schema(self.connection, sql)
            return super().execute("SELECT 1 WHERE FALSE")
        return super().execute(query, params, **kwargs)

    def executemany(self, query: Any, params_seq: Any, **kwargs: Any):
        sql = _query_sql(query, self.connection)
        if is_state_schema_write_sql(sql):
            raise RuntimeStateSchemaWriteError(
                "runtime PostgreSQL schema writes through executemany are prohibited; "
                "scripts/state_schema_migrate.py --apply is the only schema writer"
            )
        return super().executemany(query, params_seq, **kwargs)


class StateMigrationConnection(psycopg.Connection):
    """DDL-capable connection reserved for the explicit migration command."""


# Existing SQL parameter adapters detect PostgreSQL by module prefix.  Keep
# that compatibility while using psycopg Connection subclasses.
RuntimeStateConnection.__module__ = "psycopg"
RuntimeStateCursor.__module__ = "psycopg"
StateMigrationConnection.__module__ = "psycopg"


def _set_state_search_path(conn: psycopg.Connection, schema: str) -> None:
    # Schema is a code-owned constant in production; retain strict identifier
    # validation before interpolation because PostgreSQL cannot bind it.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(schema or "")):
        raise ValueError(f"invalid PostgreSQL state schema: {schema!r}")
    conn.execute(f'SET search_path TO "{schema}", public')


def connect_state_store(dsn: str, *, read_only: bool = False, schema: str = STATE_SCHEMA):
    """Open an ordinary runtime connection without implicit schema creation."""

    conn = RuntimeStateConnection.connect(
        dsn,
        autocommit=False,
        row_factory=dict_row,
        cursor_factory=RuntimeStateCursor,
    )
    if read_only:
        # psycopg applies this as the session default before the first
        # transaction starts, so commit/rollback cannot turn a reused
        # read-only handle back into a writer.
        conn.read_only = True
    _set_state_search_path(conn, schema)
    return conn


def connect_state_migration_store(dsn: str, *, schema: str = STATE_SCHEMA):
    """Open the DDL-capable connection used only by the migration CLI."""

    conn = StateMigrationConnection.connect(dsn, autocommit=False, row_factory=dict_row)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(schema or "")):
        conn.close()
        raise ValueError(f"invalid PostgreSQL state schema: {schema!r}")
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    _set_state_search_path(conn, schema)
    return conn


__all__ = [
    "STATE_SCHEMA",
    "RuntimeStateConnection",
    "RuntimeStateCursor",
    "RuntimeStateSchemaError",
    "RuntimeStateSchemaMissingError",
    "RuntimeStateSchemaWriteError",
    "StateMigrationConnection",
    "connect_state_migration_store",
    "connect_state_store",
    "is_state_schema_write_sql",
    "validate_runtime_state_schema",
]
