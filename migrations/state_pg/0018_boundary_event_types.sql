-- 0018_boundary_event_types.sql
-- P2 边界 4 域事件化：扩展 canonical_v2.event 的 event_type CHECK 约束，
-- 新增 counterfactual_review / supervisor_trace / broker_deal 三种事件类型。

ALTER TABLE canonical_v2.event DROP CONSTRAINT event_event_type_check;

ALTER TABLE canonical_v2.event ADD CONSTRAINT event_event_type_check
CHECK (event_type = ANY (ARRAY[
    'market_observation', 'broker_execution', 'position_transition',
    'risk_decision', 'factor_observation', 'governance_proposal',
    'governance_command', 'governance_effect', 'trade_review',
    'label_observation', 'training_run',
    'counterfactual_review', 'supervisor_trace', 'broker_deal'
]::text[]));
