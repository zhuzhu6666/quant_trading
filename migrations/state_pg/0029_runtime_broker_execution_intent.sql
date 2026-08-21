-- 0029: single execution-outcome intent ledger.
--
-- The broker mutation path has one authority: runtime.broker_execution_intent.
-- This table is created only by the explicit migration runner.  Application
-- code must fail closed when it is unavailable and must never create it with
-- ensure-DDL at runtime.

CREATE TABLE runtime.broker_execution_intent (
    intent_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL DEFAULT '',
    trade_id TEXT NOT NULL DEFAULT '',
    position_id TEXT NOT NULL DEFAULT '',
    broker TEXT NOT NULL DEFAULT 'ctrader',
    account_id TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL CHECK (action IN ('market_open', 'close_position', 'reduce_position', 'amend_position_sltp')),
    side TEXT NOT NULL DEFAULT '' CHECK (side IN ('', 'buy', 'sell')),
    requested_volume DOUBLE PRECISION NOT NULL DEFAULT 0.0 CHECK (requested_volume >= 0.0),
    requested_price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    target_stop_loss DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    target_take_profit DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'prepared'
        CHECK (status IN ('prepared', 'submitting', 'confirmed', 'rejected', 'unknown')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    broker_order_id TEXT NOT NULL DEFAULT '',
    request_json TEXT NOT NULL DEFAULT '{}',
    risk_verdict_json TEXT NOT NULL DEFAULT '{}',
    broker_response_json TEXT NOT NULL DEFAULT '{}',
    error_json TEXT NOT NULL DEFAULT '{}',
    config_version BIGINT NOT NULL DEFAULT 0,
    config_hash TEXT NOT NULL DEFAULT '',
    prepared_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    submitted_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    completed_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at DOUBLE PRECISION NOT NULL DEFAULT 0.0
);

CREATE INDEX idx_broker_execution_intent_status
    ON runtime.broker_execution_intent(status, updated_at);
CREATE INDEX idx_broker_execution_intent_decision
    ON runtime.broker_execution_intent(decision_id, created_at);
CREATE INDEX idx_broker_execution_intent_position
    ON runtime.broker_execution_intent(position_id, created_at);
