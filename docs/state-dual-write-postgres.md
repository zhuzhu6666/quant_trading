# PostgreSQL State Dual-Write Audit

Last updated: 2026-07-01

## Purpose

`state.db` remains the source of truth for live trading, risk, learning, and
runtime state.  PostgreSQL dual-write is a migration audit layer only.  It must
not block the live loop, order flow, or risk decisions when PostgreSQL is down.

The dual-written scope is still an audit/migration layer, but it now covers
both append-only decision evidence and the key state rows needed to reconstruct
the learning/control plane:

- `decision_ledger`
- full `decision_factor_snapshot`
- `lifecycle_events`
- `runtime_kv`
- `recovery_position_state`
- `position_supervisor_trace`
- `autonomous_learning_sample`
- `evolution_events`

Data granularity is preserved. State rows are mirrored as full JSON payloads in
PostgreSQL rather than downsampled summaries.

## Configuration

Set these values in `.env` or the systemd environment.  Do not commit secrets.

```bash
QUANT_AUDIT_PG_DSN=postgresql://user:password@host:5432/dbname
QUANT_AUDIT_PG_DUAL_WRITE=true
```

If the DSN is missing or `QUANT_AUDIT_PG_DUAL_WRITE` is not true, the system
continues to write SQLite only.

## Current Server Status

The Linux backend server is currently configured with a local PostgreSQL 16
audit sink:

- service: `postgresql`
- database: `quant_audit`
- login role: `quant_audit`
- DSN location: server-local `.env`
- enabled flag: `QUANT_AUDIT_PG_DUAL_WRITE=true`

The DSN contains a generated password and must not be printed into logs,
committed, or copied into documentation.  `.env` remains the only configured
secret location for this stage.

## Data Flow

1. SQLite writes remain the first write and the source of truth.
2. The same SQLite transaction queues a replayable row in
   `state_dual_write_outbox` for state rows that are already inside an open
   transaction. Decision ledger events may enqueue immediately after their
   SQLite transaction succeeds.
3. A background worker reads pending outbox rows and writes PostgreSQL audit
   tables.
4. Successful outbox rows are marked `synced`; failures are marked `retry` with
   `attempts` and `last_error`.

Decision events use `decision_id` as the outbox `event_id`. State-row mirror
events use a deterministic hash of table, entity key, operation, timestamp, and
payload hash, so replay is idempotent while preserving every distinct state
mutation event.

## PostgreSQL Tables

- `audit_decision_ledger`: mirrors `decision_ledger`, plus
  `schema_version`, `source_db`, `outbox_event_id`, `synced_at`.
- `audit_decision_factor_snapshot`: mirrors every factor snapshot, plus
  `snapshot_seq`, `schema_version`, `source_db`, `outbox_event_id`,
  `synced_at`.
- `audit_state_mirror_event`: append-only state-row mirror event stream. Each
  event contains `table_name`, `entity_key`, operation, full `payload_json`,
  payload hash, source timestamp, and sync metadata.
- `audit_state_mirror_latest`: latest full JSON state per
  `(table_name, entity_key)`, useful for read-heavy audit/reporting queries.

`snapshot_seq` is generated from the original factor snapshot order and does not
depend on SQLite autoincrement ids.

## Operations

Inspect status:

```bash
python scripts/state_dual_write_status.py --json
```

Expected enabled state on the server:

```text
enabled: true
dsn_configured: true
```

Check PostgreSQL table counts without exposing the DSN:

```bash
.venv/bin/python - <<'PY'
from backend.services.state_dual_write import audit_pg_dsn
import psycopg

with psycopg.connect(audit_pg_dsn()) as conn:
    for table in ["audit_decision_ledger", "audit_decision_factor_snapshot"]:
        print(table, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
PY
```

Flush one batch manually:

```bash
python scripts/state_dual_write_status.py --flush-once --limit 50 --json
```

Run database doctor:

```bash
python scripts/db_doctor.py
```

## Migration Path

1. Keep SQLite as source of truth and dual-write new decisions/state rows.
2. Verify PostgreSQL row counts and outbox `synced` counts.
3. Add a separate offline historical backfill from `state.db` to PostgreSQL for
   rows written before dual-write coverage existed.
4. Switch read-heavy audit/learning queries to PostgreSQL once coverage and
   backfill parity are verified.
5. Later split hot transactional state and cold append-only evidence into the
   final storage layout.
