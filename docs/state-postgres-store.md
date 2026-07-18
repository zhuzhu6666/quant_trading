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
python scripts/state_schema_migrate.py --apply --runner-id external_restore_preflight
python scripts/migrate_state_sqlite_to_pg.py --sqlite-db /path/to/external/state.db --backup-dir data/migration_backups/pg_migration_20260701_162925
python scripts/verify_state_pg_parity.py --sqlite-db /path/to/external/state.db --strict
```

The strict parity check passed for all 47 migrated tables before the safe-start
runtime flag was changed in PostgreSQL.

## Operational Notes

- Use `backend.core.db.get_state_pg_conn()` for runtime state access.
- Use `get_state_pg_conn(read_only=True)` for runtime checks and projections;
  the connector sets PostgreSQL's session read-only default before its first
  transaction, so commit/rollback cannot turn the reused handle into a writer.
- Do not add new production code that writes `data/state.db`.
- Do not inspect live status with `sqlite3 data/state.db` or ad hoc
  `sqlite3.connect("data/state.db")`; that file has been removed by design.
  Use `.venv/bin/python scripts/state_query.py --sql "..."` for read-only
  runtime checks against PostgreSQL `state_v1`.
- Only `scripts/migrate_state_sqlite_to_pg.py` and
  `scripts/verify_state_pg_parity.py` may open a SQLite backup, and only when
  an external backup path is passed explicitly via `--sqlite-db`.
- `migrate_state_sqlite_to_pg.py` only validates target tables/columns and
  imports data. It cannot create/drop a schema, create/alter tables, or create
  indexes; `state_schema_migrate.py --apply` is the sole PostgreSQL schema writer.
- DuckDB market databases remain separate and are not part of this migration.
- `experiments.db` remains a separate SQLite research store. Its full
  experiments/model registry/shadow/canary/inference schema is written only by
  the explicit `scripts/experiments_schema_migrate.py --apply` path
  (`db_doctor --repair` remains a broader operator compatibility wrapper).
  Backend/worker model
  constructors call read-only validation for the canonical file; only a
  caller-supplied noncanonical path may self-initialize as an isolated fixture.
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

The separate canonical research store uses the same explicit check/apply
operator shape, without PostgreSQL privileges:

```bash
./.venv/bin/python scripts/experiments_schema_migrate.py --check
./.venv/bin/python scripts/experiments_schema_migrate.py --apply
./.venv/bin/python scripts/experiments_schema_migrate.py --check
```

The runner:

- validates the migrated baseline before changing anything;
- takes a transaction-scoped PostgreSQL advisory lock without waiting
  indefinitely for another runner;
- uses a 5-second table-lock timeout and 120-second statement timeout;
- applies additive DDL and the checksum-protected ledger row in one
  transaction;
- treats a changed checksum/name, missing version, or missing baseline table as
  a failure; runtime catalog assertions separately reject a same-name index
  whose table/key contract differs;
- is a no-op when every checked-in migration is already recorded.

`quant-backend` and `quant-learning-worker` do not apply migrations. At startup
they validate `STATE_SCHEMA_MIN_VERSION=9`; a version mismatch is blocking even
when the backend is otherwise in dry-run mode. Backend startup performs this
gate before restoring RuntimeConfig overlay because overlay restore can ensure
tables and persist a startup snapshot.

The deployment order is therefore fixed:

```text
backup/inspect -> migration --apply -> migration --check -> deploy compatible code
-> inspect overlay authority/operator review -> experiments schema --apply/check -> restart backend/worker
-> health/readiness verification
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

Migration `0002_phase3_governance_coordinator.sql` adds only coordinator
recovery/projection metadata and the partial unique index that permits one
`reserved/prepared` mutation per control surface and scope. Applying v2 does
not enable the coordinator; `governance_mutation_coordinator_v2_mode` remains
the release-time `off|dual_record|enforce` switch and defaults to `off`.

