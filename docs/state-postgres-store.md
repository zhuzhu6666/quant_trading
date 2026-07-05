# PostgreSQL State Store

> Status: active
> Last verified: 2026-07-06
> Scope: PostgreSQL `state_v1` runtime state source of truth and legacy SQLite boundary.

## Current Status

Runtime state has been migrated from `data/state.db` to local PostgreSQL schema
`state_v1`.

- PostgreSQL is now the source of truth for live runtime state, recovery state,
  decision ledger, supervisor traces, learning state, and frontend state reads.
- `data/state.db` has been removed. No local SQLite state cold backup is
  retained; runtime state checks must use PostgreSQL `state_v1`.
- Historical `audit_*` dual-write tables are retained as audit/migration
  evidence. They are not the main state schema.

## Configuration

Server-local `.env` contains the state backend settings. Do not commit or print
the DSN value.

```bash
QUANT_STATE_BACKEND=postgres
QUANT_STATE_PG_DSN=postgresql://user:password@host:5432/dbname
```

Legacy audit settings may remain only as historical configuration. Runtime
state writes do not use the SQLite outbox path.

## Migration Artifacts

The historical migration artifact directory is:

```text
data/migration_backups/pg_migration_20260701_162925/
```

It contains:

- `postgres_before_state_v1.sql`: PostgreSQL dump before creating `state_v1`.
- `sqlite_state_manifest.json`: source table/column/index/count manifest.
- No local SQLite `state.db` artifact is retained after cutover.

The one-off migration and parity commands require an explicit external SQLite
backup if a restore is ever needed:

```bash
python scripts/migrate_state_sqlite_to_pg.py --sqlite-db /path/to/external/state.db --drop-schema --backup-dir data/migration_backups/pg_migration_20260701_162925
python scripts/verify_state_pg_parity.py --sqlite-db /path/to/external/state.db --strict
```

The strict parity check passed for all 47 migrated tables before the safe-start
runtime flag was changed in PostgreSQL.

## Operational Notes

- Use `backend.core.db.get_state_pg_conn()` for runtime state access.
- Do not add new production code that writes `data/state.db`.
- Do not inspect live status with `sqlite3 data/state.db` or ad hoc
  `sqlite3.connect("data/state.db")`; that file has been removed by design.
  Use `.venv/bin/python scripts/state_query.py --sql "..."` for read-only
  runtime checks against PostgreSQL `state_v1`.
- Only `scripts/migrate_state_sqlite_to_pg.py` and
  `scripts/verify_state_pg_parity.py` may open a SQLite backup, and only when
  an external backup path is passed explicitly via `--sqlite-db`.
- DuckDB market databases remain separate and are not part of this migration.
- `experiments.db` remains separate unless explicitly migrated later.
- When starting after migration, keep `live.loop.desired_state.enabled=false`
  until frontend/API verification is complete.
