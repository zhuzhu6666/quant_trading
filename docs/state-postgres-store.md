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

## Versioned Forward Migrations

Forward changes to PostgreSQL `state_v1` are now recorded in
`state_schema_migration`. Checked-in migrations live under
`migrations/state_pg/` and are applied only by the operator command:

```bash
# Read-only and the default mode. Exit code 2 means the runtime minimum is not met.
./.venv/bin/python scripts/state_schema_migrate.py --check

# The only supported write mode; --apply must be explicit.
./.venv/bin/python scripts/state_schema_migrate.py --apply --runner-id phase0b_deploy

# Verify before deploying/restarting processes that require the new version.
./.venv/bin/python scripts/state_schema_migrate.py --check
```

The runner:

- validates the migrated baseline before changing anything;
- takes a transaction-scoped PostgreSQL advisory lock without waiting
  indefinitely for another runner;
- uses a 5-second table-lock timeout and 120-second statement timeout;
- applies additive DDL and the checksum-protected ledger row in one
  transaction;
- treats a changed checksum/name, missing version, missing baseline table, or
  same-name schema object as a failure;
- is a no-op when every checked-in migration is already recorded.

`quant-backend` and `quant-learning-worker` do not apply migrations. At startup
they validate `STATE_SCHEMA_MIN_VERSION`; a version mismatch is blocking even
when the backend is otherwise in dry-run mode. Backend startup performs this
gate before restoring RuntimeConfig overlay because overlay restore can ensure
tables and persist a startup snapshot.

The deployment order is therefore fixed:

```text
backup/inspect -> migration --apply -> migration --check -> deploy code
-> restart backend/worker -> health/readiness verification
```

Migration `0001_phase0b_foundation.sql` only creates additive foundations. It
does not switch any live writer or backfill historical rows:

- `broker_execution_intent`
- `governance_mutation_intent`
- `factor_lifecycle_state`
- `factor_runtime_projection`
- `auth_session`
- mutation/finalization/governance-eligibility linkage columns on existing
  ledgers

`governance_eligibility_version=''` means a historical row has not been
evaluated; `system_contaminated=0` alone must not be interpreted as verified
clean evidence. `governance_effective_weight` is reserved for governance
mutation aggregation and does not replace the existing model-training
`train_weight`.

Existing service-local dynamic `CREATE/ALTER` compatibility remains temporarily
in place. It is not the forward migration authority and will be retired in a
separate phase after every legacy statement has a versioned equivalent and the
runtime database role can drop DDL privileges.
