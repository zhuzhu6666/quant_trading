CREATE TABLE IF NOT EXISTS risk_daily_equity (
    account_key TEXT NOT NULL,
    risk_date TEXT NOT NULL,
    equity DOUBLE PRECISION NOT NULL CHECK (equity > 0),
    observed_at DOUBLE PRECISION NOT NULL,
    account_reconcile_id TEXT NOT NULL,
    PRIMARY KEY (account_key, risk_date)
);

CREATE INDEX IF NOT EXISTS idx_risk_daily_equity_observed
    ON risk_daily_equity(account_key, observed_at DESC);
