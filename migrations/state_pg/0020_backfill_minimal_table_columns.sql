-- 0020: backfill columns on S7.3-minimal tables to match the SQLite full standard (STATE_DB_DDL) that production stores/readers consume.
-- Conservative: only ADDs missing columns (existing wrong-named columns are left untouched)
-- Root cause: S7.3 rebuild used _PG_BUSINESS_TABLES_DDL minimal definitions while code
-- consumes the full SQLite-standard columns. These 6 tables still diverge (others are
-- already caught up via ensure_*/later migrations).

ALTER TABLE decision_ledger
    ADD COLUMN IF NOT EXISTS "action_json" text DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS "action_reason" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "action_score" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "event_type" text,
    ADD COLUMN IF NOT EXISTS "factor_set_version" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "policy_version" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "portfolio_state_json" text DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS "position_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "regime_confidence" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "regime_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "risk_state_json" text DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS "symbol" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "timeframe" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "trade_id" text DEFAULT '';
ALTER TABLE factor_health
    ADD COLUMN IF NOT EXISTS "factor" text,
    ADD COLUMN IF NOT EXISTS "n_obs" integer DEFAULT 0,
    ADD COLUMN IF NOT EXISTS "score" double precision DEFAULT 50.0,
    ADD COLUMN IF NOT EXISTS "section" text DEFAULT 'unknown';
ALTER TABLE order_lifecycle_event
    ADD COLUMN IF NOT EXISTS "broker_order_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "decision_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "details_json" text DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS "order_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "price" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "status" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "trade_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "volume" double precision DEFAULT 0.0;
ALTER TABLE position_lifecycle_event
    ADD COLUMN IF NOT EXISTS "avg_price" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "details_json" text DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS "net_volume" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "position_id" text,
    ADD COLUMN IF NOT EXISTS "realized_pnl" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "symbol" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "trade_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "unrealized_pnl" double precision DEFAULT 0.0;
ALTER TABLE recovery_position_state
    ADD COLUMN IF NOT EXISTS "broker" text DEFAULT 'ctrader',
    ADD COLUMN IF NOT EXISTS "close_pnl" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "close_reason" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "closed_at" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "context_integrity" text DEFAULT 'full',
    ADD COLUMN IF NOT EXISTS "direction" integer DEFAULT 0,
    ADD COLUMN IF NOT EXISTS "entry_decision_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "first_seen_at" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "open_price" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "recovery_meta_json" text DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS "status" text DEFAULT 'open',
    ADD COLUMN IF NOT EXISTS "strategy_name" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "symbol" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "volume" double precision DEFAULT 0.0;
ALTER TABLE trade_outcome_review
    ADD COLUMN IF NOT EXISTS "entry_decision_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "entry_quality" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "execution_quality" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "exit_decision_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "exit_quality" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "failure_tags_json" text DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS "hold_quality" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "mae" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "mfe" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "outcome_label" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "pnl" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "position_id" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "regime_fit_score" double precision DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS "summary_text" text DEFAULT '',
    ADD COLUMN IF NOT EXISTS "trade_id" text DEFAULT '';

-- 补列后，此前 0019 因"列不存在"排除的缺列索引现可建立（收敛 S7.3 索引欠账）。
-- 仅纳入依赖本次补列的索引；引用标准外列（execution_intent_id / review_archive_hash）
-- 的孤儿索引不建（无标准对应列）。
CREATE INDEX IF NOT EXISTS idx_decision_ledger_pos_event ON decision_ledger(position_id, event_type);
CREATE INDEX IF NOT EXISTS idx_order_lifecycle_trade ON order_lifecycle_event(trade_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_position_lifecycle_pos ON position_lifecycle_event(position_id, event_ts);
CREATE INDEX IF NOT EXISTS idx_trade_outcome_review_trade ON trade_outcome_review(trade_id);
CREATE INDEX IF NOT EXISTS idx_recovery_position_status ON recovery_position_state(status, broker, last_seen_at);
