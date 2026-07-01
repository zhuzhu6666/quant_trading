# PostgreSQL State Store

Last updated: 2026-07-01

## Current Status

Runtime state has been migrated from `data/state.db` to local PostgreSQL schema
`state_v1`.

- PostgreSQL is now the source of truth for live runtime state, recovery state,
  decision ledger, supervisor traces, learning state, and frontend state reads.
- `data/state.db` is retained only as the migration cold backup and rollback
  source.
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

The migration cold backup directory is:

```text
data/migration_backups/pg_migration_20260701_162925/
```

It contains:

- `state.db`: SQLite cold backup before PostgreSQL cutover.
- `postgres_before_state_v1.sql`: PostgreSQL dump before creating `state_v1`.
- `sqlite_state_manifest.json`: source table/column/index/count manifest.

The full migration and parity commands are:

```bash
python scripts/migrate_state_sqlite_to_pg.py --drop-schema --backup-dir data/migration_backups/pg_migration_20260701_162925
python scripts/verify_state_pg_parity.py --strict
```

The strict parity check passed for all 47 migrated tables before the safe-start
runtime flag was changed in PostgreSQL.

## Operational Notes

- Use `backend.core.db.get_state_pg_conn()` for runtime state access.
- Do not add new production code that writes `data/state.db`.
- DuckDB market databases remain separate and are not part of this migration.
- `experiments.db` remains separate unless explicitly migrated later.
- When starting after migration, keep `live.loop.desired_state.enabled=false`
  until frontend/API verification is complete.
