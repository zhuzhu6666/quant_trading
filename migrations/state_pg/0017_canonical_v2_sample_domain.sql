-- 0017_canonical_v2_sample_domain.sql
-- 训练样本域表化（P1 方案 B）：镜像 legacy autonomous_learning_sample 列集，
-- content_fingerprint 去重 + 查询索引；内容列后续可平滑升级为 payload 引用。

CREATE TABLE IF NOT EXISTS canonical_v2.training_sample_row (
    sample_id                    TEXT PRIMARY KEY,
    sample_type                  TEXT NOT NULL DEFAULT '',
    source_table                 TEXT NOT NULL DEFAULT '',
    source_id                    TEXT NOT NULL DEFAULT '',
    decision_id                  TEXT NOT NULL DEFAULT '',
    trade_id                     TEXT NOT NULL DEFAULT '',
    position_id                  TEXT NOT NULL DEFAULT '',
    symbol                       TEXT NOT NULL DEFAULT '',
    timeframe                    TEXT NOT NULL DEFAULT '',
    event_ts                     DOUBLE PRECISION,
    label_status                 TEXT NOT NULL DEFAULT '',
    integrity                    TEXT NOT NULL DEFAULT '',
    train_weight                 DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    features_json                TEXT NOT NULL DEFAULT '{}',
    verdict_json                 TEXT NOT NULL DEFAULT '{}',
    label_json                   TEXT NOT NULL DEFAULT '{}',
    trace_json                   TEXT NOT NULL DEFAULT '{}',
    evidence_contract_json       TEXT NOT NULL DEFAULT '{}',
    config_version               INTEGER NOT NULL DEFAULT 0,
    config_hash                  TEXT NOT NULL DEFAULT '',
    evolution_run_id             TEXT NOT NULL DEFAULT '',
    system_contaminated          INTEGER NOT NULL DEFAULT 0,
    governance_eligible          INTEGER NOT NULL DEFAULT 0,
    governance_effective_weight  DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    governance_eligibility_version TEXT NOT NULL DEFAULT '',
    governance_ineligible_reason TEXT NOT NULL DEFAULT '',
    governance_eligibility_fingerprint TEXT NOT NULL DEFAULT '',
    content_fingerprint          TEXT NOT NULL DEFAULT '',
    created_at                   DOUBLE PRECISION NOT NULL,
    updated_at                   DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tsr_sample_type_status
    ON canonical_v2.training_sample_row (sample_type, label_status, governance_eligible);
CREATE INDEX IF NOT EXISTS idx_tsr_decision
    ON canonical_v2.training_sample_row (decision_id);
CREATE INDEX IF NOT EXISTS idx_tsr_fingerprint
    ON canonical_v2.training_sample_row (content_fingerprint, updated_at);
CREATE INDEX IF NOT EXISTS idx_tsr_event_ts
    ON canonical_v2.training_sample_row (event_ts);
