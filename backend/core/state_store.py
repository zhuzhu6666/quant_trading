"""Direct PostgreSQL state store connections."""

from __future__ import annotations

from typing import Final

import psycopg
from psycopg.rows import dict_row


STATE_SCHEMA: Final[str] = "state_v1"


def connect_state_store(dsn: str, *, read_only: bool = False, schema: str = STATE_SCHEMA):
    conn = psycopg.connect(dsn, autocommit=False, row_factory=dict_row)
    if not read_only:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.execute(f'SET search_path TO "{schema}", public')
    if read_only:
        conn.execute("SET TRANSACTION READ ONLY")
    return conn
