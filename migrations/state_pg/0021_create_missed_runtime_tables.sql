-- 0021: create tables that S7.3 rebuild missed (declared in STATE_DB_DDL / pre-built by
-- code ensure_*, actively read/written, but absent from the PG runtime schema because S7.3
-- only built the _PG_BUSINESS_TABLES_DDL 26-table list).
-- Restores the SQLite full-standard shapes (STATE_DB_DDL) so consumers work unchanged.
-- Dead declarations NOT rebuilt: strategy_perf / sync_health (zero real SQL references).
CREATE TABLE IF NOT EXISTS factor_contribution_review (
    "id" BIGSERIAL PRIMARY KEY,
    "review_id" text NOT NULL,
    "trade_id" text DEFAULT '',
    "factor" text NOT NULL,
    "entry_contribution" double precision DEFAULT 0.0,
    "hold_contribution" double precision DEFAULT 0.0,
    "exit_contribution" double precision DEFAULT 0.0,
    "net_contribution" double precision DEFAULT 0.0,
    "confidence" double precision DEFAULT 0.0,
    "notes" text DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_factor_contribution_review_trade ON factor_contribution_review(trade_id);
CREATE INDEX IF NOT EXISTS idx_factor_contribution_review_factor ON factor_contribution_review(factor);

CREATE TABLE IF NOT EXISTS decision_factor_snapshot (
    "id" BIGSERIAL PRIMARY KEY,
    "decision_id" text NOT NULL,
    "factor" text NOT NULL,
    "source" text DEFAULT 'registry',
    "raw_value" double precision DEFAULT 0.0,
    "normalized_value" double precision DEFAULT 0.0,
    "direction" double precision DEFAULT 0.0,
    "base_weight" double precision DEFAULT 0.0,
    "policy_weight" double precision DEFAULT 0.0,
    "shadow_score" double precision DEFAULT 0.0,
    "health_score" double precision DEFAULT 0.0,
    "gated" integer DEFAULT 0,
    "gated_reason" text DEFAULT '',
    "contribution_score" double precision DEFAULT 0.0,
    "generation" integer NOT NULL DEFAULT 0,
    "artifact_hash" text NOT NULL DEFAULT '',
    "definition_fingerprint" text NOT NULL DEFAULT '',
    "runtime_selection_fingerprint" text NOT NULL DEFAULT '',
    "config_hash" text NOT NULL DEFAULT '',
    "lineage_status" text NOT NULL DEFAULT 'lineage_missing'
);
CREATE INDEX IF NOT EXISTS idx_decision_factor_snapshot_decision ON decision_factor_snapshot(decision_id);
CREATE INDEX IF NOT EXISTS idx_decision_factor_snapshot_factor ON decision_factor_snapshot(factor);
CREATE INDEX IF NOT EXISTS idx_decision_factor_snapshot_factor_id ON decision_factor_snapshot(factor, id DESC);

CREATE TABLE IF NOT EXISTS calibrator (
    "id" BIGSERIAL PRIMARY KEY,
    "data_json" text NOT NULL,
    "updated_at" double precision
);

CREATE TABLE IF NOT EXISTS decision_log (
    "id" BIGSERIAL PRIMARY KEY,
    "run_id" integer,
    "ts" double precision,
    "bar_date" text,
    "decision_type" text NOT NULL,
    "strategy" text,
    "regime" text,
    "direction" integer,
    "confidence" double precision,
    "factor_scores" text,
    "decision" text,
    "meta" text DEFAULT '{}',
    "created_at" double precision
);
CREATE INDEX IF NOT EXISTS idx_decision_log_ts ON decision_log(ts);
CREATE INDEX IF NOT EXISTS idx_decision_log_type ON decision_log(decision_type);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    "id" BIGSERIAL PRIMARY KEY,
    "timestamp" double precision NOT NULL,
    "event" text NOT NULL,
    "factor" text NOT NULL,
    "source" text DEFAULT '',
    "description" text DEFAULT '',
    "score" double precision DEFAULT 0.0,
    "status" text DEFAULT '',
    "reason" text DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_factor ON lifecycle_events(factor);
CREATE INDEX IF NOT EXISTS idx_lifecycle_events_event ON lifecycle_events(event);

CREATE TABLE IF NOT EXISTS shadow_trades (
    "id" BIGSERIAL PRIMARY KEY,
    "factor" text NOT NULL,
    "symbol" text DEFAULT '',
    "timeframe" text DEFAULT '',
    "ts" double precision,
    "signal" double precision DEFAULT 0.0,
    "position" integer DEFAULT 0,
    "pnl" double precision DEFAULT 0.0,
    "created_at" double precision
);
CREATE INDEX IF NOT EXISTS idx_shadow_trades_factor_ts ON shadow_trades(factor, ts);

CREATE TABLE IF NOT EXISTS weight_history (
    "id" BIGSERIAL PRIMARY KEY,
    "timestamp" double precision NOT NULL,
    "factor" text NOT NULL,
    "old_weight" double precision,
    "new_weight" double precision,
    "reason" text DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_weight_history_factor ON weight_history(factor);