Migration `0003_phase5_persistent_job_queue.sql` adds the leased PostgreSQL job
queue fields/indexes and the remaining v3 compatibility objects used by
supervisor history, reservations, Proposal Registry and V16 claims.

Migration `0004_phase5_runtime_schema_writer_retirement.sql` materializes the
worker/research audit object family formerly created by backend/worker ensure paths:
factor/open/position/meta/LLM shadow audit, meta report snapshots, model
influence decision/effect and off-market job audit tables plus their indexes.
After v4, ordinary state connections and cursors cannot execute schema writes.
Legacy idempotent ensure SQL is interpreted as non-mutating table/column/index
validation; index assertions compare target table, uniqueness, ordered key
definition and predicate presence rather than accepting a matching name alone.
Missing or mismatched objects fail with an instruction to add an additive
migration and run the migration CLI.

Migration `0005_phase3_governance_eligibility_weighting.sql` adds deterministic
eligibility fingerprints to learning samples and policy suggestions, plus
effective-sample and weighted win/loss/reward fields on
`experience_pattern_stats`. Historical rows keep empty version/fingerprint and
weight zero until the explicit evidence-contract repair/backfill reevaluates
them; an empty field is never treated as compatibility approval.

Migration `0006_phase3_factor_lifecycle_identity.sql` adds the database-level
unique index on `factor_lifecycle_state.factor_name`. The canonical DSL-AST
SHA-256 remains the primary `factor_id`; the name index prevents concurrent
workers from committing two definitions behind one runtime-facing name. The
deployment preflight must reject existing duplicates before applying v6.

Migration `0007_phase3_v16_authority_freshness.sql` adds
`v16_brain_command.authority_issued_at`, backfills it from the original command
creation time, and indexes it for delegation lookup. Claim, release, finalize,
and expired-claim recovery may continue updating operational `updated_at`, but
authorization freshness is evaluated only from the immutable authority time;
an absent/zero value fails closed.

Migration `0008_phase5_runtime_schema_contract_completion.sql` finishes the
catalog contract for high-frequency backend/learning-worker paths. It owns
`runtime_kv` and factor `canary_state`, materializes the remaining legacy
columns previously guarded by service-local ALTER code, and creates
`idx_experience_memory_source_append` for the actual
`(source_table, source_id, append_source)` idempotency lookup. The older
`idx_experience_memory_source` is retained because v4 used that name for a
different additive read index; v8 does not drop or rewrite it.

Migration `0009_phase3_runtime_overlay_authority.sql` adds the non-null
`runtime_config_overlay.legacy_authority_json` compatibility manifest and the
`(mutation_id, updated_at)` lookup index. It does not grandfather existing
blank `mutation_id` rows. Before restarting backend/learning worker, inspect the
exact overlay hash, mutation ID and keys. A nonempty mutation must resolve to a
`committed/current` intent with matching target/committed config hash and a
nonempty domain hash. For a historical blank mutation, an operator may call
`RuntimeConfigOverlayService.review_legacy_quarantine()` only for keys that the
central before/after classifier derives as `risk_tightening`; the review is
bound to that exact overlay hash and remains blocked until every overlay key is
reviewed. Never label an expansion or unknown key as legacy tightening. Leave
the no-new-risk latch active and rebuild those controls through typed committed
mutations (or explicitly clear/reconstruct the overlay) before release.

`governance_eligibility_version=''` means a historical row has not been
evaluated; `system_contaminated=0` alone must not be interpreted as verified
clean evidence. `governance_effective_weight` is reserved for governance
mutation aggregation and does not replace the existing model-training
`train_weight`.

Service-local compatibility SQL may remain temporarily for SQLite fixtures and
as PostgreSQL catalog declarations. Worker/model/readiness paths call
`validate_runtime_state_schema()` explicitly; the guarded ordinary connection
and cursor remain the final backstop for older ensures. Neither path can mutate
PostgreSQL. A stable
release should additionally revoke CREATE/ALTER/DROP from the runtime database
role so the database enforces the same boundary independently of application
code.
