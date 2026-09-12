"""Live trading service.

Responsibilities:
- Probe broker connection status (cTrader)
- Read real account info (balance / equity / margin / leverage)
- Read real positions (open trades)
- Start/stop the live trading loop as a background **thread** in the backend
  process (not a subprocess — keeps state in the same memory space as the
  WS broadcaster, so /ws/state can include live account info)
- Emergency close all positions on a broker

(audit 2026-06-08: previous version only had status probes and emergency
close. live/start + live/stop were placeholders returning "not implemented
in v1", forcing the user to SSH in and run `python main.py --mode live` by
hand. v8 added real thread management so the Web 总览 can drive the
trading loop from the browser.)
"""
import copy
from dataclasses import asdict, is_dataclass
from functools import partial
from pathlib import Path
import threading
import time
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Mapping

from loguru import logger

import os
import pandas as pd
import numpy as _np
from pathlib import Path

from backend.ledger.service import DecisionLedger
from alpha.reflection.reviewer import TradeReviewer
from research.learning.experience_builder import ExperienceBuilder
from research.learning.policy_suggester import PolicySuggester
from risk.policy_service import INCIDENT_MODE_RANK, RiskPolicyService, RiskVerdict
from risk.runtime_policy import RiskLimitSnapshot
from backend.services.incident_controls import RuntimeIncidentControlService
from backend.core.static_feature_flags import shared_static_feature_flags
from backend.services.live_loop_controller import LiveLoopController
from backend.services.live_safety_plane import SafetyCandidate
from backend.services.live_safety_planner import (
    plan_live_safety_candidates,
    protection_candidate_to_safety,
    safety_candidate,
)
from backend.services.live_safety_state import (
    activate_no_new_risk_latch,
    append_safety_outbox,
    no_new_risk_latched,
    no_new_risk_latch_status,
    release_no_new_risk_latch_cause,
)
from backend.services.live_safety_shadow_observation import (
    build_safety_shadow_observer,
    safety_shadow_gate_status,
)
from backend.services.live_safety_watchdog import (
    evaluate_safety_freshness,
)
from backend.core.db import state_table_columns
from backend.services.canonical_v2_reader import (
    canonical_ready,
    iter_decision_rows,
    iter_supervisor_trace_rows,
    load_position_decision_index,
)
from backend.services.live_reconciliation import (
    LIVE_SAFETY_FRESHNESS_SEC as _LIVE_SAFETY_FRESHNESS_SEC,
    evaluate_reconciliation_snapshot as _evaluate_reconciliation_snapshot,
    explicit_account_reconcile as _explicit_account_reconcile,
    explicit_position_reconcile as _explicit_position_reconcile,
    fresh_observation_timestamp as _fresh_observation_timestamp,
    reconcile_value as _reconcile_value,
    verify_position_protection_projection as _verify_position_protection_projection,
)
from backend.services.live_loop_v2 import (
    LiveSafetyCycleRuntime,
    StartupBarrierRuntime,
    attempt_generation_startup_barrier as _loop_v2_attempt_startup_barrier,
    run_live_safety_cycle as _loop_v2_run_safety_cycle,
)
from backend.services.live_loop_bootstrap import (
    BarWarmupRuntime,
    StartupSafetyRuntime,
    run_startup_safety_cycle as _bootstrap_run_startup_safety,
    warmup_live_bars as _bootstrap_warmup_live_bars,
)
from backend.services.live_loop_runner import (
    SerialLiveTickRuntime,
    run_serial_live_ticks as _runtime_run_serial_live_ticks,
)
from backend.services.live_loop_stop import (
    LiveLoopStopRuntime,
    stop_live_loop as _runtime_stop_live_loop,
)
from backend.services.live_loop_start import (
    LiveLoopStartRuntime,
    start_live_loop as _runtime_start_live_loop,
)
from backend.services.live_loop_tick_runtime import (
    LiveLoopTickRuntime,
    run_live_loop_tick_body as _runtime_run_live_loop_tick_body,
)
from backend.services.live_execution_recovery import (
    ExecutionRecoveryRuntime,
    PositionRecoveryRuntime,
    bootstrap_position_recovery as _runtime_bootstrap_position_recovery,
    recover_emergency_execution_intents as _runtime_recover_emergency_intents,
    recover_execution_outcomes_before_alpha as _loop_recover_execution_outcomes,
)
from backend.services.live_emergency import (
    EmergencyCloseRuntime,
    fresh_emergency_position_reconcile as _fresh_emergency_position_reconcile,
    run_emergency_close as _run_emergency_close,
)
from backend.services.live_entry_protection import (
    EntryProtectionLatchRuntime,
    activate_entry_protection_pending_latch as _entry_protection_activate_latch,
    release_entry_protection_pending_latch as _entry_protection_release_latch,
)
from backend.services.live_open_admission import (
    evaluate_final_open_admission as _evaluate_final_open_admission,
    probe_postgres_authority as _probe_postgres_authority,
)
from backend.services.live_open_risk_context import (
    OpenLearningContextRuntime,
    OpenRiskContextRuntime,
    build_open_learning_context as _runtime_build_open_learning_context,
    build_open_trade_risk_context as _runtime_build_open_risk_context,
)
from backend.services.live_committed_policy import load_live_policy_controls
from backend.services.live_readiness import build_live_readiness
from backend.services.live_learning_policy import (
    LiveLearningPolicyRuntime,
    load_active_learning_policy,
)
from backend.services.live_reentry_guard import (
    ReentryGuardRuntime,
    active_supervisor_reentry_block as _guard_active_reentry_block,
    pending_supervisor_reentry_block_from_positions as _guard_pending_reentry_block,
    recent_review_reentry_block as _guard_recent_review_reentry_block,
    remember_supervisor_reentry_block as _guard_remember_reentry_block,
)
from backend.services.live_safety_candidate_execution import (
    SafetyCandidateExecutionRuntime,
    execute_live_safety_candidate as _runtime_execute_safety_candidate,
)
from backend.services.live_position_protection_cycle import (
    ProtectionCandidate,
    PositionProtectionCycleRuntime,
    run_position_protection_cycle as _runtime_run_position_protection_cycle,
)
from backend.services.live_recovery_position_store import (
    RecoveryPositionStore,
    RecoveryPositionStoreRuntime,
)
from backend.services.live_recovery_close import (
    MissingPositionRetirementRuntime,
    RecoveredCloseReplayRuntime,
    replay_recovered_close as _runtime_replay_recovered_close,
    retire_broker_missing_position as _runtime_retire_missing_position,
)
from backend.services.live_closed_position_cycle import (
    ClosedPositionCycleRuntime,
    handle_closed_positions_after_tick as _runtime_handle_closed_positions,
)
from backend.services.live_closed_position_processing import (
    ClosedPositionProcessingRuntime,
    cleanup_closed_position as _runtime_cleanup_closed_position,
    collect_closed_position_attribution as _runtime_collect_close_attribution,
    log_closed_position_ledger as _runtime_log_closed_position_ledger,
    run_closed_position_learning as _runtime_run_closed_position_learning,
)
from backend.services.live_open_submission import (
    OpenSubmissionRuntime,
    finalize_nursery_reservation as _runtime_finalize_nursery_reservation,
    submit_open_trade_candidate as _runtime_submit_open_trade_candidate,
)
from backend.services.live_open_protection import (
    OpenProtectionRequest,
    OpenProtectionRuntime,
    attach_open_trade_protection as _runtime_attach_open_trade_protection,
)
from backend.services.live_open_processing import (
    AmendFailureRequest,
    AmendFailureRuntime,
    AmendedOpenSuccessRequest,
    AmendedOpenSuccessRuntime,
    FilledOpenRequest,
    FilledOpenRuntime,
    record_amend_failure_after_fill as _runtime_record_amend_failure,
    record_amended_open_success_context as _runtime_record_amended_success,
    record_filled_position_open_context as _runtime_record_filled_open,
)
from backend.services.live_risk_reduction import (
    RiskReductionRuntime,
    build_close_position_risk_context as _risk_reduction_build_close_context,
    evaluate_risk_reduction_policy as _risk_reduction_evaluate_policy,
    load_recovery_row_for_risk_reduction as _risk_reduction_load_recovery_row,
    lookup_entry_context_for_risk_reduction as _risk_reduction_lookup_entry_context,
    lookup_entry_decision_for_risk_reduction as _risk_reduction_lookup_entry_decision,
    record_risk_reduction_aux_failure as _risk_reduction_record_aux_failure,
)
from backend.services.market_session import evaluate_market_session
from backend.services.review_contract import build_entry_timing_context
from backend.services.live_runtime_state import (
    cache_get_or_refresh as _runtime_cache_get_or_refresh,
    default_live_state,
    safe_container_snapshot as _safe_container_snapshot,
    state_get as _runtime_state_get,
    state_set as _runtime_state_set,
    state_update as _runtime_state_update,
)
from backend.services import live_close_settlement
from backend.services import live_open_processing
from backend.services import live_open_pipeline
from backend.services import live_bar_warmup
from backend.services import live_factor_bootstrap
from backend.services import live_loop_tick_runtime
from backend.services import live_safety_watchdog
from backend.services import live_safety_plane
from backend.services import live_safety_planner
from backend.services import live_state_store
from backend.services.live_state_store import (
    _LIVE_STATE_LOCK,
    _live_state,
    live_state_get,
    live_state_set,
    live_state_snapshot,
    live_state_update,
)
from backend.services.session_restore import (
    PartialCloseSessionFactRuntime,
    authoritative_close_pnl as _authoritative_close_pnl,
    build_authoritative_session_state as _session_build_authoritative_state,
    load_authoritative_session_deal_facts as _session_load_authoritative_deal_facts,
    resolve_session_restore as _session_resolve_restore,
    session_trade_window as _session_restore_trade_window,
    sync_partial_close_session_fact as _session_sync_partial_close_fact,
)
from backend.services.live_ctrader_runtime import CTraderRuntime
from backend.services.live_data_sync_job import make_data_sync_job as _make_data_sync_job
from backend.services.live_data_sync_helpers import (
    DATA_SYNC_CRON as _DATA_SYNC_CRON,
)
from backend.services.live_decision_pipeline import (
    run_live_decision_pipeline as _decision_run_live_decision_pipeline,
)
from backend.services.live_factor_state import (
    commit_ready_factor_decision as _factor_state_commit_ready_decision,
    resolve_decision_bar_progress as _factor_state_resolve_bar_progress,
)
from config.runtime_config import (
    autonomy_expansion_freeze_applies,
    bounded_demo_mode_active,
)
from backend.services.live_loop_shell import (
    acknowledge_prepared_factor_projections as _loop_ack_prepared_factor_projections,
    compare_spot_quote_to_latest_bar as _loop_compare_spot_quote_to_latest_bar,
    apply_factor_pipeline_config_update as _loop_apply_factor_pipeline_config_update,
    bridge_readiness_label as _loop_bridge_readiness_label,
    build_extra_symbol_factor_pipelines as _loop_build_extra_symbol_factor_pipelines,
    collect_open_risk_runtime_health as _loop_collect_open_risk_runtime_health,
    cross_asset_symbols_for_config as _loop_cross_asset_symbols_for_config,
    build_warmup_feed as _loop_build_warmup_feed,
    enabled_symbols_from_config as _loop_enabled_symbols_from_config,
    execution_gate_config as _loop_execution_gate_config,
    loop_identity_snapshot as _loop_identity_snapshot,
    market_closed_log_message as _loop_market_closed_log_message,
    mark_loop_stopped_for_display as _loop_mark_stopped_for_display,
    subscribe_spot_once as _loop_subscribe_spot_once,
    unique_factor_pipelines as _loop_unique_factor_pipelines,
)
from backend.services.live_risk_sizing import (
    apply_entry_event_sizing as _sizing_apply_entry_event_sizing,
    build_event_sizing_fallback_context as _sizing_build_event_sizing_fallback_context,
    ceil_api_volume_to_step as _sizing_ceil_api_volume_to_step,
    floor_api_volume_to_step as _sizing_floor_api_volume_to_step,
    normalize_event_sizing_context as _sizing_normalize_event_sizing_context,
    protection_prices_from_reference as _sizing_protection_prices_from_reference,
    risk_kelly_sizing as _sizing_risk_kelly_sizing,
    round_api_volume_to_step as _sizing_round_api_volume_to_step,
    should_full_close_untradeable_reduce as _sizing_should_full_close_untradeable_reduce,
)
from backend.services.live_supervision_actions import (
    execute_supervisor_close_action as _execute_supervisor_close_action,
    execute_supervisor_reduce_action as _execute_supervisor_reduce_action,
    execute_supervisor_tighten_action as _execute_supervisor_tighten_action,
    normalize_supervisor_reduce_verdict as _normalize_supervisor_reduce_verdict,
    plan_supervisor_reduce_action as _plan_supervisor_reduce_action,
)
from backend.services.live_supervision_runtime import (
    LiveSupervisionRuntime,
    PositionPathMetricsRuntime,
    PositionSupervisorEvaluationRuntime,
    evaluate_position_supervisor_for_position as _runtime_evaluate_position,
    position_path_metrics_for_position as _runtime_position_path_metrics,
    run_position_supervision as _runtime_run_position_supervision,
)
from backend.services.live_factor_wiring import (
    merge_portfolio_configs as _merge_portfolio_configs,
)
from backend.services.low_frequency_factor_warmup import (
    build_low_frequency_factor_snapshots as _build_low_frequency_factor_snapshots,
)
from backend.services.live_factor_bootstrap import (
    FactorInitializationResult,
    FactorInitializationRuntime,
    FactorWarmupRuntime,
    initialize_factor_pipelines as _bootstrap_initialize_factor_pipelines,
    warmup_factor_pipeline as _bootstrap_warmup_factor_pipeline,
)
from backend.services.live_tick_pipeline import (
    build_factor_bar as _tick_build_factor_bar,
    build_factor_snapshot_summary as _tick_build_factor_snapshot_summary,
    build_factor_votes as _tick_build_factor_votes,
    guard_current_price_with_spot_quote as _tick_guard_current_price_with_spot_quote,
    build_signal_log_suffix as _tick_build_signal_log_suffix,
    build_close_ledger_payloads as _tick_build_close_ledger_payloads,
    build_effective_event_sizing_payload as _tick_build_effective_event_sizing_payload,
    build_amend_failed_ledger_payloads as _tick_build_amend_failed_ledger_payloads,
    build_trade_review_payload as _tick_build_trade_review_payload,
    build_market_order_block as _tick_build_market_order_block,
    build_open_order_preflight as _tick_build_open_order_preflight,
    build_open_ledger_payloads as _tick_build_open_ledger_payloads,
    build_order_failed_ledger_payloads as _tick_build_order_failed_ledger_payloads,
    build_skip_ledger_payload as _tick_build_skip_ledger_payload,
    collect_position_ids as _tick_collect_position_ids,
    normalize_live_positions_payload as _tick_normalize_live_positions_payload,
    resolve_closed_position_ids as _tick_resolve_closed_position_ids,
    resolve_open_protection_prices as _tick_resolve_open_protection_prices,
    resolve_order_fill_price as _tick_resolve_order_fill_price,
    resolve_order_position_id as _tick_resolve_order_position_id,
    select_close_total_pnl as _tick_select_close_total_pnl,
)
from backend.services.live_position_lifecycle import (
    active_pending_open_attach_ids as _lifecycle_active_pending_open_attach_ids,
    adjust_sl_plan_for_tp_only_protection as _lifecycle_adjust_sl_plan_for_tp_only_protection,
    apply_unrealized_pnl_fields as _lifecycle_apply_unrealized_pnl_fields,
    build_applied_entry_protection_plan_payload as _lifecycle_build_applied_entry_protection_plan_payload,
    build_bar_context_snapshot as _lifecycle_build_bar_context_snapshot,
    build_close_position_risk_context_payload as _lifecycle_build_close_position_risk_context_payload,
    build_decision_quality_context as _lifecycle_build_decision_quality_context,
    build_entry_cluster_context as _lifecycle_build_entry_cluster_context,
    build_filled_open_ledger_payloads as _lifecycle_build_filled_open_ledger_payloads,
    build_filled_open_recovery_payloads as _lifecycle_build_filled_open_recovery_payloads,
    build_entry_protection_plan_payload as _lifecycle_build_entry_protection_plan_payload,
    build_holding_summary_from_close_context as _lifecycle_build_holding_summary_from_close_context,
    build_holding_timeout_market_budget as _lifecycle_build_holding_timeout_market_budget,
    build_holding_timeout_result_trace_fields as _lifecycle_build_holding_timeout_result_trace_fields,
    build_holding_timeout_verdict_payload as _lifecycle_build_holding_timeout_verdict_payload,
    build_market_micro_context_payload as _lifecycle_build_market_micro_context_payload,
    build_open_learning_context_payload as _lifecycle_build_open_learning_context_payload,
    build_open_decision_replay_payload as _lifecycle_build_open_decision_replay_payload,
    validate_open_learning_context as _lifecycle_validate_open_learning_context,
    build_open_trade_risk_context_payload as _lifecycle_build_open_trade_risk_context_payload,
    build_position_path_metrics_update as _lifecycle_build_position_path_metrics_update,
    build_position_path_metrics_inputs as _lifecycle_build_position_path_metrics_inputs,
    build_replayed_close_payloads as _lifecycle_build_replayed_close_payloads,
    build_recovered_open_ledger_payloads as _lifecycle_build_recovered_open_ledger_payloads,
    build_protection_execution_plan as _lifecycle_build_protection_execution_plan,
    build_protection_execution_result_payloads as _lifecycle_build_protection_execution_result_payloads,
    market_open_seconds_between as _lifecycle_market_open_seconds_between,
    build_position_supervisor_context_inputs as _lifecycle_build_position_supervisor_context_inputs,
    build_position_supervisor_context_payload as _lifecycle_build_position_supervisor_context_payload,
    build_position_protection_cycle_result as _lifecycle_build_position_protection_cycle_result,
    build_protection_candidate_verdict_payload as _lifecycle_build_protection_candidate_verdict_payload,
    build_protection_candidate_risk_context_from_candidate as _lifecycle_build_protection_candidate_risk_context_from_candidate,
    build_protection_execution_trace_fields as _lifecycle_build_protection_execution_trace_fields,
    build_protection_position_event_details as _lifecycle_build_protection_position_event_details,
    build_protection_state_upsert_payload as _lifecycle_build_protection_state_upsert_payload,
    build_protection_superseded_trace_fields as _lifecycle_build_protection_superseded_trace_fields,
    build_recovery_closed_update_payload as _lifecycle_build_recovery_closed_update_payload,
    build_recovery_meta_update_payload as _lifecycle_build_recovery_meta_update_payload,
    build_risk_state_with_policy_verdict as _lifecycle_build_risk_state_with_policy_verdict,
    build_pending_supervisor_reentry_block_payload as _lifecycle_build_pending_supervisor_reentry_block_payload,
    build_supervisor_reentry_block_payload as _lifecycle_build_supervisor_reentry_block_payload,
    build_supervisor_decision_ledger_payload as _lifecycle_build_supervisor_decision_ledger_payload,
    build_supervisor_position_event_payload as _lifecycle_build_supervisor_position_event_payload,
    build_supervisor_state_upsert_payload as _lifecycle_build_supervisor_state_upsert_payload,
    build_supervisor_trace_ledger_payload as _lifecycle_build_supervisor_trace_ledger_payload,
    build_supervisor_close_context_inputs as _lifecycle_build_supervisor_close_context_inputs,
    build_supervisor_action_fingerprint as _lifecycle_build_supervisor_action_fingerprint,
    build_supervisor_risk_context_payload as _lifecycle_build_supervisor_risk_context_payload,
    build_supervisor_runtime_risk_evaluation_inputs as _lifecycle_build_supervisor_runtime_risk_evaluation_inputs,
    build_supervisor_tighten_execution_plan as _lifecycle_build_supervisor_tighten_execution_plan,
    build_supervisor_tighten_result_payloads as _lifecycle_build_supervisor_tighten_result_payloads,
    build_supervisor_tighten_sl_plan_inputs as _lifecycle_build_supervisor_tighten_sl_plan_inputs,
    build_supervisor_tighten_sl_plan as _lifecycle_build_supervisor_tighten_sl_plan,
    build_target_tp_extension_inputs as _lifecycle_build_target_tp_extension_inputs,
    build_trade_attribution_payload_from_composite as _lifecycle_build_trade_attribution_payload_from_composite,
    classify_close_source_from_evidence as _lifecycle_classify_close_source_from_evidence,
    classify_trading_session as _lifecycle_classify_trading_session,
    consume_close_reason as _lifecycle_consume_close_reason,
    consume_close_verdict as _lifecycle_consume_close_verdict,
    current_regime_hint_from_composite as _lifecycle_current_regime_hint_from_composite,
    estimate_close_pnl_from_state as _lifecycle_estimate_close_pnl_from_state,
    enrich_positions_with_lifecycle_metrics as _lifecycle_enrich_positions_with_lifecycle_metrics,
    entry_quality_gate_from_learning_policy as _lifecycle_entry_quality_gate_from_learning_policy,
    float_payload_value as _lifecycle_float_payload_value,
    filter_removed_live_position as _lifecycle_filter_removed_live_position,
    forget_pending_close_state as _lifecycle_forget_pending_close_state,
    holding_timeout_is_expired as _lifecycle_holding_timeout_is_expired,
    latest_close_evidence as _lifecycle_latest_close_evidence,
    normalize_protection_trace_row as _lifecycle_normalize_protection_trace_row,
    normalize_recovery_position_row as _lifecycle_normalize_recovery_position_row,
    normalize_supervisor_event_row as _lifecycle_normalize_supervisor_event_row,
    payload_get as _lifecycle_payload_get,
    normalize_position_snapshot as _lifecycle_normalize_position_snapshot,
    position_api_volume as _lifecycle_position_api_volume,
    position_direction_from_payload as _lifecycle_position_direction_from_payload,
    position_direction_sign as _lifecycle_position_direction_sign,
    position_id_value as _lifecycle_position_id_value,
    position_open_price as _lifecycle_position_open_price,
    position_open_timestamp as _lifecycle_position_open_timestamp,
    position_symbol_value as _lifecycle_position_symbol_value,
    position_unrealized_pnl as _lifecycle_position_unrealized_pnl,
    remember_pending_open_attach as _lifecycle_remember_pending_open_attach,
    remember_close_reason as _lifecycle_remember_close_reason,
    remember_close_verdict as _lifecycle_remember_close_verdict,
    recovery_active_position_ids as _lifecycle_recovery_active_position_ids,
    recovery_missing_position_ids as _lifecycle_recovery_missing_position_ids,
    recovery_replay_lookback_from as _lifecycle_recovery_replay_lookback_from,
    max_abs_entry_score_for_positions as _lifecycle_max_abs_entry_score_for_positions,
    restore_attribution_for_positions as _lifecycle_restore_attribution_for_positions,
    same_symbol_position as _lifecycle_same_symbol_position,
    side_name as _lifecycle_side_name,
    supervisor_recently_applied_from_meta as _lifecycle_supervisor_recently_applied_from_meta,
    supervisor_noop_fingerprint_seen as _lifecycle_supervisor_noop_fingerprint_seen,
    supervisor_reentry_block_view as _lifecycle_supervisor_reentry_block_view,
    supervisor_reentry_cooldown_seconds as _lifecycle_supervisor_reentry_cooldown_seconds,
    supervisor_reentry_key as _lifecycle_supervisor_reentry_key,
    supervisor_risk_action_for_action as _lifecycle_supervisor_risk_action_for_action,
    target_tp_is_extension as _lifecycle_target_tp_is_extension,
    temporal_context_for_trade as _lifecycle_temporal_context_for_trade,
    timeframe_seconds as _lifecycle_timeframe_seconds,
    tracked_total_api_volume as _lifecycle_tracked_total_api_volume,
    update_entry_protection_plan_payload as _lifecycle_update_entry_protection_plan_payload,
)
from backend.services.live_scheduler_jobs import (
    register_backend_readiness_refresh_job as _register_backend_readiness_refresh_job,
    register_external_sync_jobs as _register_external_sync_jobs,
    register_factor_selection_heartbeat_job as _register_factor_selection_heartbeat_job,
    start_scheduler_catch_up as _start_scheduler_catch_up,
)
from backend.services.position_metrics import normalize_path_state, update_position_path_metrics
from backend.services.position_supervisor import (
    evaluate_position_supervisor,
    is_hard_supervisor_action,
)
from backend.services.position_supervisor_governance import (
    POSITION_SUPERVISOR_SELECTION_PROJECTION_KEY,
    select_position_supervisor_binding,
)
from backend.services.position_supervisor_templates import (
    build_legacy_position_supervisor_binding,
    build_position_supervisor_binding,
    get_position_supervisor_template,
    verify_position_supervisor_binding,
)
from backend.services.stability import record_timed
_LEDGER: DecisionLedger | None = None
_TRADE_REVIEWER: TradeReviewer | None = None
_EXPERIENCE_BUILDER: ExperienceBuilder | None = None
_POLICY_SUGGESTER: PolicySuggester | None = None
_POSITION_QUALITY_ADVISOR: Any = None
_OPEN_QUALITY_ADVISOR: Any = None
_RISK_POLICY = RiskPolicyService.shared()
_ENTRY_CLUSTER_POLICY_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": {}}
_ENTRY_CLUSTER_POLICY_CACHE_LOCK = threading.Lock()
_EVENT_WINDOW_POLICY_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": {}}
_EVENT_WINDOW_POLICY_CACHE_LOCK = threading.Lock()
_ENTRY_QUALITY_POLICY_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": {}}
_ENTRY_QUALITY_POLICY_CACHE_LOCK = threading.Lock()





# ── Local SL/TP tracking (live loop only) ──────────────────────────
# audit 2026-06-10: 之前 SL/TP 完全靠本地 Python 监控 1 bar 延迟的
# check_sl_tp(), 实际 market_buy 时 bridge 协议不传 SL/TP 字段
# (MARKET 单限制). 改成: market_buy 成交后立即 amend_position_sltp 推
# server. _local_positions 跟踪每个 position_id 的 SL/TP, amend 成功后
# 覆盖, amend 失败时保留旧值(下次 tick 重试).
from dataclasses import asdict, dataclass, field

# ── Factor Takeover v4 管道 (Phase 3c) ──────────────────
# lazy-import: 在 _run_loop 中按需导入, 避免启动时循环依赖
# from alpha.streaming_factor_engine import StreamingFactorEngine
# from alpha.signal_normalizer import SignalNormalizer
# from alpha.portfolio_compositor import PortfolioCompositor
# from alpha.execution_gate import ExecutionGate

@dataclass
class _LocalSLTP:
    position_id: int
    sl: float = 0.0
    tp: float = 0.0
    updated_at: float = 0.0  # epoch seconds


@dataclass
class _OpenTradeCandidate:
    direction_name: str
    bridge_meta: dict[str, Any]
    digits: int
    sl_dist: float
    tp_dist: float
    sl_price: float
    tp_price: float
    base_volume: float
    volume: float
    event_multiplier: float
    event_sizing_context: dict[str, Any]
    sizing_trace: dict[str, Any]
    risk_verdict: Any
    market_session: dict[str, Any]
    order_block: dict[str, Any]
    nursery_reservation_id: str = ""
    open_decision_id: str = ""
    execution_intent_id: str = ""
    position_supervisor_binding: dict[str, Any] = field(default_factory=dict)

_local_positions: dict[int, _LocalSLTP] = {}
_local_positions_lock = threading.Lock()
_ENTRY_PROTECTION_PLAN_SCHEMA = "entry_protection_plan.v1"
_ENTRY_PROTECTION_REPAIR_SOURCE = "entry_protection_repair"
_ENTRY_PROTECTION_REPAIR_COOLDOWN_SECONDS = 20.0
_PENDING_OPEN_ATTACH_TTL_SECONDS = 300.0

# ── AttributionEngine 开仓/平仓跟踪 ──
# 记录上一 tick 的 position_id 集合, 用于检测平仓事件.
# 在 _process_tick_factor_pipeline 中每 tick 更新.
_prev_position_ids: set[int] = set()
# 用于 close detection: position_id → open_price
_pos_open_prices: dict[int, float] = {}
# 用于仓位上限/展示的策略口径 API volume (开仓后回查到的实际 API 量)
_pos_open_api_volume: dict[int, float] = {}
_pending_open_attach_until: dict[int, float] = {}
# ── 金字塔规则: position_id → 开仓时的 composite.score
# 用于判断新信号是否比已有持仓更强, 避免递减加仓
_pos_entry_scores: dict[int, float] = {}
_pos_entry_decisions: dict[int, str] = {}
_pending_close_reasons: dict[int, str] = {}
_pending_close_verdicts: dict[int, dict] = {}
_supervisor_reentry_blocks: dict[str, dict[str, Any]] = {}
_supervisor_reentry_blocks_lock = threading.Lock()

_RUNTIME_KV_LAST_SHUTDOWN = "live.loop.last_shutdown"
_RECOVERY_CONTEXT_PARTIAL = "partial"
_RECOVERY_CONTEXT_FULL = "full"
recovery_zero_confirmations: dict[str, int] = {}
_AUTO_RESUME_DELAY_SEC = 4.0


def _risk_kelly_volume(
    cfg, direction: int, current_price: float, sl_price: float,
    bridge_meta: dict, acct: dict,
) -> float:
    """根据 Kelly 分数计算 API 原生开仓量。"""
    return _risk_kelly_sizing(
        cfg, direction, current_price, sl_price, bridge_meta, acct,
    )["volume"]


def _risk_kelly_sizing(
    cfg, direction: int, current_price: float, sl_price: float,
    bridge_meta: dict, acct: dict,
) -> dict[str, Any]:
    """根据 Kelly 分数计算 API 原生开仓量，并返回可审计 trace。

    返回值使用 cTrader API volume unit；XAUUSD 常见最小开仓量约为 100 API units。
    """
    kelly_data = live_state_get("risk", {}, clone=True).get("kelly", {})
    return _sizing_risk_kelly_sizing(
        cfg=cfg,
        direction=direction,
        current_price=current_price,
        sl_price=sl_price,
        bridge_meta=bridge_meta,
        account=acct,
        kelly_data=kelly_data,
    )


def _ceil_api_volume_to_step(volume: float, bridge_meta: dict) -> float:
    return _sizing_ceil_api_volume_to_step(volume, bridge_meta)


def _round_api_volume_to_step(volume: float, bridge_meta: dict) -> float:
    return _sizing_round_api_volume_to_step(volume, bridge_meta)


def _floor_api_volume_to_step(volume: float, bridge_meta: dict) -> float:
    return _sizing_floor_api_volume_to_step(volume, bridge_meta)


def _apply_entry_event_sizing(
    *,
    base_volume: float,
    event_multiplier: float,
    bridge_meta: dict,
    sizing_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply event sizing without silently lifting reduced orders back to min volume."""
    return _sizing_apply_entry_event_sizing(
        base_volume=base_volume,
        event_multiplier=event_multiplier,
        bridge_meta=bridge_meta,
        sizing_trace=sizing_trace,
    )


def _should_full_close_untradeable_reduce(
    *,
    current_volume: float,
    raw_reduce_volume: float,
    reduce_volume: float,
    min_volume: float,
    verdict: dict[str, Any],
) -> tuple[bool, str]:
    """Escalate minimum-size reduce intents only when the risk evidence is strong."""
    return _sizing_should_full_close_untradeable_reduce(
        current_volume=current_volume,
        raw_reduce_volume=raw_reduce_volume,
        reduce_volume=reduce_volume,
        min_volume=min_volume,
        verdict=verdict,
    )


def _event_sizing_context(event_sizing: Any, bar_time: float) -> dict[str, Any]:
    if event_sizing is None:
        return {"enabled": False, "multiplier": 1.0}
    if hasattr(event_sizing, "get_context"):
        try:
            ctx = dict(event_sizing.get_context(bar_time) or {})
            try:
                stats = event_sizing.stats()
            except Exception:
                stats = None
            return _sizing_normalize_event_sizing_context(
                context=ctx,
                enabled=bool(getattr(event_sizing, "enabled", ctx.get("enabled", False))),
                stats=stats,
            )
        except Exception:
            pass
    try:
        multiplier = float(event_sizing.get_multiplier(bar_time))
    except Exception:
        multiplier = 1.0
    try:
        event_near, event_desc = event_sizing.is_event_near(bar_time)
    except Exception:
        event_near, event_desc = False, None
    try:
        stats = event_sizing.stats()
    except Exception:
        stats = {}
    return _sizing_build_event_sizing_fallback_context(
        enabled=bool(getattr(event_sizing, "enabled", False)),
        multiplier=multiplier,
        event_near=bool(event_near),
        event=event_desc,
        stats=stats,
    )


def _protection_prices_from_reference(
    direction: int,
    reference_price: float,
    sl_dist: float,
    tp_dist: float,
    digits: int = 2,
) -> tuple[float, float]:
    """Compute SL/TP from the freshest executable reference price."""
    return _sizing_protection_prices_from_reference(
        direction=direction,
        reference_price=reference_price,
        sl_dist=sl_dist,
        tp_dist=tp_dist,
        digits=digits,
    )


def _position_api_volume(pos: Any) -> float:
    """Extract the canonical API volume from a position payload.

    The live stack should use the broker-returned volume field directly and
    avoid falling back to legacy unit aliases when doing risk and sizing
    math.
    """
    return _lifecycle_position_api_volume(pos)


def _estimate_close_pnl_from_cached_state(position_id: int, current_price: float) -> float:
    recovery_row = live_close_settlement.load_recovery_position_row(int(position_id))
    return _lifecycle_estimate_close_pnl_from_state(
        position_id=position_id,
        current_price=current_price,
        recovery_row=recovery_row,
        open_prices=_pos_open_prices,
        open_api_volumes=_pos_open_api_volume,
    )


_position_direction_sign = _lifecycle_position_direction_sign
_apply_unrealized_pnl_fields = _lifecycle_apply_unrealized_pnl_fields


def _tracked_total_api_volume(positions: list[Any]) -> float:
    return _lifecycle_tracked_total_api_volume(
        positions,
        open_api_volumes=_pos_open_api_volume,
    )


def _max_abs_entry_score_for_positions(positions: list[Any]) -> float:
    return _lifecycle_max_abs_entry_score_for_positions(
        positions,
        entry_scores=_pos_entry_scores,
    )


_payload_get = _lifecycle_payload_get
_position_symbol_value = _lifecycle_position_symbol_value
_direction_from_position_payload = _lifecycle_position_direction_from_payload
_supervisor_reentry_key = _lifecycle_supervisor_reentry_key


def _supervisor_reentry_cooldown_seconds(cfg) -> float:
    return _lifecycle_supervisor_reentry_cooldown_seconds(
        cooldown_bars=getattr(cfg, "risk_supervisor_reentry_cooldown_bars", 3),
        timeframe=str(getattr(cfg, "timeframe", "M5") or "M5"),
        timeframe_seconds=_timeframe_seconds,
    )


def _reentry_guard_runtime() -> ReentryGuardRuntime:
    from backend.core.db import get_state_pg_conn

    return ReentryGuardRuntime(
        blocks=_supervisor_reentry_blocks,
        blocks_lock=_supervisor_reentry_blocks_lock,
        reentry_key=_supervisor_reentry_key,
        build_block_payload=_lifecycle_build_supervisor_reentry_block_payload,
        block_view=_lifecycle_supervisor_reentry_block_view,
        direction_from_position=_direction_from_position_payload,
        position_symbol=_position_symbol_value,
        payload_get=_payload_get,
        cooldown_seconds=_supervisor_reentry_cooldown_seconds,
        build_pending_payload=_lifecycle_build_pending_supervisor_reentry_block_payload,
        state_connection_factory=get_state_pg_conn,
        warning=logger.warning,
        now=time.time,
    )


def _remember_supervisor_reentry_block(
    *,
    position: Any,
    action: str,
    reason: str,
    cfg,
    current_price: float = 0.0,
    tick: int = 0,
) -> None:
    _guard_remember_reentry_block(
        position=position,
        action=action,
        reason=reason,
        cfg=cfg,
        runtime=_reentry_guard_runtime(),
        current_price=current_price,
        tick=tick,
    )


def _active_supervisor_reentry_block(
    *, symbol: str, direction: int,
) -> dict[str, Any] | None:
    return _guard_active_reentry_block(
        symbol=symbol,
        direction=direction,
        runtime=_reentry_guard_runtime(),
    )


def _recent_review_reentry_block(
    *, symbol: str, direction: int, now_ts: float | None = None,
) -> dict[str, Any] | None:
    return _guard_recent_review_reentry_block(
        symbol=symbol,
        direction=direction,
        runtime=_reentry_guard_runtime(),
        now_ts=now_ts,
    )


def _pending_supervisor_reentry_block_from_positions(
    positions: list[Any],
    *,
    symbol: str,
    direction: int,
    cfg,
) -> dict[str, Any] | None:
    return _guard_pending_reentry_block(
        positions,
        symbol=symbol,
        direction=direction,
        cfg=cfg,
        runtime=_reentry_guard_runtime(),
    )


def _build_open_trade_risk_context(
    *,
    cfg,
    bridge,
    acct: dict,
    positions: list[Any],
    requested_api_volume: float,
    signal_score: float,
    symbol: str = "XAUUSD",
    direction: int = 0,
    current_price: float = 0.0,
    atr_price: float = 0.0,
    event_sizing_context: dict[str, Any] | None = None,
    event_filter_context: dict[str, Any] | None = None,
    decision_quality_context: dict[str, Any] | None = None,
    decision_ts: float | None = None,
) -> dict:
    runtime = OpenRiskContextRuntime(
        state_get=live_state_get,
        collect_runtime_health=partial(
            _loop_collect_open_risk_runtime_health,
            decision_freshness_provider=partial(live_state_get, "decision_bar_freshness", {}, clone=True),
        ),
        temporal_context_for_trade=_temporal_context_for_trade,
        active_supervisor_reentry_block=_active_supervisor_reentry_block,
        recent_review_reentry_block=_recent_review_reentry_block,
        pending_supervisor_reentry_block=(
            _pending_supervisor_reentry_block_from_positions
        ),
        build_entry_cluster_context=_build_entry_cluster_context,
        active_entry_quality_policy=_active_entry_quality_learning_policy,
        entry_quality_gate=_entry_quality_gate_from_learning_policy,
        build_payload=_lifecycle_build_open_trade_risk_context_payload,
        tracked_total_api_volume=_tracked_total_api_volume,
        active_event_window_policy=_active_event_window_learning_policy,
        active_entry_cluster_policy=_active_entry_cluster_learning_policy,
        max_abs_entry_score=_max_abs_entry_score_for_positions,
        now=time.time,
    )
    return _runtime_build_open_risk_context(
        cfg=cfg,
        bridge=bridge,
        account=acct,
        positions=positions,
        requested_api_volume=requested_api_volume,
        signal_score=signal_score,
        runtime=runtime,
        symbol=symbol,
        direction=direction,
        current_price=current_price,
        atr_price=atr_price,
        event_sizing_context=event_sizing_context,
        event_filter_context=event_filter_context,
        decision_quality_context=decision_quality_context, decision_ts=decision_ts,
        loss_streak_ladder_facts=loss_streak_ladder_facts,
    )


def _event_filter_context_for_risk_policy(
    *,
    cfg,
    direction: int,
    bar: dict[str, Any],
    factor_values: dict[str, Any],
) -> dict[str, Any]:
    gate_config = _loop_execution_gate_config(cfg)
    # NOTE: risk/strategy *_gvz_gate keys are retired (GVZ execution gate
    # deleted); only NFP skip is evaluated here. Do not re-add gvz branches.
    if not (
        bool(gate_config.get("risk_enable_nfp_skip", False))
        or bool(gate_config.get("strategy_enable_nfp_skip", False))
    ):
        return {}
    try:
        from alpha.execution_gate import evaluate_event_risk_filter

        verdict = evaluate_event_risk_filter(gate_config, direction, bar, factor_values)
    except Exception as exc:
        return {
            "schema_version": "event_risk_filter.v1",
            "active": True,
            "blocked": False,
            "source": "execution_gate_event_filter",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "schema_version": "event_risk_filter.v1",
        "active": True,
        "blocked": not bool(getattr(verdict, "passed", False)),
        "reason": str(getattr(verdict, "reason", "") or ""),
        "source": "execution_gate_event_filter",
        "authority": "RiskPolicyService",
    }


def _live_learning_policy_runtime(cache: dict, cache_lock) -> LiveLearningPolicyRuntime:
    from backend.core.db import get_state_pg_conn

    return LiveLearningPolicyRuntime(
        connection_factory=get_state_pg_conn,
        load_controls=load_live_policy_controls,
        cache=cache,
        cache_lock=cache_lock,
        warning=logger.warning,
        now=time.time,
    )


def _active_entry_cluster_learning_policy(
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    return load_active_learning_policy(
        "entry_cluster",
        runtime=_live_learning_policy_runtime(
            _ENTRY_CLUSTER_POLICY_CACHE,
            _ENTRY_CLUSTER_POLICY_CACHE_LOCK,
        ),
        now_ts=now_ts,
    )


def _active_entry_quality_learning_policy(
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    return load_active_learning_policy(
        "entry_quality",
        runtime=_live_learning_policy_runtime(
            _ENTRY_QUALITY_POLICY_CACHE,
            _ENTRY_QUALITY_POLICY_CACHE_LOCK,
        ),
        now_ts=now_ts,
    )


def _active_event_window_learning_policy(
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    return load_active_learning_policy(
        "event_window",
        runtime=_live_learning_policy_runtime(
            _EVENT_WINDOW_POLICY_CACHE,
            _EVENT_WINDOW_POLICY_CACHE_LOCK,
        ),
        now_ts=now_ts,
    )


def _risk_state_with_verdict(verdict) -> dict:
    state = live_state_get("risk", {}, clone=True) or {}
    return _lifecycle_build_risk_state_with_policy_verdict(state, verdict)


_position_open_price = _lifecycle_position_open_price
_position_open_timestamp = _lifecycle_position_open_timestamp
_position_id_value = _lifecycle_position_id_value
_same_symbol_position = _lifecycle_same_symbol_position


def _build_entry_cluster_context(
    *,
    positions_before: list[Any] | None,
    direction: int,
    symbol: str,
    now_ts: float,
    new_position_id: int = 0,
    new_api_volume: float = 0.0,
) -> dict[str, Any]:
    return _lifecycle_build_entry_cluster_context(
        positions_before=positions_before,
        direction=direction,
        symbol=symbol,
        now_ts=now_ts,
        new_position_id=new_position_id,
        new_api_volume=new_api_volume,
    )


def _market_micro_context_snapshot(
    *,
    bridge: Any,
    current_price: float,
    fill_price: float = 0.0,
    direction: int = 0,
    now_ts: float | None = None,
) -> dict[str, Any]:
    now_ts = float(now_ts or time.time())
    quote = live_state_get("spot_quote", None, clone=True) or {}
    if bridge is not None and hasattr(bridge, "get_spot_quote"):
        try:
            fresh_quote = bridge.get_spot_quote() or {}
            if fresh_quote:
                quote = fresh_quote
                live_state_update(spot_quote=fresh_quote)
        except Exception:
            pass
    return _lifecycle_build_market_micro_context_payload(
        quote=quote,
        current_price=current_price,
        fill_price=fill_price,
        direction=direction,
        quote_age_seconds=_quote_age_seconds(quote, now_ts=now_ts),
        quote_fresh=_quote_is_fresh(quote, now_ts=now_ts),
    )


_bar_context_snapshot = _lifecycle_build_bar_context_snapshot
_decision_quality_context = _lifecycle_build_decision_quality_context


def _evaluate_open_quality_model_veto(
    *,
    cfg: Any,
    bridge: Any,
    bar: dict[str, Any],
    composite: Any,
    positions: list[Any],
    current_price: float,
    event_context: dict[str, Any],
    rule_decision: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate the promoted PIT-v2 open model as a veto-only control."""
    global _OPEN_QUALITY_ADVISOR
    from backend.services.model_influence import shared_model_influence_service

    influence = shared_model_influence_service()
    policy = influence.active_policy("open_quality_lightgbm", cfg)
    if _OPEN_QUALITY_ADVISOR is None:
        from research.open_quality_lightgbm import OpenQualityLightGBMService

        _OPEN_QUALITY_ADVISOR = OpenQualityLightGBMService()
    direction = int(getattr(composite, "direction", 0) or 0)
    decision_quality = _decision_quality_context(composite)
    entry_cluster = _build_entry_cluster_context(
        positions_before=positions,
        direction=direction,
        symbol="XAUUSD+",
        now_ts=float(bar.get("time") or time.time()),
    )
    action = {
        "score": float(getattr(composite, "score", 0.0) or 0.0),
        "direction": direction,
        "entry_cluster": entry_cluster,
        **{
            key: decision_quality.get(key)
            for key in (
                "tactical_score", "macro_score", "alpha_score", "n_active_factors",
                "n_active_alpha_factors", "n_abstain_factors",
            )
        },
    }
    context = {
        "action_score": action["score"],
        "action": action,
        "entry_cluster": entry_cluster,
        "portfolio_exposure": entry_cluster,
        "market_micro_context": _market_micro_context_snapshot(
            bridge=bridge,
            current_price=current_price,
            direction=direction,
        ),
        "bar_context": _bar_context_snapshot(bar),
        "event_context": event_context,
        "decision_quality_context": decision_quality,
    }
    subject_id = f"XAUUSD+:{int(float(bar.get('time') or time.time()))}:{direction}"
    if not policy:
        # Live shadow observation: score every risk-passed open attempt with the
        # latest registered artifact and audit it, so predictions can later be
        # joined to matured open_target_v2 outcomes by decision bar + direction.
        # Pure observation: failures must degrade to the old inactive no-op and
        # never block the open path; veto power still requires an active policy.
        try:
            shadow = _OPEN_QUALITY_ADVISOR.score_open_context_shadow(
                context,
                subject_id=subject_id,
                payload_extra={
                    "subject_id": subject_id,
                    "bar_time": float(bar.get("time") or 0.0),
                    "direction": direction,
                },
            )
        except Exception as exc:
            return {"passed": True, "reason": f"model_open_live_shadow_error:{type(exc).__name__}"}
        if not shadow.get("ok"):
            return {
                "passed": True,
                "reason": f"model_open_influence_inactive:{shadow.get('error') or 'shadow_unavailable'}",
            }
        return {
            "passed": True,
            "reason": "model_open_influence_live_shadow",
            "quality_score": shadow.get("quality_score"),
            "inference_id": (shadow.get("inference") or {}).get("inference_id"),
        }
    score = _OPEN_QUALITY_ADVISOR.score_open_context(
        context, artifact_path=str(policy.get("artifact_path") or "")
    )
    return influence.evaluate_open_veto(
        score=score,
        subject_id=subject_id,
        cfg=cfg,
        rule_decision=rule_decision,
    )


def _entry_quality_gate_from_learning_policy(
    *,
    policy: dict[str, Any],
    decision_quality: dict[str, Any],
    signal_score: float,
) -> dict[str, Any]:
    return _lifecycle_entry_quality_gate_from_learning_policy(
        policy=policy,
        decision_quality=decision_quality,
        signal_score=signal_score,
    )


def _open_learning_context_payload(
    *,
    bridge: Any,
    bar: dict[str, Any],
    positions_before: list[Any] | None,
    composite: Any,
    symbol: str,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    current_price: float,
    fill_price: float,
    sl_price: float,
    tp_price: float,
    sl_dist: float,
    tp_dist: float,
    event_sizing_context: dict[str, Any] | None,
    sizing_trace: dict[str, Any] | None = None,
    risk_verdict: Any = None,
    market_session: dict[str, Any] | None = None,
    position_supervisor_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = OpenLearningContextRuntime(
        build_entry_cluster_context=_build_entry_cluster_context,
        market_micro_context_snapshot=_market_micro_context_snapshot,
        state_get=live_state_get,
        build_entry_timing_context=build_entry_timing_context,
        build_payload=_lifecycle_build_open_learning_context_payload,
        tracked_total_api_volume=_tracked_total_api_volume,
        now=time.time,
    )
    payload = _runtime_build_open_learning_context(
        bridge=bridge,
        bar=bar,
        positions_before=positions_before,
        composite=composite,
        symbol=symbol,
        position_id=pid,
        actual_api_volume=actual_api_volume,
        requested_volume=requested_volume,
        base_requested_volume=base_requested_volume,
        current_price=current_price,
        fill_price=fill_price,
        stop_loss_price=sl_price,
        take_profit_price=tp_price,
        stop_loss_distance=sl_dist,
        take_profit_distance=tp_dist,
        event_sizing_context=event_sizing_context,
        runtime=runtime,
        sizing_trace=sizing_trace,
        risk_verdict=risk_verdict,
        market_session=market_session,
        position_supervisor_binding=position_supervisor_binding,
    )
    return payload


_classify_trading_session = _lifecycle_classify_trading_session
_timeframe_seconds = _lifecycle_timeframe_seconds


def _temporal_context_for_trade(
    *,
    decision_ts: float,
    timeframe: str,
    evaluated_at_ts: float | None = None,
    session_last_trade_ts: float = 0.0,
    loop_started_at: float = 0.0,
) -> dict:
    evaluated_at = float(evaluated_at_ts or time.time())
    return _lifecycle_temporal_context_for_trade(
        decision_ts=decision_ts,
        timeframe=timeframe,
        evaluated_at_ts=evaluated_at,
        session_last_trade_ts=session_last_trade_ts,
        loop_started_at=loop_started_at,
    )


def _risk_reduction_runtime() -> RiskReductionRuntime:
    from config.runtime_config import shared as _runtime_config

    return RiskReductionRuntime(
        append_safety_outbox=append_safety_outbox,
        logger_error=logger.error,
        logger_warning=logger.warning,
        now=time.time,
        config_factory=_runtime_config,
        position_open_timestamp=_position_open_timestamp,
        lookup_open_decision_context=live_close_settlement.lookup_open_decision_context,
        temporal_context_for_trade=_temporal_context_for_trade,
        build_close_context_payload=(
            _lifecycle_build_close_position_risk_context_payload
        ),
        load_recovery_position_row=live_close_settlement.load_recovery_position_row,
        lookup_entry_decision_id=live_close_settlement.lookup_entry_decision_id,
        risk_policy=_RISK_POLICY,
    )


def _broker_schedule_from_bridge(bridge: Any) -> dict[str, Any] | None:
    """Return the latest cTrader symbol schedule without making a broker call."""

    meta = getattr(bridge, "_symbol_meta", None) if bridge is not None else None
    schedule = meta.get("broker_schedule") if isinstance(meta, dict) else None
    return dict(schedule) if isinstance(schedule, dict) else None


def _build_close_position_risk_context(
    *,
    position_id: int,
    close_reason: str,
    mode: str = "live",
    broker: str = "",
    symbol: str = "",
    position: Any | None = None,
    cfg=None,
    decision_ts: float | None = None,
    broker_schedule: dict[str, Any] | None = None,
) -> dict:
    return _risk_reduction_build_close_context(
        position_id=position_id,
        close_reason=close_reason,
        mode=mode,
        broker=broker,
        symbol=symbol,
        position=position,
        cfg=cfg,
        decision_ts=decision_ts,
        broker_schedule=broker_schedule,
        runtime=_risk_reduction_runtime(),
    )




def _load_recovery_row_for_risk_reduction(
    position_id: int,
    *,
    operation: str,
) -> dict[str, Any]:
    return _risk_reduction_load_recovery_row(
        position_id,
        operation=operation,
        runtime=_risk_reduction_runtime(),
    )


def _lookup_entry_context_for_risk_reduction(
    position_id: int,
    *,
    operation: str,
) -> dict[str, Any]:
    return _risk_reduction_lookup_entry_context(
        position_id,
        operation=operation,
        runtime=_risk_reduction_runtime(),
    )


def _lookup_entry_decision_for_risk_reduction(
    position_id: int,
    *,
    operation: str,
) -> str:
    return _risk_reduction_lookup_entry_decision(
        position_id,
        operation=operation,
        runtime=_risk_reduction_runtime(),
    )


def _evaluate_risk_reduction_policy(
    action: str,
    context: dict[str, Any],
) -> RiskVerdict:
    return _risk_reduction_evaluate_policy(
        action,
        context,
        runtime=_risk_reduction_runtime(),
    )


def _holding_summary_for_position(
    position: Any,
    *,
    cfg=None,
    now_ts: float | None = None,
    broker_schedule: dict[str, Any] | None = None,
) -> dict:
    try:
        pid = int(
            (position.get("position_id") if isinstance(position, dict) else getattr(position, "position_id", None))
            or (position.get("ticket") if isinstance(position, dict) else getattr(position, "ticket", None))
            or 0
        )
    except Exception:
        pid = 0
    if pid <= 0:
        return {}
    close_context = _build_close_position_risk_context(
        position_id=pid,
        close_reason="position_snapshot",
        mode="snapshot",
        symbol=str(position.get("symbol") if isinstance(position, dict) else getattr(position, "symbol", "") or ""),
        position=position,
        cfg=cfg,
        decision_ts=now_ts,
        broker_schedule=broker_schedule,
    )
    return _lifecycle_build_holding_summary_from_close_context(close_context)


def _position_unrealized_pnl(position: Any) -> float:
    return _lifecycle_position_unrealized_pnl(position)








def _replace_recovery_position_meta(
    position_id: int,
    meta: Mapping[str, Any],
    *,
    expected_meta: Mapping[str, Any] | None = None,
) -> bool:
    return live_close_settlement.recovery_position_store().replace_meta(
        position_id,
        meta,
        expected_meta=expected_meta,
    )


def _entry_protection_plan_payload(
    *,
    position_id: int,
    direction: int,
    entry_price: float,
    target_stop_loss: float,
    target_take_profit: float,
    requested_volume: float,
    actual_api_volume: float,
    tick: int,
    status: str = "pending",
    source: str = "factor_v4_open",
    error: str = "",
    supervisor_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    anchor = _runtime_config_anchor()
    now = time.time()
    return _lifecycle_build_entry_protection_plan_payload(
        schema_version=_ENTRY_PROTECTION_PLAN_SCHEMA,
        position_id=position_id,
        direction=direction,
        entry_price=entry_price,
        target_stop_loss=target_stop_loss,
        target_take_profit=target_take_profit,
        requested_volume=requested_volume,
        actual_api_volume=actual_api_volume,
        tick=tick,
        created_at=now,
        config_version=int(anchor.get("config_version") or 0),
        config_hash=str(anchor.get("config_hash") or ""),
        status=status,
        source=source,
        error=error,
        supervisor_binding=supervisor_binding,
    )


def _position_supervisor_selection_key(
    *,
    cfg: Any,
    composite: Any,
) -> dict[str, str]:
    quality = _decision_quality_context(composite)
    current_regime = str(
        getattr(composite, "regime_id", "")
        or quality.get("regime_id")
        or _current_regime_hint()
        or "unknown"
    )
    symbol = str(
        getattr(composite, "symbol", "")
        or live_state_get("symbol", "")
        or "XAUUSD+"
    )
    timeframe = str(getattr(cfg, "timeframe", "") or "M5")
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "entry_regime": current_regime,
        "current_regime": current_regime,
    }


def _static_position_supervisor_binding(
    *,
    cfg: Any,
    composite: Any,
    reason: str,
) -> dict[str, Any]:
    template_id = str(
        getattr(cfg, "position_supervisor_template_id", "")
        or "position_supervisor:default.v1"
    )
    template = get_position_supervisor_template(template_id)
    source = (
        "static_baseline"
        if template_id == "position_supervisor:default.v1"
        else "governed_global_baseline"
    )
    return build_position_supervisor_binding(
        template,
        binding_source=source,
        selection_status="bound",
        selection_key=_position_supervisor_selection_key(
            cfg=cfg,
            composite=composite,
        ),
        evidence_refs={"reason": str(reason or "static_baseline")},
    )


def _select_position_supervisor_binding_for_open(
    *,
    cfg: Any,
    composite: Any,
) -> dict[str, Any]:
    """Select once before an open, without creating a broker-side mutation."""

    mode = str(
        getattr(cfg, "position_supervisor_auto_selection_mode", "off") or "off"
    ).strip().lower()
    static_binding = _static_position_supervisor_binding(
        cfg=cfg,
        composite=composite,
        reason=(
            "selection_disabled"
            if mode == "off"
            else "selection_mode_not_executable"
        ),
    )
    if mode not in {"shadow", "demo_execute"}:
        static_binding["evidence_refs"]["selection_status"] = "no_change"
        static_binding["evidence_refs"]["selection_reason"] = (
            "selection_disabled"
        )
        return static_binding
    if mode == "demo_execute" and not bounded_demo_mode_active(cfg):
        static_binding["evidence_refs"]["selection_reason"] = "bounded_demo_required"
        return static_binding
    try:
        projection = live_close_settlement.runtime_kv_get(POSITION_SUPERVISOR_SELECTION_PROJECTION_KEY, {})
        selection = select_position_supervisor_binding(
            projection if isinstance(projection, dict) else {},
            **_position_supervisor_selection_key(cfg=cfg, composite=composite),
            current_binding=None,
            max_age_seconds=float(
                getattr(cfg, "position_supervisor_selection_max_age_seconds", 900.0)
                or 900.0
            ),
        )
    except Exception as exc:
        static_binding["evidence_refs"]["selection_reason"] = (
            f"selection_projection_unavailable:{type(exc).__name__}"
        )
        return static_binding
    selected = dict(selection.get("binding") or {})
    if mode == "shadow":
        static_binding["evidence_refs"]["shadow_selection"] = {
            "reason": str(selection.get("reason") or ""),
            "ok": bool(selection.get("ok")),
            "selection_event_id": str(selection.get("selection_event_id") or ""),
            "template_id": str(selected.get("template_id") or ""),
            "template_hash": str(selected.get("template_hash") or ""),
        }
        return static_binding
    if (
        not selection.get("ok")
        or not selected
        or str(selection.get("reason") or "")
        != "selected_highest_positive_effect"
    ):
        static_binding["evidence_refs"]["selection_reason"] = str(
            selection.get("reason") or "selection_not_available"
        )
        return static_binding
    selected_check = verify_position_supervisor_binding(selected)
    if not selected_check.get("valid"):
        static_binding["evidence_refs"]["selection_reason"] = (
            "selected_binding_invalid"
        )
        return static_binding
    selected["evidence_refs"] = {
        **dict(selected.get("evidence_refs") or {}),
        "selection_event_id": str(selection.get("selection_event_id") or ""),
    }
    return selected


def _position_supervisor_policy_for_position(
    *,
    cfg: Any,
    supervisor_state: dict[str, Any],
    position: dict[str, Any],
    position_metrics: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve one position's snapshot and its fail-closed policy state."""

    plan = dict(supervisor_state.get("entry_protection_plan") or {})
    raw_binding = plan.get("supervisor_binding")
    symbol = str(position.get("symbol") or "XAUUSD+")
    selection_key = {
        "symbol": symbol,
        "timeframe": str(getattr(cfg, "timeframe", "") or "M5"),
        "entry_regime": str(
            position_metrics.get("entry_regime")
            or supervisor_state.get("entry_regime")
            or ""
        ),
        "current_regime": str(
            position_metrics.get("current_regime")
            or supervisor_state.get("current_regime")
            or ""
        ),
    }
    if isinstance(raw_binding, dict):
        checked = verify_position_supervisor_binding(raw_binding)
        if checked.get("valid"):
            binding = dict(checked.get("binding") or raw_binding)
            template = dict(checked.get("template") or {})
            return template, {
                "schema_version": "position_supervisor_policy.v1",
                "binding_state": "bound",
                "binding_reason": "binding_verified",
                "binding_source": str(binding.get("binding_source") or ""),
                "template_id": str(binding.get("template_id") or ""),
                "template_version": str(binding.get("template_version") or ""),
                "template_hash": str(binding.get("template_hash") or ""),
                "selection_event_id": str(
                    dict(binding.get("evidence_refs") or {}).get(
                        "selection_event_id", ""
                    )
                ),
                "posterior_fingerprint": str(
                    binding.get("posterior_fingerprint") or ""
                ),
                "selection_key": dict(binding.get("selection_key") or selection_key),
                "binding": binding,
            }
        if checked.get("state") == "legacy":
            template_id = str(
                raw_binding.get("template_id")
                or getattr(cfg, "position_supervisor_template_id", "")
                or "position_supervisor:default.v1"
            )
            return get_position_supervisor_template(template_id), {
                "schema_version": "position_supervisor_policy.v1",
                "binding_state": "legacy",
                "binding_reason": "legacy_global_fallback",
                "binding_source": "legacy_global_fallback",
                "template_id": template_id,
                "template_version": "",
                "template_hash": "",
                "selection_event_id": "",
                "posterior_fingerprint": "",
                "selection_key": selection_key,
                "binding": raw_binding,
            }
        # The template is deliberately not taken from a corrupted snapshot.
        # Hard-risk evaluation still uses the safe built-in baseline, while
        # the runtime evaluator blocks discretionary actions below.
        return get_position_supervisor_template("position_supervisor:default.v1"), {
            "schema_version": "position_supervisor_policy.v1",
            "binding_state": "invalid",
            "binding_reason": str(
                checked.get("reason") or "binding_unverified"
            ),
            "binding_source": str(raw_binding.get("binding_source") or ""),
            "template_id": str(raw_binding.get("template_id") or ""),
            "template_version": str(raw_binding.get("template_version") or ""),
            "template_hash": str(raw_binding.get("template_hash") or ""),
            "selection_event_id": "",
            "posterior_fingerprint": "",
            "selection_key": selection_key,
            "binding": raw_binding,
        }
    template_id = str(
        getattr(cfg, "position_supervisor_template_id", "")
        or "position_supervisor:default.v1"
    )
    legacy = build_legacy_position_supervisor_binding(
        template_id,
        selection_key=selection_key,
    )
    return get_position_supervisor_template(template_id), {
        "schema_version": "position_supervisor_policy.v1",
        "binding_state": "legacy",
        "binding_reason": "legacy_global_fallback",
        "binding_source": "legacy_global_fallback",
        "template_id": template_id,
        "template_version": "",
        "template_hash": "",
        "selection_event_id": "",
        "posterior_fingerprint": "",
        "selection_key": selection_key,
        "binding": legacy,
    }


def _position_supervisor_switch_state_payload(
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current = dict(state or {})
    try:
        switch_count = max(0, int(current.get("switch_count") or 0))
    except (TypeError, ValueError):
        switch_count = 0
    try:
        stable_bars = max(0, int(current.get("stable_bars") or 0))
    except (TypeError, ValueError):
        stable_bars = 0
    try:
        last_switch_bar_number = int(current.get("last_switch_bar_number"))
    except (TypeError, ValueError):
        last_switch_bar_number = -1
    return {
        "schema_version": "position_supervisor_switch.v1",
        "candidate_regime": str(current.get("candidate_regime") or ""),
        "candidate_bar_key": str(current.get("candidate_bar_key") or ""),
        "last_seen_bar_key": str(current.get("last_seen_bar_key") or ""),
        "stable_bars": stable_bars,
        "switch_count": switch_count,
        "last_switch_bar_number": last_switch_bar_number,
        "last_switch_bar_key": str(current.get("last_switch_bar_key") or ""),
        "last_switch_ts": float(current.get("last_switch_ts") or 0.0),
        "last_selection_bar_key": str(current.get("last_selection_bar_key") or ""),
        "last_selection_reason": str(current.get("last_selection_reason") or ""),
    }


def _position_supervisor_switch_block_reason(
    *,
    cfg: Any,
    context: Mapping[str, Any],
    verdict: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> str:
    """Return a conservative reason when a soft policy switch is unsafe."""

    selection_mode = str(
        getattr(cfg, "position_supervisor_auto_selection_mode", "off") or "off"
    ).strip().lower()
    if selection_mode not in {"shadow", "demo_execute"}:
        return "selection_mode_not_demo_execute"
    if selection_mode == "demo_execute" and not bounded_demo_mode_active(cfg):
        return "bounded_demo_required"
    if str(policy.get("binding_state") or "").strip().lower() != "bound":
        return "position_binding_not_verified"
    binding = policy.get("binding")
    if not isinstance(binding, Mapping) or not verify_position_supervisor_binding(binding).get("valid"):
        return "position_binding_not_verified"
    evidence = dict(verdict.get("evidence") or {})
    action = str(
        verdict.get("requested_action")
        or verdict.get("action")
        or "hold"
    ).strip().lower()
    if action != "hold":
        return "active_supervisor_action_has_priority"
    if is_hard_supervisor_action(
        action=action,
        summary_reason=str(verdict.get("summary_reason") or ""),
        evidence=evidence,
    ):
        return "hard_risk_has_priority"
    if bool(evidence.get("hard_risk_active")):
        return "hard_risk_has_priority"
    blocked_tags = {
        "hard_risk_active",
        "holding_timeout_exceeded",
        "near_stop_loss",
        "thesis_broken",
        "regime_shift_detected",
    }
    if blocked_tags.intersection(str(item) for item in evidence.get("trigger_tags") or []):
        return "risk_reduction_has_priority"
    position = dict(context.get("position") or {})
    required_states = {
        "price": str(
            position.get("current_price_state") or position.get("price_state") or ""
        ).strip().lower(),
        "pnl": str(
            position.get("pnl_state") or position.get("unrealized_pnl_state") or ""
        ).strip().lower(),
        "path_metrics": str(
            position.get("position_path_metrics_state") or ""
        ).strip().lower(),
    }
    if any(state != "known" for state in required_states.values()):
        return "position_management_facts_unknown"
    market = dict(context.get("market") or {})
    if str(market.get("market_context_state") or "unknown").strip().lower() != "known":
        return "market_context_unknown"
    if not bool(evidence.get("market_dimensions_known")):
        return "market_dimensions_unknown"
    temporal = dict(context.get("temporal_context") or {})
    if not str(
        evidence.get("closed_bar_key")
        or temporal.get("closed_bar_key")
        or temporal.get("closed_bar_ts")
        or ""
    ):
        return "closed_bar_unknown"
    execution_recovery = live_state_get("execution_recovery", {}, clone=True) or {}
    if not bool(execution_recovery.get("ready")):
        return "execution_recovery_not_ready"
    try:
        unresolved_count = int(execution_recovery.get("unresolved_count"))
    except (TypeError, ValueError):
        return "execution_recovery_unknown"
    if unresolved_count != 0:
        return "unresolved_execution_intent"
    safety = live_state_get("safety_plane", {}, clone=True) or {}
    if str(safety.get("reconciliation_state") or "unknown").strip().lower() != "fresh":
        return "positions_reconciliation_not_fresh"
    if list(safety.get("blockers") or []):
        return "safety_blocker_present"
    if bool(live_state_get("safety_cycle_active", False)):
        return "safety_cycle_in_progress"
    return ""


def _maybe_switch_position_supervisor_binding(
    *,
    position: dict[str, Any],
    cfg: Any,
    context: Mapping[str, Any],
    verdict: dict[str, Any],
    now_ts: float,
) -> dict[str, Any]:
    """Switch one bound position only after a stable, safe regime boundary."""

    policy = context.get("position_supervisor_policy")
    if not isinstance(policy, Mapping):
        return verdict
    block_reason = _position_supervisor_switch_block_reason(
        cfg=cfg,
        context=context,
        verdict=verdict,
        policy=policy,
    )
    if block_reason:
        return verdict
    evidence = dict(verdict.get("evidence") or {})
    current_regime = str(
        evidence.get("current_regime")
        or (context.get("market") or {}).get("regime_id")
        or (context.get("risk") or {}).get("current_regime")
        or ""
    ).strip()
    if not current_regime or current_regime.lower() in {"unknown", "none", "unavailable"}:
        return verdict
    binding = dict(policy.get("binding") or {})
    selection_key = dict(binding.get("selection_key") or {})
    previous_regime = str(selection_key.get("current_regime") or "").strip()
    if not previous_regime or previous_regime.lower() in {"unknown", "none", "unavailable"}:
        return verdict
    if current_regime == previous_regime:
        return verdict

    closed_bar_key = str(
        evidence.get("closed_bar_key")
        or (context.get("temporal_context") or {}).get("closed_bar_key")
        or ""
    )
    try:
        bar_number = int(evidence.get("completed_bars_after_entry"))
    except (TypeError, ValueError):
        return verdict
    if not closed_bar_key or bar_number < 0:
        return verdict
    position_id = int(position.get("position_id") or position.get("ticket") or 0)
    if position_id <= 0:
        return verdict

    row = _load_recovery_row_for_risk_reduction(
        position_id,
        operation="position_supervisor_binding_switch",
    )
    meta = copy.deepcopy(dict((row or {}).get("recovery_meta") or {}))
    state = _position_supervisor_switch_state_payload(
        meta.get("supervisor_switch_state")
    )
    if state["candidate_regime"] != current_regime:
        state["candidate_regime"] = current_regime
        state["candidate_bar_key"] = closed_bar_key
        state["stable_bars"] = 1
    elif state["last_seen_bar_key"] != closed_bar_key:
        state["stable_bars"] = int(state["stable_bars"] or 0) + 1
        state["candidate_bar_key"] = closed_bar_key
    state["last_seen_bar_key"] = closed_bar_key
    min_stable_bars = max(
        1,
        int(getattr(cfg, "position_supervisor_switch_min_stable_bars", 2) or 2),
    )
    plan = dict(meta.get("entry_protection_plan") or {})
    persisted_binding = plan.get("supervisor_binding")
    if not isinstance(persisted_binding, Mapping):
        return verdict
    persisted_check = verify_position_supervisor_binding(persisted_binding)
    if not persisted_check.get("valid") or str(
        persisted_binding.get("template_hash") or ""
    ) != str(binding.get("template_hash") or ""):
        return verdict
    state_changed = state != _position_supervisor_switch_state_payload(
        meta.get("supervisor_switch_state")
    )

    def persist_state() -> None:
        if state_changed:
            live_close_settlement.merge_recovery_position_meta(
                position_id,
                {"supervisor_switch_state": state},
            )

    if int(state["stable_bars"] or 0) < min_stable_bars:
        persist_state()
        return verdict
    if state["last_selection_bar_key"] == closed_bar_key:
        persist_state()
        return verdict
    max_switches = max(
        0,
        int(getattr(cfg, "position_supervisor_max_switches_per_position", 2) or 0),
    )
    if max_switches <= 0 or int(state["switch_count"] or 0) >= max_switches:
        state["last_selection_bar_key"] = closed_bar_key
        state["last_selection_reason"] = "max_switches_reached"
        live_close_settlement.merge_recovery_position_meta(position_id, {"supervisor_switch_state": state})
        return verdict
    cooldown_bars = max(
        0,
        int(getattr(cfg, "position_supervisor_switch_cooldown_bars", 3) or 0),
    )
    if (
        int(state["last_switch_bar_number"] or -1) >= 0
        and bar_number - int(state["last_switch_bar_number"]) < cooldown_bars
    ):
        state["last_selection_bar_key"] = closed_bar_key
        state["last_selection_reason"] = "switch_cooldown"
        live_close_settlement.merge_recovery_position_meta(position_id, {"supervisor_switch_state": state})
        return verdict

    try:
        projection = live_close_settlement.runtime_kv_get(POSITION_SUPERVISOR_SELECTION_PROJECTION_KEY, {})
        selection = select_position_supervisor_binding(
            projection if isinstance(projection, dict) else {},
            symbol=str(position.get("symbol") or "XAUUSD+"),
            timeframe=str(getattr(cfg, "timeframe", "M5") or "M5"),
            entry_regime=str(selection_key.get("entry_regime") or ""),
            current_regime=current_regime,
            current_binding=binding,
            now_ts=now_ts,
            max_age_seconds=float(
                getattr(cfg, "position_supervisor_selection_max_age_seconds", 900.0)
                or 900.0
            ),
        )
    except Exception as exc:
        selection = {
            "ok": False,
            "changed": False,
            "reason": f"selection_projection_unavailable:{type(exc).__name__}",
        }
    state["last_selection_bar_key"] = closed_bar_key
    state["last_selection_reason"] = str(selection.get("reason") or "selection_not_available")
    selected = dict(selection.get("selected_binding") or selection.get("binding") or {})
    selected_check = verify_position_supervisor_binding(selected)
    selection_event_id = str(selection.get("selection_event_id") or "")
    selection_mode = str(
        getattr(cfg, "position_supervisor_auto_selection_mode", "off") or "off"
    ).strip().lower()
    if selection_mode == "shadow":
        if not _LEDGER:
            state["last_selection_reason"] = "supervisor_trace_sink_unavailable"
            live_close_settlement.merge_recovery_position_meta(
                position_id,
                {"supervisor_switch_state": state},
            )
            return verdict
        shadow_verdict = copy.deepcopy(verdict)
        shadow_evidence = dict(shadow_verdict.get("evidence") or {})
        shadow_evidence.update(
            {
                "selection_event_id": selection_event_id,
                "position_supervisor_selection": {
                    "schema_version": "position_supervisor_selection_observation.v1",
                    "ok": bool(selection.get("ok")),
                    "changed": bool(selection.get("changed")),
                    "reason": str(selection.get("reason") or "selection_not_available"),
                    "selected_template_id": str(selected.get("template_id") or ""),
                    "selected_template_version": str(
                        selected.get("template_version") or ""
                    ),
                    "selected_template_hash": str(selected.get("template_hash") or ""),
                    "selection_event_id": selection_event_id,
                },
            }
        )
        if selected_check.get("valid"):
            shadow_evidence["position_supervisor_selection"]["selected_binding"] = selected
        shadow_verdict["evidence"] = shadow_evidence
        shadow_trace_id = _log_supervisor_trace(
            position=position,
            verdict=shadow_verdict,
            cfg=cfg,
            tick=int(evidence.get("tick") or 0),
            stage="selection_shadow",
            outcome="shadow",
            execution_status="shadow_only",
            execution_reason=str(
                selection.get("reason") or "selection_not_available"
            ),
            execution={
                "policy_switch_status": "shadow",
                "selection_event_id": selection_event_id,
                "selected_binding": selected if selected_check.get("valid") else {},
                "broker_action_attempted": False,
                "is_real_execution": False,
                "no_change_reason": str(selection.get("reason") or ""),
            },
            acct=live_state_get("account", {}, clone=True) or {},
        )
        state["last_selection_reason"] = (
            f"shadow:{selection.get('reason') or 'selection_not_available'}"
            if shadow_trace_id
            else "supervisor_trace_persist_failed"
        )
        live_close_settlement.merge_recovery_position_meta(
            position_id,
            {"supervisor_switch_state": state},
        )
        return verdict
    if (
        not selection.get("ok")
        or not selection.get("changed")
        or not selection_event_id
        or not selected_check.get("valid")
        or str(selected.get("template_hash") or "")
        == str(binding.get("template_hash") or "")
    ):
        live_close_settlement.merge_recovery_position_meta(position_id, {"supervisor_switch_state": state})
        return verdict

    if not _LEDGER:
        state["last_selection_reason"] = "supervisor_trace_sink_unavailable"
        live_close_settlement.merge_recovery_position_meta(position_id, {"supervisor_switch_state": state})
        return verdict

    new_template = dict(selected_check.get("template") or {})
    switch_ts = float(now_ts or time.time())
    old_binding = dict(persisted_binding)
    previous_history = [
        dict(item)
        for item in list(meta.get("position_supervisor_binding_history") or [])
        if isinstance(item, Mapping)
    ]
    history = [old_binding, *previous_history][:3]
    next_plan = dict(plan)
    next_plan.update(
        {
            "supervisor_binding": selected,
            "supervisor_binding_previous": old_binding,
            "supervisor_binding_switched_at": switch_ts,
        }
    )
    state["switch_count"] = int(state["switch_count"] or 0) + 1
    state["last_switch_bar_number"] = bar_number
    state["last_switch_bar_key"] = closed_bar_key
    state["last_switch_ts"] = switch_ts
    state["last_selection_reason"] = "selected_highest_positive_effect"
    next_meta = {
        "entry_protection_plan": next_plan,
        "position_supervisor_binding_history": history,
        "supervisor_switch_state": state,
        "position_supervisor_last_switch": {
            "selection_event_id": selection_event_id,
            "previous_template_id": str(old_binding.get("template_id") or ""),
            "previous_template_hash": str(old_binding.get("template_hash") or ""),
            "template_id": str(selected.get("template_id") or ""),
            "template_hash": str(selected.get("template_hash") or ""),
            "switched_at": switch_ts,
            "regime": current_regime,
        },
    }
    live_close_settlement.merge_recovery_position_meta(position_id, next_meta)

    switch_evidence = dict(evidence)
    switch_evidence.update(
        {
            "position_supervisor_binding": selected,
            "previous_position_supervisor_binding": old_binding,
            "position_supervisor_binding_state": "bound",
            "binding_source": str(selected.get("binding_source") or ""),
            "supervisor_template_hash": str(selected.get("template_hash") or ""),
            "selection_event_id": selection_event_id,
            "current_regime": current_regime,
            "policy_switch_status": "applied",
        }
    )
    switch_verdict = copy.deepcopy(verdict)
    switch_verdict["supervisor_template"] = new_template
    switch_verdict["evidence"] = switch_evidence
    switch_verdict["position_supervisor_policy"] = {
        **dict(policy),
        "binding_state": "bound",
        "binding_reason": "policy_switch_applied",
        "binding_source": str(selected.get("binding_source") or ""),
        "template_id": str(selected.get("template_id") or ""),
        "template_version": str(selected.get("template_version") or ""),
        "template_hash": str(selected.get("template_hash") or ""),
        "selection_event_id": selection_event_id,
        "binding": selected,
    }
    trace_id = _log_supervisor_trace(
        position=position,
        verdict=switch_verdict,
        cfg=cfg,
        tick=int(evidence.get("tick") or 0),
        stage="policy_switch",
        outcome="applied",
        execution_status="no_op",
        execution_reason="position_supervisor_binding_switched",
        execution={
            "policy_switch_status": "applied",
            "selection_event_id": selection_event_id,
            "previous_binding": old_binding,
            "binding": selected,
            "broker_action_attempted": False,
            "is_real_execution": False,
            "no_change_reason": "",
        },
        acct=live_state_get("account", {}, clone=True) or {},
    )
    if not trace_id:
        # Do not leave a binding that cannot be proven by a trace.  Restore the
        # previous object and all switch-owned metadata with a CAS.  Do not
        # overwrite an unrelated concurrent recovery update.
        latest_row = live_close_settlement.load_recovery_position_row(position_id)
        latest_meta = copy.deepcopy(dict((latest_row or {}).get("recovery_meta") or {}))
        restored_meta = dict(latest_meta)
        for key in (
            "entry_protection_plan",
            "position_supervisor_binding_history",
            "supervisor_switch_state",
            "position_supervisor_last_switch",
        ):
            if key in meta:
                restored_meta[key] = copy.deepcopy(meta[key])
            else:
                restored_meta.pop(key, None)
        restored = _replace_recovery_position_meta(
            position_id,
            restored_meta,
            expected_meta=latest_meta,
        )
        if not restored:
            logger.warning(
                "position supervisor binding rollback CAS failed position_id={}",
                position_id,
            )
        return verdict
    switch_evidence.pop("policy_switch_status", None)
    verdict["evidence"] = switch_evidence
    verdict["position_supervisor_policy"] = switch_verdict["position_supervisor_policy"]
    verdict["supervisor_template"] = new_template
    return verdict


def _update_entry_protection_plan_status(
    position_id: int,
    *,
    status: str,
    error: str = "",
    attempted: bool = False,
    applied_sl: float = 0.0,
    applied_tp: float = 0.0,
) -> None:
    row = live_close_settlement.load_recovery_position_row(int(position_id))
    meta = dict((row or {}).get("recovery_meta") or {})
    plan = dict(meta.get("entry_protection_plan") or {})
    if not plan:
        return
    now = time.time()
    plan = _lifecycle_update_entry_protection_plan_payload(
        plan=plan,
        status=status,
        updated_at=now,
        error=error,
        attempted=attempted,
        applied_sl=applied_sl,
        applied_tp=applied_tp,
    )
    meta["entry_protection_plan"] = plan
    live_close_settlement.merge_recovery_position_meta(int(position_id), meta)


def _remember_pending_open_attach(position_id: int) -> None:
    _lifecycle_remember_pending_open_attach(
        _pending_open_attach_until,
        position_id,
        ttl_seconds=_PENDING_OPEN_ATTACH_TTL_SECONDS,
    )


def _entry_protection_latch_runtime() -> EntryProtectionLatchRuntime:
    return EntryProtectionLatchRuntime(
        activate_latch=activate_no_new_risk_latch,
        release_latch_cause=release_no_new_risk_latch_cause,
        latch_status=no_new_risk_latch_status,
        append_safety_outbox=append_safety_outbox,
        live_state_update=live_state_update,
        reconcile_value=_reconcile_value,
        pending_open_attach_until=_pending_open_attach_until,
        now=time.time,
    )


def _activate_entry_protection_pending_latch(
    position_id: int,
    *,
    broker: str,
    tick: int,
) -> dict[str, Any]:
    return _entry_protection_activate_latch(
        position_id,
        broker=broker,
        tick=tick,
        runtime=_entry_protection_latch_runtime(),
    )


def _release_entry_protection_pending_latch(
    position_id: int,
    *,
    reconcile: Any,
    expected_stop_loss: float,
    expected_take_profit: float,
) -> dict[str, Any]:
    return _entry_protection_release_latch(
        position_id,
        reconcile=reconcile,
        expected_stop_loss=expected_stop_loss,
        expected_take_profit=expected_take_profit,
        runtime=_entry_protection_latch_runtime(),
    )


def _active_pending_open_attach_ids(current_position_ids: set[int] | None = None) -> list[int]:
    return _lifecycle_active_pending_open_attach_ids(
        _pending_open_attach_until,
        current_position_ids,
    )


def _trade_attribution_payload_from_composite(
    *,
    position_id: int,
    open_ts: float,
    open_price: float,
    direction: int,
    actual_api_volume: float,
    composite,
) -> dict[str, Any]:
    return _lifecycle_build_trade_attribution_payload_from_composite(
        position_id=position_id,
        open_ts=open_ts,
        open_price=open_price,
        direction=direction,
        actual_api_volume=actual_api_volume,
        composite=composite,
    )


def _restore_attribution_for_positions(attr_engine, positions: list[Any] | None) -> int:
    return _lifecycle_restore_attribution_for_positions(
        attr_engine,
        positions,
        load_recovery_row=live_close_settlement.load_recovery_position_row,
        debug_log=lambda pid, exc: logger.debug(
            "[live] attribution restore skipped for pos {}: {}",
            pid,
            exc,
        ),
    )


def _current_regime_hint() -> str:
    return _lifecycle_current_regime_hint_from_composite(
        live_state_get("last_composite", clone=True) or {}
    )


def _position_path_metrics_runtime() -> PositionPathMetricsRuntime:
    return PositionPathMetricsRuntime(
        position_id=_position_id_value,
        holding_summary=_holding_summary_for_position,
        load_recovery_row=_load_recovery_row_for_risk_reduction,
        lookup_entry_context=_lookup_entry_context_for_risk_reduction,
        build_inputs=_lifecycle_build_position_path_metrics_inputs,
        current_regime_hint=_current_regime_hint,
        position_unrealized_pnl=_position_unrealized_pnl,
        now=time.time,
        loop_strategy_name=_current_loop_strategy_name("factor_pipeline_v4"),
        default_context_integrity=_RECOVERY_CONTEXT_PARTIAL,
        build_update=_lifecycle_build_position_path_metrics_update,
        normalize_path_state=normalize_path_state,
        update_path_metrics=update_position_path_metrics,
        upsert_recovery_position=live_close_settlement.upsert_recovery_position_state,
        record_aux_failure=live_close_settlement.record_risk_reduction_aux_failure,
    )


def _position_path_metrics_for_position(
    position: Any,
    *,
    cfg=None,
    now_ts: float | None = None,
    persist: bool = False,
    broker: str = "",
    strategy_name: str = "",
) -> dict[str, Any]:
    return _runtime_position_path_metrics(
        position,
        cfg=cfg,
        now_ts=now_ts,
        persist=persist,
        broker=broker,
        strategy_name=strategy_name,
        runtime=_position_path_metrics_runtime(),
    )


def _build_position_supervisor_context(
    position: dict[str, Any],
    *,
    cfg=None,
    acct: dict | None = None,
    now_ts: float | None = None,
    positions: list[Any] | None = None,
    broker_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now_ts = float(now_ts or time.time())
    temporal_context = _build_close_position_risk_context(
        position_id=int(position.get("position_id") or position.get("ticket") or 0),
        close_reason="position_supervisor",
        mode="supervisor",
        symbol=str(position.get("symbol") or "XAUUSD+"),
        position=position,
        cfg=cfg,
        decision_ts=now_ts,
        broker_schedule=broker_schedule,
    )
    position_metrics = _position_path_metrics_for_position(position, cfg=cfg, now_ts=now_ts, persist=False)
    supervisor_row = _load_recovery_row_for_risk_reduction(
        int(position.get("position_id") or position.get("ticket") or 0),
        operation="position_supervisor_context",
    )
    supervisor_state = dict((supervisor_row or {}).get("recovery_meta") or {})
    supervisor_template, supervisor_policy = _position_supervisor_policy_for_position(
        cfg=cfg,
        supervisor_state=supervisor_state,
        position=position,
        position_metrics=position_metrics,
    )
    context_inputs = _lifecycle_build_position_supervisor_context_inputs(
        position=position,
        cfg=cfg,
        positions=positions,
        account=acct,
        entry_decision_id=_lookup_entry_decision_for_risk_reduction(
            int(position.get("position_id") or position.get("ticket") or 0),
            operation="position_supervisor_context",
        ),
        risk_snapshot=live_state_get("risk", {}, clone=True) or {},
        total_api_volume=_tracked_total_api_volume(positions or []),
        market_context=live_state_get("last_composite", {}, clone=True) or {},
        supervisor_state=supervisor_state,
        loop_running=bool(live_state_get("loop_running", True)),
        position_supervisor_template=supervisor_template,
        position_supervisor_policy=supervisor_policy,
    )
    return _lifecycle_build_position_supervisor_context_payload(
        **context_inputs,
        temporal_context=temporal_context,
        position_metrics=position_metrics,
    )


def _get_position_quality_advisor():
    return _POSITION_QUALITY_ADVISOR


def _set_position_quality_advisor(advisor) -> None:
    global _POSITION_QUALITY_ADVISOR
    _POSITION_QUALITY_ADVISOR = advisor


def _create_position_quality_advisor():
    from research.position_quality_lightgbm import PositionQualityLightGBMService

    return PositionQualityLightGBMService()


def _evaluate_position_supervisor_for_position(
    position: dict[str, Any],
    *,
    cfg=None,
    acct: dict | None = None,
    now_ts: float | None = None,
    positions: list[Any] | None = None,
    persist: bool = False,
    broker: str = "",
    strategy_name: str = "",
    broker_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from backend.services.model_influence import shared_model_influence_service
    from backend.services.position_supervisor import build_model_tighten_controls

    def build_context(position_value: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        return _build_position_supervisor_context(
            position_value,
            broker_schedule=broker_schedule,
            **kwargs,
        )

    after_persist = (
        partial(_maybe_switch_position_supervisor_binding, position=position, cfg=cfg,
                now_ts=float(now_ts or time.time()))
        if persist else None
    )

    runtime = PositionSupervisorEvaluationRuntime(
        build_context=build_context,
        evaluate_rule=evaluate_position_supervisor,
        get_quality_advisor=_get_position_quality_advisor,
        set_quality_advisor=_set_position_quality_advisor,
        quality_advisor_factory=_create_position_quality_advisor,
        model_influence_service=shared_model_influence_service,
        build_model_tighten_controls=build_model_tighten_controls,
        load_recovery_row=_load_recovery_row_for_risk_reduction,
        upsert_recovery_position=live_close_settlement.upsert_recovery_position_state,
        build_state_upsert_payload=_lifecycle_build_supervisor_state_upsert_payload,
        loop_strategy_name=_current_loop_strategy_name(""),
        default_context_integrity=_RECOVERY_CONTEXT_PARTIAL,
        record_aux_failure=live_close_settlement.record_risk_reduction_aux_failure,
        after_persist=after_persist,
    )
    verdict = _runtime_evaluate_position(
        position,
        runtime=runtime,
        cfg=cfg,
        account=acct,
        now_ts=now_ts,
        positions=positions,
        persist=persist,
        broker=broker,
        strategy_name=strategy_name,
    )
    return verdict


def _enrich_positions_with_path_metrics(
    pos_list: list[Any],
    *,
    cfg=None,
    now_ts: float | None = None,
    persist: bool = False,
    broker: str = "",
    strategy_name: str = "",
) -> list[dict]:
    now_ts = float(now_ts or time.time())
    return _lifecycle_enrich_positions_with_lifecycle_metrics(
        pos_list,
        cfg=cfg,
        now_ts=now_ts,
        persist=persist,
        broker=broker,
        strategy_name=strategy_name,
        coerce_positions=_coerce_live_positions,
        apply_unrealized_pnl_fields_fn=_apply_unrealized_pnl_fields,
        holding_summary_for_position=_holding_summary_for_position,
        position_path_metrics_for_position=_position_path_metrics_for_position,
        evaluate_position_supervisor_for_position=_evaluate_position_supervisor_for_position,
    )


def _supervisor_risk_context(
    position: dict[str, Any],
    verdict: dict[str, Any],
    *,
    cfg=None,
    mode: str = "live",
    broker_schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    close_inputs = _lifecycle_build_supervisor_close_context_inputs(
        position=position,
        verdict=verdict,
        mode=mode,
        broker="ctrader",
    )
    close_context = _build_close_position_risk_context(
        **close_inputs,
        cfg=cfg,
        broker_schedule=broker_schedule,
    )
    return _lifecycle_build_supervisor_risk_context_payload(
        close_context=close_context,
        position=position,
        verdict=verdict,
    )


def _remember_supervisor_state(
    position: dict[str, Any],
    verdict: dict[str, Any],
    *,
    action_applied: str = "",
    broker: str = "ctrader",
    strategy_name: str = "",
) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    row = _load_recovery_row_for_risk_reduction(pid, operation="remember_supervisor_state")
    try:
        live_close_settlement.upsert_recovery_position_state(
            position,
            **_lifecycle_build_supervisor_state_upsert_payload(
                recovery_row=row,
                verdict=verdict,
                broker=broker,
                strategy_name=strategy_name,
                loop_strategy_name=_current_loop_strategy_name(),
                default_context_integrity=_RECOVERY_CONTEXT_PARTIAL,
                action_applied=action_applied,
                applied_ts=time.time() if action_applied else 0.0,
            ),
        )
    except Exception as exc:
        live_close_settlement.record_risk_reduction_aux_failure(
            "risk_reduction_state_persist_failed",
            position_id=pid,
            action="remember_supervisor_state",
            error=exc,
        )


def _remember_protection_state(
    position: dict[str, Any],
    verdict: dict[str, Any],
    *,
    source: str,
    action_applied: str = "",
    broker: str = "ctrader",
    strategy_name: str = "",
) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    row = _load_recovery_row_for_risk_reduction(pid, operation="remember_protection_state")
    try:
        live_close_settlement.upsert_recovery_position_state(
            position,
            **_lifecycle_build_protection_state_upsert_payload(
                recovery_row=row,
                verdict=verdict,
                source=source,
                broker=broker,
                strategy_name=strategy_name,
                loop_strategy_name=_current_loop_strategy_name(),
                default_context_integrity=_RECOVERY_CONTEXT_PARTIAL,
                action_applied=action_applied,
                applied_ts=time.time() if action_applied else 0.0,
            ),
        )
    except Exception as exc:
        live_close_settlement.record_risk_reduction_aux_failure(
            "risk_reduction_state_persist_failed",
            position_id=pid,
            action="remember_protection_state",
            error=exc,
        )


def _supervisor_recently_applied(position_id: int, action: str, cooldown_seconds: float = 300.0) -> bool:
    row = _load_recovery_row_for_risk_reduction(
        position_id,
        operation="supervisor_cooldown",
    )
    meta = dict((row or {}).get("recovery_meta") or {})
    return _lifecycle_supervisor_recently_applied_from_meta(
        recovery_meta=meta,
        action=action,
        now_ts=time.time(),
        cooldown_seconds=cooldown_seconds,
    )


def _supervisor_noop_fingerprint_seen(position_id: int, fingerprint: str) -> bool:
    row = _load_recovery_row_for_risk_reduction(
        position_id,
        operation="supervisor_noop_fingerprint",
    )
    return _lifecycle_supervisor_noop_fingerprint_seen(
        recovery_meta=dict((row or {}).get("recovery_meta") or {}),
        fingerprint=fingerprint,
    )


def _supervisor_adaptive_duplicate_seen(
    position_id: int,
    verdict: dict[str, Any],
) -> bool:
    """Suppress repeated discretionary recommendations in one bar/episode."""

    evidence = dict(verdict.get("evidence") or {})
    closed_bar_key = str(evidence.get("closed_bar_key") or "")
    trigger_key = "|".join(
        sorted({str(item) for item in evidence.get("trigger_tags") or [] if str(item)})
    )
    if not closed_bar_key or not trigger_key:
        return False
    if is_hard_supervisor_action(
        action=str(verdict.get("requested_action") or verdict.get("action") or ""),
        summary_reason=str(verdict.get("summary_reason") or ""),
        evidence=evidence,
    ):
        return False
    fingerprint = str(verdict.get("action_fingerprint") or "")
    if not fingerprint:
        return False
    row = _load_recovery_row_for_risk_reduction(
        int(position_id or 0),
        operation="supervisor_adaptive_duplicate",
    )
    meta = dict((row or {}).get("recovery_meta") or {})
    return bool(
        str(meta.get("supervisor_last_adaptive_closed_bar_key") or "")
        == closed_bar_key
        and str(meta.get("supervisor_last_adaptive_trigger_key") or "")
        == trigger_key
        and str(meta.get("supervisor_last_adaptive_fingerprint") or "")
        == fingerprint
        and str(meta.get("supervisor_posture") or "")
        == str(evidence.get("supervisor_posture") or "")
    )


def _remember_supervisor_noop(position: dict[str, Any], verdict: dict[str, Any], *, fingerprint: str, reason: str) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    _remember_supervisor_state(
        position,
        verdict,
        broker="ctrader",
        strategy_name=_current_loop_strategy_name(),
    )
    live_close_settlement.merge_recovery_position_meta(
        pid,
        {
            "last_supervisor_noop_fingerprint": str(fingerprint or ""),
            "last_supervisor_noop_reason": str(reason or ""),
            "last_supervisor_noop_ts": time.time(),
        },
    )


def _log_supervisor_decision(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    risk_verdict: dict[str, Any] | None,
    acct: dict | None,
    cfg,
    event_type: str,
    tick: int,
) -> str:
    if not _LEDGER:
        return ""
    try:
        return _LEDGER.log_decision(
            **_lifecycle_build_supervisor_decision_ledger_payload(
                position=position,
                verdict=verdict,
                risk_state=live_close_settlement.risk_state_with_verdict_dict(risk_verdict or {}),
                risk_verdict=risk_verdict,
                account=acct,
                cfg=cfg,
                event_type=event_type,
                tick=tick,
                session_pnl=live_state_get("session_pnl", 0.0),
                fallback_decision_ts=time.time(),
            )
        )
    except Exception as exc:
        logger.warning("[live] supervisor ledger failed for pos {}: {}", position.get("position_id"), exc)
        return ""


_float_payload_value = _lifecycle_float_payload_value
_direction_from_position = _lifecycle_position_direction_from_payload
_side_name = _lifecycle_side_name


def _runtime_config_anchor() -> dict[str, Any]:
    try:
        from backend.services.evolution_ledger import current_runtime_config_snapshot

        snapshot = current_runtime_config_snapshot(create_if_missing=False)
        return {
            "config_version": int(snapshot.get("config_version") or 0),
            "config_hash": str(snapshot.get("config_hash") or ""),
        }
    except Exception:
        return {"config_version": 0, "config_hash": ""}


def _candidate_verdict(candidate: ProtectionCandidate) -> dict[str, Any]:
    return _lifecycle_build_protection_candidate_verdict_payload(
        position_id=candidate.position_id,
        decision_ts=time.time(),
        action=candidate.action,
        confidence=float((candidate.evidence or {}).get("confidence", 0.0) or 0.0),
        reason=candidate.reason,
        source=candidate.source,
        evidence=candidate.evidence,
        controls=candidate.controls,
        config_version=int(candidate.config_version or 0),
        config_hash=str(candidate.config_hash or ""),
        position_side=_side_name(_direction_from_position(candidate.position or {})),
    )


def _log_protection_candidate_superseded(
    candidate: ProtectionCandidate,
    *,
    cfg,
    tick: int,
    reason: str,
    acct: dict | None = None,
) -> None:
    if not candidate.position:
        return
    trace_fields = _lifecycle_build_protection_superseded_trace_fields(
        candidate_payload=asdict(candidate),
        risk_action=candidate.risk_action,
        reason=reason,
    )
    _log_supervisor_trace(
        position=candidate.position,
        verdict=_candidate_verdict(candidate),
        cfg=cfg,
        tick=tick,
        **trace_fields,
        acct=acct,
    )


def _supervisor_tighten_sl_plan(position: dict[str, Any], target_sl: float, quote: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        from config.runtime_config import shared as _runtime_cfg

        cfg = _runtime_cfg()
        policy = {
            "min_stop_distance_points": getattr(cfg, "supervisor_min_stop_distance_points", 0.20),
            "stop_safety_buffer_ratio": getattr(cfg, "supervisor_stop_safety_buffer_ratio", 0.00008),
            "min_tighten_delta_points": getattr(cfg, "supervisor_min_tighten_delta_points", 0.01),
            "quote_max_age_seconds": getattr(cfg, "supervisor_quote_max_age_seconds", 10.0),
        }
    except Exception:
        policy = {}
    return _lifecycle_build_supervisor_tighten_sl_plan(
        **_lifecycle_build_supervisor_tighten_sl_plan_inputs(
            position=position,
            target_sl=target_sl,
            quote=quote,
            policy=policy,
        ),
    )


def _target_tp_is_extension(position: dict[str, Any], target_tp: float) -> bool:
    return _lifecycle_target_tp_is_extension(
        **_lifecycle_build_target_tp_extension_inputs(
            position=position,
            target_tp=target_tp,
        ),
    )


def _log_supervisor_position_event(
    *,
    position: dict[str, Any],
    event_type: str,
    details: dict[str, Any],
    realized_pnl: float = 0.0,
) -> None:
    if not _LEDGER:
        return
    try:
        _LEDGER.log_position_event(
            **_lifecycle_build_supervisor_position_event_payload(
                position=position,
                event_type=event_type,
                details=details,
                realized_pnl=realized_pnl,
            )
        )
    except Exception as exc:
        logger.debug("[live] supervisor position event {} failed for pos {}: {}", event_type, position.get("position_id"), exc)


def _log_supervisor_trace(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    cfg,
    tick: int,
    stage: str,
    outcome: str,
    decision_id: str = "",
    risk_action: str = "",
    risk_verdict: dict[str, Any] | None = None,
    execution_status: str = "",
    execution_reason: str = "",
    execution: dict[str, Any] | None = None,
    acct: dict | None = None,
) -> str:
    if not _LEDGER:
        return ""
    try:
        return _LEDGER.log_position_supervisor_trace(
            **_lifecycle_build_supervisor_trace_ledger_payload(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage=stage,
                outcome=outcome,
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_verdict,
                execution_status=execution_status,
                execution_reason=execution_reason,
                execution=execution,
                account=acct,
                fallback_event_ts=time.time(),
            )
        )
    except Exception as exc:
        logger.opt(exception=True).warning(
            "[live] supervisor trace failed for pos {}: {}",
            position.get("position_id"),
            exc,
        )
        return ""


_supervisor_evaluation_bars: dict[int, str] = {}


def _log_supervisor_evaluation(
    *,
    position_id: int,
    verdict: dict[str, Any],
    tick: int,
    decision_ts: float | None = None,
) -> str:
    """Bar-deduplicated lean evaluation ledger row (see ledger method)."""
    if not _LEDGER:
        return ""
    try:
        evidence = dict((verdict or {}).get("evidence") or {})
        bar_key = str(evidence.get("closed_bar_key") or "")
        if not bar_key:
            return ""
        pid = int(position_id or 0)
        if _supervisor_evaluation_bars.get(pid) == bar_key:
            return ""
        _supervisor_evaluation_bars[pid] = bar_key
        return _LEDGER.log_position_supervisor_evaluation(
            position_id=str(pid),
            event_ts=float(decision_ts if decision_ts is not None else time.time()),
            verdict=verdict,
        )
    except Exception as exc:
        logger.opt(exception=True).warning(
            "[live] supervisor evaluation ledger failed for pos {}: {}",
            position_id,
            exc,
        )
        return ""


def _delegate_timeout_supervisor_close(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    cfg: Any,
    tick: int,
    acct: dict[str, Any],
    broker_schedule: dict[str, Any] | None = None,
) -> bool:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    timeout_context = _build_close_position_risk_context(
        position_id=pid,
        close_reason="holding_timeout",
        mode="live",
        broker="ctrader",
        symbol=str(position.get("symbol") or "XAUUSD+"),
        position=position,
        cfg=cfg,
        broker_schedule=broker_schedule,
    )
    timeout_holding_seconds = float(timeout_context.get("holding_seconds", 0.0) or 0.0)
    timeout_limit_seconds = float(timeout_context.get("max_holding_seconds", 0.0) or 0.0)
    _log_supervisor_trace(
        position=position,
        verdict=verdict,
        cfg=cfg,
        tick=tick,
        stage="timeout_delegated",
        outcome="skipped",
        execution_status="delegated",
        execution_reason="main_timeout_path",
        execution={"timeout_context": timeout_context},
        acct=acct,
    )
    return bool(
        timeout_limit_seconds > 0
        and _lifecycle_holding_timeout_is_expired(timeout_context)
    )


def _build_position_supervision_runtime(
    bridge: Any,
    *,
    tick: int,
) -> LiveSupervisionRuntime:
    broker_schedule = _broker_schedule_from_bridge(bridge)
    return LiveSupervisionRuntime(
        logger=logger,
        strategy_name=_current_loop_strategy_name(),
        ledger=_LEDGER,
        evaluate_position=lambda position, **kwargs: _evaluate_position_supervisor_for_position(
            position,
            broker_schedule=broker_schedule,
            **kwargs,
        ),
        record_aux_failure=live_close_settlement.record_risk_reduction_aux_failure,
        log_trace=_log_supervisor_trace,
        log_evaluation=_log_supervisor_evaluation,
        make_candidate=safety_candidate,
        recently_applied=_supervisor_recently_applied,
        delegate_timeout_close=lambda **kwargs: _delegate_timeout_supervisor_close(
            broker_schedule=broker_schedule,
            **kwargs,
        ),
        build_tighten_execution_plan=_lifecycle_build_supervisor_tighten_execution_plan,
        build_action_fingerprint=_lifecycle_build_supervisor_action_fingerprint,
        noop_fingerprint_seen=_supervisor_noop_fingerprint_seen,
        remember_noop=_remember_supervisor_noop,
        risk_action_for_action=_lifecycle_supervisor_risk_action_for_action,
        build_risk_evaluation_inputs=(
            _lifecycle_build_supervisor_runtime_risk_evaluation_inputs
        ),
        supervisor_risk_context=lambda position, verdict, **kwargs: _supervisor_risk_context(
            position,
            verdict,
            broker_schedule=broker_schedule,
            **kwargs,
        ),
        live_state_get=live_state_get,
        evaluate_risk_policy=_evaluate_risk_reduction_policy,
        log_decision=_log_supervisor_decision,
        remember_state=_remember_supervisor_state,
        execute_tighten=_execute_supervisor_tighten_action,
        execute_reduce=_execute_supervisor_reduce_action,
        execute_close=_execute_supervisor_close_action,
        build_tighten_result_payloads=(
            _lifecycle_build_supervisor_tighten_result_payloads
        ),
        log_position_event=_log_supervisor_position_event,
        remember_reentry_block=_remember_supervisor_reentry_block,
        track_local_sl_tp=_track_local_sl_tp,
        result_is_position_not_found=live_close_settlement.result_is_position_not_found,
        retire_broker_missing_position=live_close_settlement.retire_broker_missing_position,
        reconcile_positions=_explicit_position_reconcile,
        verify_protection_projection=_verify_position_protection_projection,
        publish_fresh_positions=lambda result: _publish_fresh_position_reconcile(
            result,
            broker="ctrader",
        ),
        persist_safety_fail_closed=live_safety_watchdog.persist_safety_fail_closed,
        plan_reduce=lambda **kwargs: _plan_supervisor_reduce_action(
            **kwargs,
            floor_api_volume_to_step=_floor_api_volume_to_step,
            should_full_close_untradeable_reduce=(
                _should_full_close_untradeable_reduce
            ),
        ),
        normalize_reduce=_normalize_supervisor_reduce_verdict,
        remember_close_reason=live_close_settlement.remember_close_reason,
        remember_close_verdict=live_close_settlement.remember_close_verdict,
        capture_partial_close_session_cursor=lambda **kwargs: (
            live_close_settlement.capture_partial_close_deal_cursor(**kwargs)
        ),
        sync_partial_close_session_fact=lambda **kwargs: (
            live_close_settlement.sync_partial_close_session_fact(
                bridge,
                broker="ctrader",
                tick=tick,
                **kwargs,
            )
        ),
        adaptive_duplicate_seen=_supervisor_adaptive_duplicate_seen,
    )


@record_timed("live.position_supervision")
def _run_position_supervision(
    bridge,
    pos: list,
    *,
    cfg,
    acct: dict,
    tick: int,
    log,
    skip_position_ids: set[int] | None = None,
    preaudited_skip_position_ids: set[int] | None = None,
    record_partial_close_execution=None,
    decision_ts: float | None = None,
    candidate_recorder=None,
    planned_verdicts: dict[int, dict[str, Any]] | None = None,
) -> set[int]:
    runtime = _build_position_supervision_runtime(bridge, tick=tick)
    return _runtime_run_position_supervision(
        bridge,
        pos,
        cfg=cfg,
        account=acct,
        tick=tick,
        log=log,
        runtime=runtime,
        skip_position_ids=skip_position_ids,
        preaudited_skip_position_ids=preaudited_skip_position_ids,
        record_partial_close_execution=record_partial_close_execution,
        decision_ts=decision_ts,
        candidate_recorder=candidate_recorder,
        planned_verdicts=planned_verdicts,
    )




def _resolve_position_api_volume(
    position_id: int,
    positions: list[Any] | None,
    fallback_volume: float,
) -> float:
    """Resolve the actual API volume for a filled position_id.

    We prefer the broker-refreshed position list, because the executed size can
    differ from the submitted request volume after min-volume / step rounding.
    """
    actual_api_volume = float(fallback_volume)
    for pos in positions or []:
        current_pid = None
        if hasattr(pos, 'get'):
            current_pid = pos.get('position_id') or pos.get('ticket')
        else:
            current_pid = getattr(pos, 'position_id', None) or getattr(pos, 'ticket', None)
        if current_pid is not None and int(current_pid) == int(position_id):
            return _position_api_volume(pos) or actual_api_volume
    return actual_api_volume


def _track_local_sl_tp(position_id: int, sl: float, tp: float) -> None:
    """Record/amend local SL/TP mirror for a cTrader position_id.

    Thread-safe. Used by live loop after amend_position_sltp() to keep
    a local copy of where the SL/TP currently sit on the server. Useful
    for reconciliation when broker rejects the next amend (e.g. already
    closed): we know what was last pushed.
    """
    if position_id is None or position_id <= 0:
        return
    with _local_positions_lock:
        _local_positions[position_id] = _LocalSLTP(
            position_id=position_id,
            sl=sl,
            tp=tp,
            updated_at=time.time(),
        )

# ── 共享 live state 缓存 (live loop 周期更新, API/WS 只读) ────────────
# audit 2026-06-08: 旧设计每次 WS 推送 / HTTP 轮询都打 broker,
# Twisted reactor 排队导致页面切换卡顿. 新设计: live loop 周期更新
# _live_state 缓存, 所有读取路径都只读缓存, 0 broker 调用.
# audit 2026-06-10: writers MUST replace the whole list / dict (e.g.
# _live_state["positions"] = new_list), NOT mutate in place
# (pos.append(item)). Readers run on different threads (loop tick +
# HTTP handlers in get_account / get_positions / start_loop); in-place
# mutation can race with iteration and yield torn reads.
# live state container + lock are owned by backend.services.live_state_store
# (imported above); live_service keeps the writer-side names bound to the same
# objects.  live_state_set/live_state_update below are the single write path.
_DATA_SYNC_LOCK = threading.Lock()








def _notify_live_state_change() -> None:
    """Wake the event-driven /ws/state projection after a state write."""
    try:
        from backend.ws.manager import get_connection_manager

        get_connection_manager().notify("state")
    except Exception:
        # WebSocket delivery is an observation surface.  Its availability
        # must never affect the live loop or risk/effect state writers.
        return


def _mark_account_reconcile_failed(error: str) -> None:
    live_state_update(
        account_reconcile_failed_at=time.time(),
        account_reconcile_error=str(error or "account_reconcile_failed")[:500],
    )


def _mark_positions_reconcile_failed(error: str) -> None:
    live_state_update(
        positions_reconcile_failed_at=time.time(),
        positions_reconcile_error=str(error or "positions_reconcile_failed")[:500],
    )






def _get_final_open_probe_conn():
    """Open a bounded read-only PG connection for the open-only liveness gate.

    This connection runs on the single live-loop thread, so both connection
    setup and ``SELECT 1`` must fail before the 20-second safety SLO.  Other
    state reads retain their existing transaction semantics.
    """

    from psycopg.conninfo import make_conninfo

    from backend.core.db import state_pg_dsn, state_pg_enabled
    from backend.core.state_store import connect_state_store

    if not state_pg_enabled():
        raise RuntimeError("PostgreSQL state backend is not enabled")
    bounded_dsn = make_conninfo(
        state_pg_dsn(),
        connect_timeout=2,
        options="-c statement_timeout=2000 -c lock_timeout=1000",
    )
    return connect_state_store(bounded_dsn, read_only=True)


























































































































# ── Loss-streak probation ladder (risk/loss_streak.py owns the math) ──

def loss_streak_ladder_facts() -> dict[str, Any]:
    """Assemble the observed facts the ladder needs, from live state.

    Session timestamps come from the broker schedule projection already
    published by market_session (single session authority); the review
    statement flag is written by the learning loop's forced review step.
    """
    book = dict(live_state_get("loss_streak_book", {}, clone=True) or {})
    if not book:
        return {}
    session = live_state_get("market_session", {}, clone=True) or {}
    now_ts = time.time()
    seconds_to_open = session.get("seconds_to_open")
    seconds_to_close = session.get("seconds_to_close")
    is_open = bool(session.get("is_open", False))
    next_open = (
        now_ts + float(seconds_to_open)
        if seconds_to_open is not None and float(seconds_to_open) >= 0.0
        else 0.0
    )
    day_end = (
        now_ts + float(seconds_to_close)
        if seconds_to_close is not None and float(seconds_to_close) >= 0.0
        else 0.0
    )
    # When the market is open the current session end IS the day-end anchor.
    if is_open and day_end <= 0.0:
        day_end = next_open
    return {
        "now_ts": now_ts,
        "tripped_at": float(book.get("tripped_at") or 0.0),
        # 盘中触发时锁到当前时段结束(day_end),否则锁到下一时段开盘 —
        # 置 0 会让 evaluate_ladder 的会话锁在唯一可交易的窗口内失效。
        "next_session_open_ts": next_open if not is_open else day_end,
        "broker_day_end_ts": day_end,
        "probation_pnl": float(book.get("probation_pnl", 0.0) or 0.0),
        "probation_trade_count": int(book.get("probation_trade_count", 0) or 0),
        "review_statement_ready": bool(
            _loss_streak_review_ready(book, now_ts=now_ts)
        ),
        "consecutive_tripped_days": int(
            book.get("consecutive_tripped_days", 1) or 1
        ),
    }


def _loss_streak_review_ready(book: dict[str, Any], *, now_ts: float) -> bool:
    """Statement is ready when the learning loop produced one for this trip
    date, or when the statement grace window (90 min) has elapsed — the lock
    must never depend on a downstream process staying healthy (fallback to
    the legacy next-session unlock)."""
    if bool(book.get("review_statement_ready", False)):
        return True
    tripped_at = float(book.get("tripped_at") or 0.0)
    if tripped_at <= 0.0:
        return False
    trip_date = str(book.get("trip_date") or "")
    try:
        from backend.services.loss_streak_review import (
            load_loss_review_statement,
            statement_grace_seconds,
        )

        if now_ts - tripped_at >= statement_grace_seconds():
            return True
        statement = load_loss_review_statement(
            trip_date=trip_date, kv_reader=live_close_settlement.runtime_kv_get
        )
        return statement is not None
    except Exception:
        # Review pipeline unavailable -> do not hold the lock hostage.
        grace = 5400.0
        return now_ts - tripped_at >= grace


def _maybe_update_loss_streak_book(*, tripped: bool, reason: str = "") -> None:
    """Track the daily-loss trip and reset the ladder on broker-day rollover."""
    book = dict(live_state_get("loss_streak_book", {}, clone=True) or {})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if tripped:
        if not book or str(book.get("trip_date") or "") != today:
            prev_streak = int(book.get("consecutive_tripped_days", 0) or 0)
            yesterday = datetime.fromtimestamp(
                time.time() - 86400.0, timezone.utc
            ).strftime("%Y-%m-%d")
            streak = prev_streak + 1 if str(
                book.get("last_trip_date") or ""
            ) == yesterday else 1
            book = {
                "schema_version": "loss_streak_book.v1",
                "trip_date": today,
                "tripped_at": time.time(),
                "reason": str(reason or ""),
                "last_trip_date": today,
                "consecutive_tripped_days": streak,
                "probation_pnl": 0.0,
                "probation_trade_count": 0,
                "review_statement_ready": False,
            }
            live_state_update(loss_streak_book=book)
            logger.info(
                "[loss_streak] daily limit tripped: streak={} reason={}",
                streak,
                reason,
            )
    elif book and str(book.get("trip_date") or "") != today:
        # Broker day rolled over without a new trip: clear the ladder so a
        # fresh day starts unconditionally clean (legacy behaviour).
        live_state_update(loss_streak_book={})


def _record_probation_trade_outcome(pnl: float, *, position_id: int = 0) -> None:
    """Book one closed position into the probation ledger when it is armed."""
    with _LIVE_STATE_LOCK:
        book = dict(_live_state.get("loss_streak_book", {}) or {})
        if not book:
            return
        ids = list(book.get("probation_position_ids", []) or [])
        if position_id and int(position_id) in ids:
            return  # 同一仓位只记一次 — 会话重建可能重复回调
        book["probation_pnl"] = float(
            book.get("probation_pnl", 0.0) or 0.0
        ) + float(pnl or 0.0)
        if position_id:
            ids.append(int(position_id))
            book["probation_position_ids"] = ids[-50:]
        else:
            book["probation_trade_count"] = int(
                book.get("probation_trade_count", 0) or 0
            ) + 1
        _live_state["loss_streak_book"] = book


def _mark_loss_review_statement_ready(statement: dict[str, Any]) -> None:
    """Learning loop hook: record the forced loss-review statement."""
    with _LIVE_STATE_LOCK:
        book = dict(_live_state.get("loss_streak_book", {}) or {})
        if not book:
            return
        book["review_statement_ready"] = True
        book["review_statement"] = {
            "action": str(statement.get("action") or "unknown"),
            "summary": str(statement.get("summary") or "")[:500],
            "produced_at": time.time(),
        }
        _live_state["loss_streak_book"] = book


def _get_risk_state() -> dict:
    return live_state_get("risk", {}, clone=True) or {}


def _set_factor_snapshot(votes: dict, composite: dict) -> None:
    live_state_update(last_factor_votes=votes, last_composite=composite)


def _set_loop_diagnostic(tick: int, bridge_status: str | None = None, *, bridge_ready: bool | None = None) -> None:
    """Record loop phase without confusing tick start with tick completion.

    ``ts`` is the public loop liveness observation and therefore advances only
    after the serial tick has completed.  Phase updates are useful diagnostics
    but must not keep ``live.loop.v2`` green while a broker/RPC call is stuck.
    """
    previous = live_state_get("_diag", {}, clone=True) or {}
    now = time.time()
    snapshot = {
        "tick": tick,
        "ts": float(previous.get("ts") or 0.0),
        "bridge": bridge_status or previous.get("bridge", ""),
        "last_error": previous.get("last_error", ""),
        "phase": bridge_status or previous.get("phase", ""),
        "phase_at": now,
        "current_tick": tick,
        "last_completed_at": float(previous.get("last_completed_at") or 0.0),
        "last_completed_tick": int(previous.get("last_completed_tick") or 0),
    }
    if bridge_status == "checking":
        snapshot["started_at"] = now
    elif bridge_status is None:
        snapshot["ts"] = now
        snapshot["last_completed_at"] = now
        snapshot["last_completed_tick"] = tick
        snapshot["phase"] = "completed"
    if bridge_ready is not None:
        snapshot["bridge_ready"] = bridge_ready
    elif "bridge_ready" in previous:
        snapshot["bridge_ready"] = previous["bridge_ready"]
    live_state_set("_diag", snapshot)


def _prime_live_loop_state(
    *,
    broker: str,
    strategy_name: str,
    started_at: float,
    account: dict,
    accepting_new_risk: bool = True,
    restore_session: bool = True,
    account_observed: bool = True,
) -> None:
    """Initialize process ownership without manufacturing broker facts.

    ``account`` is a compatibility/startup projection only.  A caller-provided
    dict (including the historical zero placeholder) is recorded on the event
    plane and cannot advance the account reconcile clock or authorize risk.
    Existing reconciled values remain visible with their original observation
    time until the new generation completes a fresh reconcile.
    """
    account_payload = dict(account or {})
    if not account_observed:
        account_payload.update(ok=False, warming_up=True)
    live_state_update(
        broker=broker,
        loop_running=True,
        loop_strategy=strategy_name,
        loop_started_at=started_at,
        loop_shutdown=None,
        # Every generation starts fail-closed.  The broker execution-intent
        # recovery contract is mandatory and reopens risk only after its
        # explicit broker/session gates complete.
        accepting_new_risk=False,
        account_event=account_payload,
        account_event_updated_at=(time.time() if account_observed else None),
        account_event_reason=(
            "startup_projection" if account_observed else "startup_warming"
        ),
        execution_recovery={
            "schema": "broker_execution_intent_recovery.v2",
            "enabled": True,
            "ready": False,
            "unresolved_count": None,
            "status": "pending",
        },
    )
    if restore_session:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        open_position_ids = live_close_settlement.fresh_cached_broker_open_position_ids()
        restored = bool(
            open_position_ids is not None
            and live_close_settlement.restore_session_state_for_day(
                today_str,
                broker_open_position_ids=open_position_ids,
            )
        )
        session_status = str(
            live_state_get("session_state_status", "unknown") or "unknown"
        )
        if not restored or session_status != "available":
            # Preserve the last known risk projection.  A missing/corrupt
            # cache or unavailable PostgreSQL deal stream is an explicit
            # authority failure, never evidence for a zero-risk new day.
            live_state_update(
                session_state_status=(
                    session_status
                    if session_status in {"unavailable", "degraded_cache"}
                    else "unavailable"
                ),
                session_state_source=(
                    live_state_get("session_state_source", "unavailable")
                    if session_status == "degraded_cache"
                    else "unavailable"
                ),
                accepting_new_risk=False,
            )



def _mark_loop_stopped_for_display() -> None:
    _loop_mark_stopped_for_display(state_update=live_state_update)


def schedule_auto_resume_loop(delay_sec: float = _AUTO_RESUME_DELAY_SEC) -> bool:
    desired = live_close_settlement.read_loop_desired_state()
    if not desired or not desired.get("enabled"):
        return False
    if loop_status().get("running"):
        return False

    broker = str(desired.get("broker") or "ctrader")
    strategy_name = str(desired.get("strategy_name") or "factor_v4")

    def _resume():
        time.sleep(max(0.0, delay_sec))
        try:
            latest_desired = live_close_settlement.read_loop_desired_state()
            if not latest_desired or not latest_desired.get("enabled"):
                logger.info("[live] auto-resume cancelled: desired state disabled")
                return
            if loop_status().get("running"):
                logger.info("[live] auto-resume skipped: loop already running")
                return
            result = start_loop(
                broker,
                strategy_name=strategy_name,
                persist_desired=False,
                trigger_reason="auto_resume",
            )
            logger.info("[live] auto-resume attempted: {}", result)
        except Exception as exc:
            logger.warning("[live] auto-resume failed: {}", exc)

    threading.Thread(target=_resume, name="live_loop_auto_resume", daemon=True).start()
    return True

# ── cTrader 缓存 (防 WS 1s 推送反复击中 Twisted reactor)
# audit 2026-06-08: WS read_state_snapshot 每 1s 调 get_account/get_positions,
# 每次都走 get_ctrader → bridge.account_info → _send (Twisted deferred) .
# cTrader Open API 是顺序协议, 同时多个 _send 互等导致延迟/超时.
# 加 5s TTL 缓存, WS 1s 推读缓存, 缓解 reactor 竞争.
import time as _time
_ACCOUNT_CACHE: dict[str, tuple[float, dict]] = {}
_POSITIONS_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 15.0  # 15s 避免 WS 1s 推 + HTTP 5s 轮询同时击中 reactor
_POSITIONS_CACHE_TTL = 3.0  # 持仓/盈亏刷新频率 (含官方 PnL API)
_CACHE_LOCK = threading.Lock()  # 防多个线程同时刷新 (WS + live tick 同时过期)


# ── Status / account / positions ──────────────────────────────────────────

_probe_ctrader_cache: tuple[float, str, str | None] | None = None
_CTRADER_PROBE_TTL = 15.0  # cTrader ping 也有 5s 超时, 按 _ACCOUNT_CACHE 节奏缓存
_BAR_TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"]


def _cache_get_or_refresh(cache: dict, ttl: float, fetcher):
    """读缓存, 过期则调 fetcher 刷新. 带锁防并发刷新. 出错时返旧缓存不抛."""
    return _runtime_cache_get_or_refresh(cache, ttl, fetcher, _CACHE_LOCK)


def _make_ctrader_bridge(**overrides):
    """从进程级有效配置构造 CTraderBridge, 支持测试 kwargs 覆盖.
    返回 (bridge, error_msg | None)."""
    # 确保 .env 的 CTRADER_* 已灌到 os.environ
    try:
        from execution._env import load_env
        load_env()
    except Exception as _e:
        logger.debug("load_env failed (non-critical): {}", _e)
    try:
        from execution.ctrader_bridge import CTraderBridge
    except ImportError as e:
        return None, f"ctrader-open-api not installed: {e}"
    from execution.broker_config import shared_broker_connection_config

    kw = shared_broker_connection_config().bridge_kwargs()
    kw.update(overrides)
    bridge = CTraderBridge(**kw)
    _install_ctrader_live_listener(bridge)
    return bridge, None


def _apply_ctrader_runtime_config(_bridge) -> None:
    """Reserved for non-order runtime bridge settings."""


def _install_ctrader_live_listener(bridge) -> None:
    if bridge is None or getattr(bridge, "_live_service_listener_installed", False):
        return

    def _listener(event_type: str, payload: dict[str, Any]) -> None:
        now_ts = time.time()
        try:
            if event_type == "spot":
                price = float(payload.get("price") or 0.0)
                if price > 0:
                    global _latest_price, _latest_price_updated_at
                    _latest_price = price
                    _latest_price_updated_at = float(payload.get("ts") or now_ts)
                    previous_quote = live_state_get("spot_quote", None, clone=True) or {}
                    previous_changed_at = float(live_state_get("spot_quote_changed_at", 0.0) or 0.0)
                    bid = float(payload.get("bid") or 0.0)
                    ask = float(payload.get("ask") or 0.0)
                    previous_values = (
                        float((previous_quote or {}).get("bid") or 0.0),
                        float((previous_quote or {}).get("ask") or 0.0),
                        float((previous_quote or {}).get("mid") or 0.0),
                    )
                    current_values = (bid, ask, price)
                    quote_changed = bool(
                        previous_quote
                        and any(abs(current_values[idx] - previous_values[idx]) > 1e-9 for idx in range(3))
                    )
                    quote_changed_at = float(payload.get("ts") or now_ts) if quote_changed else previous_changed_at
                    quote = {
                        "bid": bid,
                        "ask": ask,
                        "mid": price,
                        "ts": float(payload.get("ts") or now_ts),
                        "changed_at": quote_changed_at,
                        "source": "ctrader_spot",
                    }
                    # Spot/event projections are useful for display, but they
                    # are not a full broker position reconciliation.  Patch a
                    # dedicated event view and leave the authoritative
                    # position snapshot/timestamp untouched.
                    positions = (
                        live_state_get("positions_event", [], clone=True)
                        or live_state_get("positions_reconciled", [], clone=True)
                        or []
                    )
                    patched_positions = []
                    for item in positions:
                        if isinstance(item, dict):
                            item = dict(item)
                            item["current_price"] = price
                            patched_positions.append(item)
                        else:
                            patched_positions.append(item)
                    live_state_update(
                        spot_price=price,
                        spot_quote=quote,
                        spot_quote_changed_at=quote_changed_at,
                        positions_event=patched_positions or positions,
                        positions_event_updated_at=float(payload.get("ts") or now_ts),
                        positions_event_reason="spot_price_patch",
                    )
                return
            if event_type == "account":
                account = payload.get("account")
                if account is None:
                    return
                if not isinstance(account, dict):
                    account = asdict(account)
                account.setdefault("ok", True)
                account.setdefault("broker", "ctrader")
                live_state_update(
                    account_event=account,
                    account_event_updated_at=now_ts,
                    account_event_reason=str(payload.get("reason") or "account_event"),
                )
                return
            if event_type == "positions":
                positions = payload.get("positions") or []
                try:
                    from config.runtime_config import shared as _rc

                    cfg = _rc()
                except Exception:
                    cfg = None
                enriched = _enrich_positions_with_path_metrics(
                    positions,
                    cfg=cfg,
                    now_ts=now_ts,
                    persist=False,
                    broker="ctrader",
                    strategy_name=_current_loop_strategy_name(),
                )
                live_state_update(
                    positions_event=enriched,
                    positions_event_updated_at=now_ts,
                    positions_event_reason=str(payload.get("reason") or "positions_event"),
                )
                return
        except Exception as exc:
            logger.debug("[ctrader] live listener ignored {}: {}", event_type, exc)

    bridge.add_event_listener(_listener)
    setattr(bridge, "_live_service_listener_installed", True)


# ── cTrader 连接管理 ──────────────────────────────────────────────
# Twisted reactor 是全局单例, 不能 stop/restart. 每次 create+connect+destroy
# bridge 会导致 reactor 状态污染 (旧 protocol 残留).
# 方案: 进程级长连接 bridge, 所有 cTrader API 复用同一个连接.
# audit 2026-06-10: connect() 之前是同步阻塞 (reactor.startService 等回包 +
# 3 次 _send 每次 10s, 总 5-50s), 切 cTrader broker 占满 FastAPI 线程池 40 线程
# 之一, 全部其它 API 排队. 改造: get_ctrader() 非阻塞 — 首次启动后台线程做
# 真 connect, 立刻返 (bridge, None, warming_up=True); 后续调用查 is_connected
# 属性(瞬时), 连好了返 warming_up=False, 没好返 warming_up=True.
_CTRADER_RUNTIME = CTraderRuntime(
    lock_path=Path(__file__).resolve().parent.parent.parent / "runtime" / "ctrader_session.lock",
)


def get_ctrader():
    """返回进程级长连接 CTraderBridge (非阻塞版, audit 2026-06-10).

    Returns:
        (bridge, error_msg | None, warming_up: bool)
        warming_up=True 表示后台 connect 还没好 — 调用方应返 warming_up 缓存,
        不要阻塞等连接 (e.g. `{"ok": True, "warming_up": True}`).
        warming_up=False + bridge 不为 None → 可直接用.
        error_msg 不为 None → 启动失败 (无 token / 库未装), 重试也没用.
    """
    try:
        from execution._env import load_env
        load_env()
    except Exception as _e:
        logger.debug("load_env failed (non-critical): {}", _e)
    try:
        from execution.ctrader_bridge import CTraderBridge
    except ImportError as e:
        return None, f"ctrader-open-api not installed: {e}", False

    result = _CTRADER_RUNTIME.get_or_start(
        make_bridge=_make_ctrader_bridge,
        should_send_orders=_should_send_orders,
        apply_runtime_config=_apply_ctrader_runtime_config,
        logger=logger,
    )
    return result


def warmup_ctrader(timeout_sec: float = 0.0) -> None:
    """在 lifespan 启动时调 — 后台预热 cTrader 连接, 用户切 Live tab 时不卡.
    timeout_sec=0 立即返回 (后台线程继续); >0 则同步等最多 timeout_sec 秒."""
    bridge, err, warming = get_ctrader()
    if err:
        logger.info(f"[ctrader] warmup skipped: {err}")
        return
    if not warming:
        return  # 已经连好了 (再次调用)
    if timeout_sec <= 0:
        logger.info("[ctrader] warmup launched in background, will be ready by user's first Live tab click")
        return
    # 同步等 (用于 main 进程 fork 之前 etc.)
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if bridge.is_connected:
            logger.info(f"[ctrader] warmup connected in {time.time()-t0:.1f}s")
            return
        time.sleep(0.2)


_last_spot_subscription_attempt_ts: float = 0.0
# Internal market-context cache tolerance only. Public spot facts and final
# open admission use the 20-second contract; this longer window must never be
# used to label the UI quote as realtime or authorize a new open.
_SPOT_QUOTE_STALE_SECONDS = 300.0


def _quote_age_seconds(quote: dict | None, *, now_ts: float | None = None) -> float | None:
    if not quote:
        return None
    ts = float((quote or {}).get("ts") or 0.0)
    if ts <= 0:
        return None
    return max(0.0, float(now_ts or time.time()) - ts)


def _quote_is_fresh(quote: dict | None, *, now_ts: float | None = None) -> bool:
    age = _quote_age_seconds(quote, now_ts=now_ts)
    return age is not None and age <= _SPOT_QUOTE_STALE_SECONDS


def market_session_snapshot(bridge=None, *, broker_error: str = "") -> dict[str, Any]:
    quote = {}
    now_ts = time.time()
    if bridge is not None and hasattr(bridge, "get_spot_quote"):
        try:
            quote = bridge.get_spot_quote() or {}
        except Exception:
            quote = {}
    if not quote:
        stored_quote = live_state_get("spot_quote", None, clone=True)
        if isinstance(stored_quote, dict):
            quote = stored_quote
    quote_changed_at = float((quote or {}).get("changed_at") or live_state_get("spot_quote_changed_at", 0.0) or 0.0)
    if quote:
        quote = {**quote, "changed_at": quote_changed_at}
    positions = live_state_get("positions_reconciled", [], clone=True) or []
    if isinstance(positions, dict):
        positions = positions.get("positions", []) or []
    account_updated_at = float(live_state_get("account_updated_at", 0.0) or 0.0)
    positions_updated_at = float(live_state_get("positions_updated_at", 0.0) or 0.0)
    account_api_ok = bool(account_updated_at > 0 and now_ts - account_updated_at <= 180.0)
    positions_api_ok = bool(positions_updated_at > 0 and now_ts - positions_updated_at <= 180.0)
    broker_connected = bool(getattr(bridge, "is_connected", False)) if bridge is not None else None
    latest_market_data_ts = 0.0
    try:
        try:
            from config.runtime_config import shared as _runtime_cfg

            active_timeframe = str(
                getattr(_runtime_cfg(), "timeframe", "M5") or "M5"
            )
        except Exception:
            active_timeframe = "M5"
        if bridge is not None and hasattr(bridge, "get_live_bars"):
            online_frame = bridge.get_live_bars(
                timeframe=active_timeframe,
                n_bars=1,
            )
            latest_market_data_ts = live_bar_warmup.df_latest_epoch(online_frame)
    except Exception:
        latest_market_data_ts = 0.0
    if latest_market_data_ts <= 0.0:
        try:
            # The durable replica remains a low-frequency session fallback;
            # it is no longer the live market-data authority.
            from data.live_sync.health import SyncHealth

            bar_ts_by_tf = dict((SyncHealth.shared().record.last_bar_ts_by_tf or {}))
            latest_market_data_ts = float(
                bar_ts_by_tf.get("M1") or bar_ts_by_tf.get("M5") or 0.0
            )
        except Exception:
            latest_market_data_ts = 0.0
    symbol_meta = getattr(bridge, "_symbol_meta", None) if bridge is not None else None
    broker_schedule = (symbol_meta or {}).get("broker_schedule") if isinstance(symbol_meta, dict) else None
    state = evaluate_market_session(
        symbol="XAUUSD+",
        now_ts=now_ts,
        latest_quote_ts=float((quote or {}).get("ts") or 0.0),
        latest_quote_change_ts=quote_changed_at,
        latest_market_data_ts=latest_market_data_ts,
        broker_error=broker_error,
        has_open_positions=bool(positions),
        api_available=bool(broker_connected or account_api_ok or positions_api_ok),
        broker_connected=broker_connected,
        account_api_ok=account_api_ok,
        positions_api_ok=positions_api_ok,
        broker_schedule=broker_schedule if isinstance(broker_schedule, dict) else None,
    ).to_dict()
    live_state_update(
        market_session=state,
        spot_quote=quote or live_state_get("spot_quote", None, clone=True),
    )
    try:
        from backend.services.runtime_health_projection import RuntimeHealthProjectionService

        RuntimeHealthProjectionService().publish(
            market_session=state,
            ctrader_connected=broker_connected,
            live_loop_running=bool(live_state_get("loop_running", False)),
            source="live_market_session",
        )
    except Exception as projection_exc:
        logger.debug("[live] runtime health projection publish failed: {}", projection_exc)
    return state


def _ensure_spot_subscription(
    bridge,
    *,
    log=None,
    timeframe: str = "M5",
) -> None:
    """Restore spot and live trendbar streams on a connected bridge.

    A maintenance/open-pending classification must never suppress the
    subscription that can produce the missing quote.
    """
    global _last_spot_subscription_attempt_ts
    if bridge is None or not getattr(bridge, "is_connected", False):
        return
    quote = {}
    if hasattr(bridge, "get_spot_quote"):
        try:
            quote = bridge.get_spot_quote() or {}
        except Exception:
            quote = {}
    now_ts = time.time()
    spot_needed = (
        float((quote or {}).get("ts") or 0.0) <= 0
        or not _quote_is_fresh(quote, now_ts=now_ts)
    )
    live_trendbar_needed = False
    try:
        needs_live_trendbars = getattr(
            bridge,
            "live_trendbars_need_subscription",
            None,
        )
        if callable(needs_live_trendbars):
            live_trendbar_needed = bool(
                needs_live_trendbars((str(timeframe or "M5").upper(),))
            )
    except Exception:
        live_trendbar_needed = True
    if not spot_needed and not live_trendbar_needed:
        return
    if now_ts - _last_spot_subscription_attempt_ts < 60:
        return
    _last_spot_subscription_attempt_ts = now_ts
    try:
        if (spot_needed or live_trendbar_needed) and hasattr(bridge, "subscribe_spots"):
            bridge.subscribe_spots()
        if live_trendbar_needed and hasattr(bridge, "subscribe_live_trendbars"):
            subscribed = bool(
                bridge.subscribe_live_trendbars(
                    (str(timeframe or "M5").upper(),)
                )
            )
            if not subscribed:
                msg = (
                    "live trendbar subscription failed; "
                    "decision-bar freshness remains fail-closed"
                )
            else:
                msg = (
                    "spot/live trendbar subscriptions refreshed after broker "
                    "connection became ready"
                )
        else:
            msg = "spot subscription refreshed after broker connection became ready"
        log(msg) if log else logger.info(msg)
    except Exception as exc:
        logger.debug("[market_session] spot subscription refresh failed: {}", exc)


def wait_ctrader_ready(bridge, timeout_sec: float = 30.0) -> str | None:
    """blocking 等待 bridge 真正连好. 用于 live loop body 这种已知在后台线程
    可以阻塞的场景. Returns error_msg | None."""
    if bridge is None:
        return "no bridge"
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if bridge.is_connected:
            return None
        time.sleep(0.2)
    return f"cTrader connect timeout after {timeout_sec:.0f}s"


# ── Status / account / positions ──────────────────────────────────────────

_probe_ctrader_cache: tuple[float, str, str | None] | None = None
_CTRADER_PROBE_TTL = 15.0  # cTrader ping 也有 5s 超时, 按 _ACCOUNT_CACHE 节奏缓存


def get_status() -> dict:
    """Report current broker connection status (best-effort, no broker call)."""
    ctrader_status, ctrader_error = _probe_ctrader()
    get_latest_price()
    state_snapshot = live_state_snapshot()
    loop = loop_status(_state_snapshot=state_snapshot)
    readiness = get_live_readiness(
        "ctrader",
        _state_snapshot=state_snapshot,
        _loop_snapshot=loop,
    )
    return {
        "ctrader": {"status": ctrader_status, "error": ctrader_error},
        "loop": loop,
        "readiness": readiness,
        "market_session": state_snapshot.get("market_session", {}) or {},
        "spot_quote": state_snapshot.get("spot_quote"),
    }


def _probe_ctrader() -> tuple[str, str | None]:
    global _probe_ctrader_cache
    now = time.time()
    if _probe_ctrader_cache and (now - _probe_ctrader_cache[0]) < _CTRADER_PROBE_TTL:
        return _probe_ctrader_cache[1], _probe_ctrader_cache[2]
    # audit 2026-06-10: get_ctrader 现在返 3-tuple; warming_up 不算 error
    bridge, err, warming = get_ctrader()
    if err:
        result = ("error", err) if "not installed" in err else \
                 ("no_token", err) if "no cTrader credentials" in err else \
                 ("disconnected", err)
        _probe_ctrader_cache = (now, result[0], result[1])
        return result
    if warming or not bridge.is_connected:
        # audit 2026-06-10: 后台 connect 进行中, 标 warming_up, 不当 error
        _probe_ctrader_cache = (now, "warming_up", None)
        return "warming_up", None
    _probe_ctrader_cache = (now, "connected", None)
    return "connected", None


def _coerce_live_positions(raw_positions) -> list[dict]:
    pos_list = raw_positions or []
    if isinstance(pos_list, dict):
        pos_list = pos_list.get("positions", []) or []
    if pos_list and not isinstance(pos_list[0], dict):
        from backend.ws.endpoints import position_to_dict
        pos_list = [position_to_dict(p) for p in pos_list]
    # Broker snapshots are JSON projections.  Copy them at this boundary so a
    # stale/enriched compatibility dict cannot retain a recursive nested
    # reference and poison every readiness/API response built from it.
    return [
        item
        if not isinstance(item, (dict, list, tuple, set, frozenset))
        else _safe_container_snapshot(item)
        for item in list(pos_list or [])
    ]


def get_live_readiness(
    broker: str = "ctrader",
    *,
    _state_snapshot: dict | None = None,
    _loop_snapshot: dict | None = None,
) -> dict:
    state_snapshot = (
        _state_snapshot
        if isinstance(_state_snapshot, dict)
        else live_state_snapshot()
    )
    state = {
        "diag": state_snapshot.get("_diag", {}) or {},
        "account_reconciled": state_snapshot.get("account_reconciled", {}) or {},
        "account_updated_at": state_snapshot.get("account_updated_at", 0.0),
        "positions_reconciled": state_snapshot.get("positions_reconciled", []) or [],
        "positions_updated_at": state_snapshot.get("positions_updated_at", 0.0),
        "account_reconcile_id": state_snapshot.get("account_reconcile_id", ""),
        "positions_reconcile_id": state_snapshot.get("positions_reconcile_id", ""),
        "account_reconcile_failed_at": state_snapshot.get(
            "account_reconcile_failed_at", 0.0
        ),
        "positions_reconcile_failed_at": state_snapshot.get(
            "positions_reconcile_failed_at", 0.0
        ),
        "account_reconcile_error": state_snapshot.get("account_reconcile_error", ""),
        "positions_reconcile_error": state_snapshot.get("positions_reconcile_error", ""),
        "account_event_updated_at": state_snapshot.get("account_event_updated_at", 0.0),
        "positions_event_updated_at": state_snapshot.get("positions_event_updated_at", 0.0),
        "account_event_reason": state_snapshot.get("account_event_reason"),
        "positions_event_reason": state_snapshot.get("positions_event_reason"),
        "positions_component_facts": state_snapshot.get("positions_component_facts", {}) or {},
    }
    positions = _coerce_live_positions(state_snapshot.get("positions_reconciled", []))
    broker_status = "unknown"
    broker_error = None
    if broker == "ctrader":
        broker_status, broker_error = _probe_ctrader()
    return build_live_readiness(
        loop=_loop_snapshot if isinstance(_loop_snapshot, dict) else loop_status(),
        state=state,
        positions=positions,
        checked_at=time.time(),
        # The canonical generation/supervisor path is the only live authority.
        v2_active=True,
        broker_status=broker_status,
        broker_error=broker_error,
    )


def get_account(broker: str) -> dict:
    """Read real broker account info. Returns dict with at minimum
    {ok, broker, balance, equity, margin, leverage, currency, error}.

    audit 2026-06-09: 如果 live loop 在跑这个 broker, 短路返回 _live_state 缓存,
    避免重复打 broker (Twisted reactor callFromThread 会阻塞主线程 50-200ms,
    直接卡前端 HTTP 请求). Loop 自己的 tick 已经每 60s 刷新 _live_state."""
    readiness = get_live_readiness(broker)
    # ── 缓存短路: loop 在跑 → 只读 _live_state ──
    if live_state_get("loop_running") and live_state_get("broker") == broker:
        acct = live_state_get("account_reconciled", clone=True)
        if acct and acct.get("ok"):
            result = dict(acct)
            result["reconcile_status"] = (
                "fresh" if readiness.get("account_ready") else "stale"
            )
            result["readiness"] = readiness
            return result
        # 缓存没准备好 (loop 刚启动或第一次 tick 未完成)
        return {
            "ok": False,
            "broker": broker,
            "warming_up": True,
            "error": "live loop warming up, first tick pending (within 60s)",
            "readiness": readiness,
        }
    if broker == "ctrader":
        def _fetch():
            # audit 2026-06-10: get_ctrader 返 3-tuple, warming_up 短路
            bridge, err, warming = get_ctrader()
            if err:
                return {"ok": False, "broker": "ctrader", "error": err}
            if warming or not bridge.is_connected:
                return {
                    "ok": True,  # 标识 HTTP 200 正常, 前端按 warming_up 渲染
                    "broker": "ctrader",
                    "warming_up": True,
                    "error": "cTrader connecting in background, first account query pending (within 30s)",
                    "readiness": readiness,
                }
            reconcile = _explicit_account_reconcile(bridge)
            if reconcile is None:
                cached = live_state_get("account_reconciled", {}, clone=True) or {}
                cached_at = float(live_state_get("account_updated_at", 0.0) or 0.0)
                cached_id = str(live_state_get("account_reconcile_id", "") or "")
                if cached and cached_at > 0 and cached_id:
                    return {
                        **dict(cached),
                        "ok": True,
                        "broker": "ctrader",
                        "reconcile_status": "failed",
                        "readiness": get_live_readiness("ctrader"),
                    }
                return {
                    "ok": False,
                    "broker": "ctrader",
                    "error": "fresh account reconcile unavailable",
                    "reconcile_status": "failed",
                    "readiness": get_live_readiness("ctrader"),
                }
            info = _reconcile_value(reconcile, "account", None)
            observed_at = float(_reconcile_value(reconcile, "observed_at", 0.0) or 0.0)
            if info is None or observed_at <= 0:
                return {
                    "ok": False,
                    "broker": "ctrader",
                    "error": "fresh account reconcile returned no observation",
                    "reconcile_status": "failed",
                    "readiness": get_live_readiness("ctrader"),
                }
            info_dict = asdict(info) if is_dataclass(info) else dict(info)
            info_dict.setdefault("ok", True)
            info_dict.setdefault("broker", "ctrader")
            info_dict["reconcile_status"] = "fresh"
            # Preserve the broker observation time. HTTP fetch time is not a
            # broker fact and must never rejuvenate an older cache projection.
            live_state_update(
                account=info_dict,
                account_reconciled=copy.deepcopy(info_dict),
                account_updated_at=observed_at,
                account_reconcile_id=str(
                    _reconcile_value(reconcile, "reconcile_id", "") or ""
                ),
                account_reconcile_failed_at=None,
                account_reconcile_error=None,
            )
            return {"ok": True, "broker": "ctrader", **info_dict, "readiness": get_live_readiness("ctrader")}
        try:
            return _cache_get_or_refresh(_ACCOUNT_CACHE, _CACHE_TTL, _fetch)
        except Exception as e:
            return {"ok": False, "broker": "ctrader", "error": f"{type(e).__name__}: {e}"[:300], "readiness": readiness}
    else:
        return {"ok": False, "broker": broker, "error": f"unknown broker: {broker}", "readiness": readiness}


def get_positions(broker: str, symbol: str | None = None) -> dict:
    """Read open positions on the given broker. Returns {ok, broker, positions: [...]}.

    audit 2026-06-09: 同 get_account, live loop 在跑时短路读缓存."""
    # ── 缓存短路: loop 在跑 → 只读 _live_state ──
    readiness = get_live_readiness(broker)
    try:
        from config.runtime_config import shared as _rc

        cfg = _rc()
    except Exception:
        cfg = None

    def _enrich_positions(pos_list: list[Any]) -> list[dict]:
        return _enrich_positions_with_path_metrics(
            pos_list,
            cfg=cfg,
            now_ts=time.time(),
            persist=False,
            broker=broker,
        )

    def _visible_positions(pos_list: list[Any]) -> list[dict]:
        projected = _coerce_live_positions(pos_list)
        # The serial live owner already enriches the authoritative snapshot.
        # HTTP/compatibility reads must project that snapshot only; repeating
        # lifecycle and supervisor evaluation per browser poll creates extra
        # DB work and lets concurrent requests amplify memory/CPU usage.
        loop_projection_ready = bool(
            live_state_get("loop_running")
            and live_state_get("broker") == broker
        )
        cached_projection_ready = bool(
            projected
            and all(
                isinstance(item, dict)
                and (
                    "supervisor" in item
                    or "position_path_metrics_state" in item
                )
                for item in projected
            )
        )
        visible = (
            projected
            if loop_projection_ready or cached_projection_ready
            else _enrich_positions(projected)
        )
        if symbol:
            expected = str(symbol).upper()
            visible = [
                item
                for item in visible
                if str(item.get("symbol") or "").upper() == expected
            ]
        return visible

    if live_state_get("loop_running") and live_state_get("broker") == broker:
        cached_at = float(live_state_get("positions_updated_at", 0.0) or 0.0)
        cached_id = str(live_state_get("positions_reconcile_id", "") or "")
        if cached_at > 0 and cached_id:
            return {
                "ok": True,
                "broker": broker,
                "positions": _visible_positions(readiness["positions"]),
                "warming_up": False,
                "reconcile_status": (
                    "fresh" if readiness.get("positions_ready") else "stale"
                ),
                "readiness": readiness,
            }
        return {
            "ok": True,
            "broker": broker,
            "positions": [],
            "warming_up": True,
            "readiness": readiness,
        }
    if broker == "ctrader":
        # 缓存短路: live loop 在跑 → 只读 _live_state (跟上面 if 分支等价,
        # 保留是为了 cache_fallback 的 robustness — 上层分支没匹配时这里兜底)
        cached_positions = live_state_get("positions_reconciled", clone=True)
        if cached_positions is not None and live_state_get("loop_running"):
            return {"ok": True, "broker": "ctrader", "positions": _visible_positions(cached_positions), "readiness": readiness}
        # 缓存空 fallback
        def _fetch():
            # audit 2026-06-10: get_ctrader 返 3-tuple, warming_up 短路
            bridge, err, warming = get_ctrader()
            if err:
                return {"ok": False, "broker": "ctrader", "error": err, "positions": []}
            if warming or not bridge.is_connected:
                return {
                    "ok": True,
                    "broker": "ctrader",
                    "positions": [],
                    "warming_up": True,
                    "readiness": readiness,
                }
            reconcile = _explicit_position_reconcile(bridge)
            if str(_reconcile_value(reconcile, "status", "failed") or "failed") != "fresh":
                cached_at = float(live_state_get("positions_updated_at", 0.0) or 0.0)
                cached_id = str(live_state_get("positions_reconcile_id", "") or "")
                cached = _coerce_live_positions(
                    live_state_get("positions_reconciled", [], clone=True)
                )
                if cached_at > 0 and cached_id:
                    visible = _visible_positions(cached)
                    return {
                        "ok": True,
                        "broker": "ctrader",
                        "positions": visible,
                        "reconcile_status": "failed",
                        "readiness": get_live_readiness("ctrader"),
                    }
                return {
                    "ok": False,
                    "broker": "ctrader",
                    "error": str(
                        _reconcile_value(reconcile, "error_code", "")
                        or "fresh positions reconcile unavailable"
                    ),
                    "positions": [],
                    "reconcile_status": "failed",
                    "readiness": get_live_readiness("ctrader"),
                }
            positions = _publish_fresh_position_reconcile(
                reconcile,
                broker="ctrader",
                persist=False,
            )
            visible = _visible_positions(positions)
            return {
                "ok": True,
                "broker": "ctrader",
                "positions": visible,
                "reconcile_status": "fresh",
                "readiness": get_live_readiness("ctrader"),
            }
        try:
            return _cache_get_or_refresh(_POSITIONS_CACHE, _POSITIONS_CACHE_TTL, _fetch)
        except Exception as e:
            return {"ok": False, "broker": "ctrader", "error": f"{type(e).__name__}: {e}"[:300], "positions": [], "readiness": readiness}
    else:
        return {"ok": False, "broker": broker, "error": f"unknown broker: {broker}", "positions": [], "readiness": readiness}


# ── Trading loop management (background thread) ─────────────────────────

# The generation controller is the sole live-loop ownership authority.  The
# shared live state below is a read-only API/UI projection, never a second
# source for thread, broker, or strategy identity.
_loop_state_lock = threading.Lock()
_OPEN_TRADE_ADMISSION_LOCK = threading.Lock()
_process_shutdown_requested = False
_LIVE_LOOP_CONTROLLER = LiveLoopController()
# Restart backoff, price-stuck detection, and bar cache.
_MIN_RESTART_INTERVAL = 60  # 最小重启间隔 60s
_BAR_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / ".bar_cache.pkl"
_PRICE_STUCK_WARNED: dict[str, float] = {}  # {(broker,tf): last_price}


def _phase2_feature_flags():
    return shared_static_feature_flags()


def _current_generation_id() -> str:
    current = _LIVE_LOOP_CONTROLLER.current()
    return str(current.generation_id) if current is not None else ""


def _current_loop_strategy_name(default: str = "factor_v4") -> str:
    current = _LIVE_LOOP_CONTROLLER.current()
    if current is None:
        return str(default)
    return str(current.strategy_name or default)
















# ═══════════════════════════════════════════════════════════
# Phase 3: 特征工程自动化
# ═══════════════════════════════════════════════════════════

def _scheduled_feature_engineering():
    from backend.services.learning_research_jobs import run_feature_engineering_job

    return run_feature_engineering_job()


def _env_enabled(name: str, default: str = "1") -> bool:
    value = str(os.getenv(name, default) or "").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _offmarket_high_load_allowed(session: dict[str, Any]) -> tuple[bool, str]:
    from backend.services.learning_research_jobs import offmarket_high_load_allowed

    return offmarket_high_load_allowed(session)


def _scheduled_offmarket_position_quality_lightgbm(
    *,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    from backend.services.learning_research_jobs import run_offmarket_position_quality_job

    session = live_state_get("market_session", {}, clone=True) or {}
    # Let the quality job consume runtime_health_projection.v1 when this
    # process has not published an in-memory session yet.  Recomputing with a
    # None bridge would silently fall back to the static schedule and create
    # a second market-session authority in the learning path.
    return run_offmarket_position_quality_job(
        session=session or None,
        db_path=db_path,
    )


def _scheduled_factor_selection_heartbeat() -> dict[str, Any]:
    """Republish the selection loaded by this live process without config IO."""

    pipeline = _factor_pipeline or {}
    engine = pipeline.get("engine")
    if engine is None:
        return {
            "ok": True,
            "status": "skipped_pipeline_unavailable",
        }
    from alpha.runtime_factor_selection import select_runtime_factors
    from backend.services.runtime_factor_selection_projection import (
        RuntimeFactorSelectionProjectionService,
    )
    from config import runtime_config as _runtime_config_module

    holder = _runtime_config_module.shared_holder()
    if holder.version() <= 0:
        return {
            "ok": False,
            "status": "runtime_config_snapshot_unavailable",
        }
    cfg = holder.get()
    selection = select_runtime_factors(cfg.factor_signal_config)
    current_generation = _LIVE_LOOP_CONTROLLER.current()
    generation_id = (
        str(current_generation.generation_id)
        if current_generation is not None
        else ""
    )
    return RuntimeFactorSelectionProjectionService().publish(
        selection,
        source="live_factor_pipeline_heartbeat",
        live_generation_id=generation_id,
        pipeline_warm=bool(getattr(engine, "is_warm", False)),
    )



def _start_live_scheduler():
    """注册并启动自进化 Scheduler (11 job). 幂等: 已运行时跳过."""
    from backend.runtime.scheduler import InProcessScheduler
    sched = InProcessScheduler()
    if getattr(sched, "_started", False):
        return
    run_heavy_jobs = _env_enabled("QUANT_BACKEND_HEAVY_JOBS", "0")

    # Drawdown attribution: aggregate the trailing trade_review evidence into
    # an account-level diagnosis so a losing session states its cause instead
    # of only freezing new risk.  Lives on the backend (owner of session/live
    # facts); offset from governance/nursery minutes (7,9,12,17,37,39,42,47).
    def _scheduled_drawdown_attribution() -> None:
        try:
            from backend.services.drawdown_attribution import (
                build_drawdown_attribution,
                persist_drawdown_attribution,
            )
            report = build_drawdown_attribution()
            persist_drawdown_attribution(report)
            logger.info(
                "[drawdown_attribution] window trades={} primary={} pnl=${:.2f}",
                int(report.get("trade_count") or 0),
                report.get("primary_responsibility", "?"),
                float(report.get("total_pnl") or 0.0),
            )
        except Exception as exc:
            logger.warning("[drawdown_attribution] run failed: {}", exc)

    sched.add_job(
        "drawdown_attribution",
        "16,46 * * * *",
        _scheduled_drawdown_attribution,
    )

    # S2.2: EvolutionKernel 已移除（它只注册 system_health，且 run_heavy_jobs=0 时
    # 从不实例化）。system_health 统一在此注册；heavy jobs 由 quant-learning-worker 独占。
    try:
        from monitor.system_health import shared as _sh_shared
        from monitor.alerter import Alerter

        _sys_health = _sh_shared()
        _sys_health.set_alerter(Alerter({
            "log_file": "logs/alerts.log",
            "min_level": "WARNING",
        }).send)
        sched.add_job("system_health", "* * * * *", _sys_health.run)
        # D13: 关键持久化失败统一走同一 Alerter (多通道)。
        from monitor.persistence_alerts import register_alerter

        register_alerter(Alerter({
            "log_file": "logs/alerts.log",
            "min_level": "WARNING",
        }).send)
    except Exception as e:
        logger.warning("[live] system_health registration failed: {}", e)

    if run_heavy_jobs:
        logger.info("[live] heavy scheduler jobs enabled in backend (QUANT_BACKEND_HEAVY_JOBS=1)")
    else:
        logger.info("[live] heavy jobs delegated to learning worker; set QUANT_BACKEND_HEAVY_JOBS=1 to run them in backend")

    sched.add_job(
        "data_sync",
        _DATA_SYNC_CRON,
        _make_data_sync_job(
            lock=_DATA_SYNC_LOCK,
            logger=logger,
            get_ctrader=get_ctrader,
            market_session_snapshot=market_session_snapshot,
        ),
    )
    _register_factor_selection_heartbeat_job(
        sched,
        heartbeat=_scheduled_factor_selection_heartbeat,
    )
    _register_external_sync_jobs(
        sched,
        repo_root=Path(__file__).resolve().parent.parent.parent,
        logger=logger,
    )
    _register_backend_readiness_refresh_job(sched, logger=logger)
    if run_heavy_jobs:
        # Phase 3: 特征工程 (03:05 UTC, 避开 :00 治理和 :02 evolution)
        sched.add_job("feature_eng", "5 3 * * *", _scheduled_feature_engineering)
        # Phase F1.1: 停盘确认窗口 LightGBM 旁路训练 (每小时检查, 非窗口只写 skip 审计)
        from backend.services.evolution_work_coordinator import coordinated_job

        sched.add_job(
            "offmarket_position_quality_lightgbm",
            "20 * * * *",
            coordinated_job(
                "offmarket_position_quality_lightgbm",
                _scheduled_offmarket_position_quality_lightgbm,
            ),
        )
    else:
        logger.info("[live] heavy jobs delegated; set QUANT_BACKEND_HEAVY_JOBS=1 to run them in backend")
    sched.start()
    logger.info("[live] InProcessScheduler started; heavy_jobs={}", run_heavy_jobs)

    _start_scheduler_catch_up(
        sched,
        run_heavy_jobs=run_heavy_jobs,
        logger=logger,
    )


def stop_live_scheduler():
    """停止 Scheduler. 幂等. wait=False 避免阻塞."""
    from backend.runtime.scheduler import InProcessScheduler
    sched = InProcessScheduler()
    try:
        sched.stop(wait=False)
        logger.info("[live] InProcessScheduler stopped")
    except Exception as e:
        logger.debug("[live] scheduler stop: {}", e)


def loop_status(*, _state_snapshot: dict | None = None) -> dict:
    """Return the canonical generation status and read-only live projections."""
    state_snapshot = (
        _state_snapshot
        if isinstance(_state_snapshot, dict)
        else live_state_snapshot()
    )
    with _loop_state_lock:
        generation = _LIVE_LOOP_CONTROLLER.status()
        identity = _loop_identity_snapshot(
            generation=generation,
        )
        freshness = evaluate_safety_freshness(
            live_safety_watchdog.live_safety_watchdog_probe(),
            now=time.time(),
            stale_after_sec=_LIVE_SAFETY_FRESHNESS_SEC,
        )
        local_blockers: list[str] = []
        if freshness.enabled and freshness.running and not freshness.ok:
            local_blockers.extend(freshness.blockers)
        if no_new_risk_latched(fail_closed=True):
            local_blockers.append("no_new_risk_latched")
        safety_payload = state_snapshot.get("safety_plane", {}) or {}
        if isinstance(safety_payload, dict):
            local_blockers.extend(
                str(item)
                for item in (safety_payload.get("blockers") or [])
                if str(item)
            )
            if (
                safety_payload.get("accepting_new_risk") is False
                and not safety_payload.get("blockers")
            ):
                local_blockers.append("safety_not_accepting_new_risk")
        if bool(generation.get("thread_alive")):
            reconcile_blockers = live_open_pipeline.new_risk_reconciliation_blockers()
            local_blockers.extend(reconcile_blockers)
            session_status = str(
                state_snapshot.get("session_state_status", "unknown") or "unknown"
            )
            if session_status != "available":
                local_blockers.append(f"session_state_{session_status}")
            if bool(state_snapshot.get("circuit_breaker", False)):
                local_blockers.append("session_circuit_breaker")
            market_session = state_snapshot.get("market_session", {}) or {}
            if isinstance(market_session, dict) and (
                "can_open_positions" in market_session
                and not bool(market_session.get("can_open_positions"))
            ):
                local_blockers.append(
                    "market_session_blocks_open"
                )
        current = _LIVE_LOOP_CONTROLLER.current()
        if current is not None:
            try:
                _LIVE_LOOP_CONTROLLER.update_runtime_health(
                    current.generation_id,
                    blockers=tuple(local_blockers),
                )
                generation = _LIVE_LOOP_CONTROLLER.status()
                live_state_update(
                    accepting_new_risk=_LIVE_LOOP_CONTROLLER.accepting_new_risk(
                        current.generation_id
                    )
                )
            except RuntimeError:
                pass
        elif local_blockers:
            generation = {
                **generation,
                "accepting_new_risk": False,
                "blockers": sorted(
                    set(generation.get("blockers") or ()) | set(local_blockers)
                ),
            }
        return {
            **identity,
            **generation,
            "running": bool(generation["thread_alive"] and generation["phase"] != "stopped"),
            "safety": state_snapshot.get("safety_plane", {}) or {},
            "safety_authority": "governed_supervisor_executor",
            "safety_heartbeat_state": freshness.state,
            "safety_freshness": freshness.to_dict(),
            "safety_shadow_gate": safety_shadow_gate_status(),
        }

def _live_loop_start_runtime() -> LiveLoopStartRuntime:
    return LiveLoopStartRuntime(
        state_lock=_loop_state_lock,
        process_shutdown_requested=lambda: _process_shutdown_requested,
        controller=_LIVE_LOOP_CONTROLLER,
        last_loop_end=_LIVE_LOOP_CONTROLLER.last_exit_at,
        now=time.time,
        sleep=time.sleep,
        logger_warning=logger.warning,
        logger_info=logger.info,
        persist_desired_state=live_close_settlement.persist_loop_desired_state,
        prime_live_loop_state=_prime_live_loop_state,
        start_safety_watchdog=live_safety_watchdog.start_live_safety_watchdog,
        start_scheduler=_start_live_scheduler,
        stop_scheduler=stop_live_scheduler,
        stop_safety_watchdog=live_safety_watchdog.stop_live_safety_watchdog,
        thread_factory=threading.Thread,
        loop_target=_run_loop,
        live_state_update=live_state_update,
    )


def start_loop(
    broker: str,
    strategy_name: str = "v1_minimal_ma_cross",
    *,
    persist_desired: bool = True,
    trigger_reason: str = "manual",
) -> dict:
    return _runtime_start_live_loop(
        broker,
        strategy_name,
        persist_desired=persist_desired,
        trigger_reason=trigger_reason,
        runtime=_live_loop_start_runtime(),
    )


def stop_loop_for_process_shutdown(timeout_sec: float = 30.0) -> dict[str, Any]:
    """Synchronously drain the live loop during backend process shutdown.

    This process-lifecycle path deliberately preserves the persisted desired
    state.  It does not stop schedulers, disconnect cTrader, or alter broker
    positions.  The current tick is allowed to finish before the loop exits.
    """
    global _process_shutdown_requested

    requested_at = time.time()
    timeout = max(0.0, float(timeout_sec))
    trigger_reason = "backend_shutdown"

    with _loop_state_lock:
        controller_status = _LIVE_LOOP_CONTROLLER.status()
        generation = _LIVE_LOOP_CONTROLLER.current()
        ownership = _LIVE_LOOP_CONTROLLER.ownership_snapshot()
        thread = ownership.thread
        broker = ownership.broker
        thread_id = getattr(thread, "ident", None) if thread is not None else None
        active = bool(
            generation is not None
            and (
                controller_status.get("thread_alive")
                or controller_status.get("phase")
                in {"starting", "running", "degraded", "draining"}
            )
        )
        if not active:
            with _OPEN_TRADE_ADMISSION_LOCK:
                _process_shutdown_requested = True
            ownership_released = True
            if generation is not None and thread is not None:
                ownership_released = _LIVE_LOOP_CONTROLLER.clear_thread_if(
                    generation.generation_id,
                    thread,
                    time.time(),
                )
            if ownership_released:
                _mark_loop_stopped_for_display()
            finished_at = time.time()
            result = {
                "schema_version": "live_loop_process_shutdown.v1",
                "status": "not_running",
                "ok": True,
                "graceful": True,
                "recovery_required": False,
                "was_running": False,
                "desired_state_preserved": True,
                "ownership_released": ownership_released,
                "replacement_detected": not ownership_released,
                "accepting_new_risk": False,
                "broker": broker,
                "thread_id": thread_id,
                "timeout_sec": timeout,
                "requested_at": requested_at,
                "ts": finished_at,
                "trigger_reason": trigger_reason,
            }
        else:
            assert generation is not None
            stop_flag = generation.stop_event
            _LIVE_LOOP_CONTROLLER.request_stop(generation.generation_id)
            draining = {
                "schema_version": "live_loop_process_shutdown.v1",
                "status": "draining",
                "ok": True,
                "graceful": False,
                "recovery_required": False,
                "was_running": True,
                "desired_state_preserved": True,
                "ownership_released": False,
                "replacement_detected": False,
                "accepting_new_risk": False,
                "broker": broker,
                "thread_id": thread_id,
                "timeout_sec": timeout,
                "requested_at": requested_at,
                "trigger_reason": trigger_reason,
                "generation": _current_generation_id(),
                "phase": "draining",
            }
            # Linearize process draining against the final open-order admission
            # check and market RPC.  An RPC admitted before this lock completes;
            # an RPC arriving afterwards observes the latch/event and is blocked.
            with _OPEN_TRADE_ADMISSION_LOCK:
                _process_shutdown_requested = True
                live_state_update(
                    loop_shutdown=draining,
                    accepting_new_risk=False,
                )
                if stop_flag is not None:
                    stop_flag.set()
            result = None

    if result is not None:
        live_state_update(loop_shutdown=result, accepting_new_risk=False)
        live_close_settlement.runtime_kv_set(_RUNTIME_KV_LAST_SHUTDOWN, result)
        logger.info("[live] process shutdown: no running live loop")
        return result

    if thread is None:
        # A generation can be in the start barrier before its worker thread is
        # bound.  Shutdown owns that generation too; acknowledge it here so a
        # later request cannot observe a phantom start.
        assert generation is not None
        _LIVE_LOOP_CONTROLLER.acknowledge_exit(generation.generation_id)
        finished_at = time.time()
        ownership_released = True
        with _loop_state_lock:
            _mark_loop_stopped_for_display()
        result = {
            "schema_version": "live_loop_process_shutdown.v1",
            "status": "completed",
            "ok": True,
            "graceful": True,
            "recovery_required": False,
            "was_running": True,
            "desired_state_preserved": True,
            "ownership_released": True,
            "replacement_detected": False,
            "accepting_new_risk": False,
            "broker": broker,
            "thread_id": thread_id,
            "timeout_sec": timeout,
            "requested_at": requested_at,
            "ts": finished_at,
            "trigger_reason": trigger_reason,
        }
        live_state_update(loop_shutdown=result, accepting_new_risk=False)
        live_close_settlement.runtime_kv_set(_RUNTIME_KV_LAST_SHUTDOWN, result)
        return result

    thread.join(timeout=timeout)
    finished_at = time.time()
    timed_out = thread.is_alive()

    if timed_out:
        result = {
            "schema_version": "live_loop_process_shutdown.v1",
            "status": "timed_out",
            "ok": False,
            "graceful": False,
            "recovery_required": True,
            "was_running": True,
            "desired_state_preserved": True,
            "ownership_released": False,
            "replacement_detected": False,
            "accepting_new_risk": False,
            "broker": broker,
            "thread_id": thread_id,
            "timeout_sec": timeout,
            "requested_at": requested_at,
            "ts": finished_at,
            "trigger_reason": trigger_reason,
        }
        live_state_update(loop_shutdown=result, accepting_new_risk=False)
        live_close_settlement.runtime_kv_set(_RUNTIME_KV_LAST_SHUTDOWN, result)
        logger.warning(
            f"[live] process shutdown timed out after {timeout:.1f}s; "
            "live loop recovery required"
        )
        return result

    with _loop_state_lock:
        current_after = _LIVE_LOOP_CONTROLLER.current()
        if (
            current_after is not None
            and generation is not None
            and current_after.generation_id == generation.generation_id
            and current_after.state not in {"stopped", "failed"}
        ):
            _LIVE_LOOP_CONTROLLER.acknowledge_exit(generation.generation_id)
        ownership_released = bool(
            generation is not None
            and _LIVE_LOOP_CONTROLLER.clear_thread_if(
                generation.generation_id,
                thread,
                finished_at,
            )
        )
        if ownership_released:
            _mark_loop_stopped_for_display()

    result = {
        "schema_version": "live_loop_process_shutdown.v1",
        "status": "completed",
        "ok": True,
        "graceful": True,
        "recovery_required": False,
        "was_running": True,
        "desired_state_preserved": True,
        "ownership_released": ownership_released,
        "replacement_detected": not ownership_released,
        "accepting_new_risk": False,
        "broker": broker,
        "thread_id": thread_id,
        "timeout_sec": timeout,
        "requested_at": requested_at,
        "ts": finished_at,
        "trigger_reason": trigger_reason,
    }
    live_state_update(loop_shutdown=result, accepting_new_risk=False)
    live_close_settlement.runtime_kv_set(_RUNTIME_KV_LAST_SHUTDOWN, result)
    logger.info(
        f"[live] process shutdown completed; ownership_released={ownership_released}"
    )
    return result


def _live_loop_stop_runtime() -> LiveLoopStopRuntime:
    return LiveLoopStopRuntime(
        state_lock=_loop_state_lock,
        controller=_LIVE_LOOP_CONTROLLER,
        admission_lock=_OPEN_TRADE_ADMISSION_LOCK,
        live_state_update=live_state_update,
        persist_desired_state=live_close_settlement.persist_loop_desired_state,
        runtime_kv_set=live_close_settlement.runtime_kv_set,
        last_shutdown_key=_RUNTIME_KV_LAST_SHUTDOWN,
        now=time.time,
        thread_factory=threading.Thread,
        persist_safety_fail_closed=live_safety_watchdog.persist_safety_fail_closed,
        logger_info=logger.info,
    )


def stop_loop(
    *,
    persist_desired: bool = True,
    trigger_reason: str = "manual",
) -> dict:
    return _runtime_stop_live_loop(
        persist_desired=persist_desired,
        trigger_reason=trigger_reason,
        runtime=_live_loop_stop_runtime(),
    )







def get_live_bars(
    symbol: str = "XAUUSD+",
    timeframe: str = "M5",
    n_bars: int = 500,
) -> "pd.DataFrame | None":
    """Read the in-memory cTrader trendbar feed without touching DuckDB."""
    try:
        bridge, error, warming = get_ctrader()
    except Exception as exc:
        logger.debug("online trendbar bridge lookup failed: {}", exc)
        return None
    if error or warming or bridge is None:
        return None
    getter = getattr(bridge, "get_live_bars", None)
    if not callable(getter):
        return None
    try:
        return getter(timeframe=str(timeframe or "M5"), n_bars=int(n_bars or 1))
    except Exception as exc:
        logger.debug(
            "online trendbar read failed: symbol={} timeframe={} error={}",
            symbol,
            timeframe,
            exc,
        )
        return None














# ★ v9-fix: 备份 bar 缓存 (防 DB 空/broker 无数据时死机)




def _publish_fresh_position_reconcile(
    result: Any,
    *,
    broker: str,
    persist: bool = True,
    bridge: Any | None = None,
) -> list[dict[str, Any]]:
    if str(_reconcile_value(result, "status", "failed") or "failed") != "fresh":
        return []
    reconcile_id = str(_reconcile_value(result, "reconcile_id", "") or "")
    positions = _coerce_live_positions(_reconcile_value(result, "positions", ()) or ())
    observed_at = float(_reconcile_value(result, "observed_at", 0.0) or 0.0)
    if not reconcile_id or not _fresh_observation_timestamp(observed_at):
        return []
    raw_components = _reconcile_value(result, "components", {}) or {}
    component_facts: dict[str, dict[str, Any]] = {}
    if hasattr(raw_components, "items"):
        for name, fact in raw_components.items():
            if fact is None:
                continue
            if is_dataclass(fact):
                payload = asdict(fact)
            elif isinstance(fact, dict):
                payload = dict(fact)
            else:
                payload = {
                    field: getattr(fact, field)
                    for field in (
                        "state",
                        "source",
                        "observed_at",
                        "reason_code",
                        "known_position_ids",
                        "unknown_position_ids",
                    )
                    if hasattr(fact, field)
                }
            for key in ("known_position_ids", "unknown_position_ids"):
                if isinstance(payload.get(key), tuple):
                    payload[key] = list(payload[key])
            component_facts[str(name)] = payload
    try:
        from config.runtime_config import shared as _rc

        positions = _enrich_positions_with_path_metrics(
            positions,
            cfg=_rc(),
            now_ts=observed_at,
            persist=bool(persist),
            broker=broker,
            strategy_name=_current_loop_strategy_name(),
        )
    except Exception as exc:
        # Enrichment/audit is advisory; the broker snapshot remains usable by
        # the safety plane when PostgreSQL or learning metadata is unavailable.
        logger.warning("[live] position snapshot enrichment unavailable: {}", exc)
    safe_positions = _safe_container_snapshot(positions)
    if not isinstance(safe_positions, list):
        safe_positions = []
    positions = [
        item if isinstance(item, dict) else {}
        for item in safe_positions
    ]
    live_state_update(
        positions=positions,
        positions_reconciled=_safe_container_snapshot(positions),
        positions_updated_at=observed_at,
        positions_reconcile_id=reconcile_id,
        positions_reconcile_failed_at=None,
        positions_reconcile_error=None,
        positions_component_facts=_safe_container_snapshot(component_facts),
    )
    # HTTP/compatibility reads may publish a broker fact without owning the
    # durable recovery/lifecycle write. Only the Safety and execution paths
    # pass persist=True; a loop-running flag is not an ownership boundary.
    if not bool(live_state_get("loop_running", False)) or not persist:
        return positions
    # A successful Reconcile response is authoritative only when it agrees
    # with broker-confirmed opens that have not yet received a complete close
    # deal.  Do not turn that conflict into a safe empty account: keep normal
    # multi-position operation when every durable open ID is present, but
    # durably stop *new* risk while any such ID is absent.
    conflict_reason = ""
    try:
        recovery_ids = _lifecycle_recovery_active_position_ids(
            live_close_settlement.list_active_recovery_positions(broker)
        )
        broker_ids = {
            int(position.get("position_id") or position.get("ticket") or 0)
            for position in positions
            if int(position.get("position_id") or position.get("ticket") or 0) > 0
        }
        missing_recovery_ids = sorted(recovery_ids - broker_ids)
        if missing_recovery_ids and bridge is not None and persist:
            # Fresh broker identity is authoritative for recovery rows that
            # have no entry lineage.  Those rows are orphaned/test state (for
            # example synthetic three-digit IDs), not trades waiting for a
            # close deal.  Purge only that narrow class; rows with an entry
            # decision remain on the close-deal proof path below.
            purged_ids = live_close_settlement.recovery_position_store().purge_unbrokered(
                set(missing_recovery_ids),
                broker=broker,
                broker_position_ids=broker_ids,
            )
            if purged_ids:
                logger.warning(
                    f"[live] purged orphaned recovery rows absent from fresh broker "
                    f"snapshot: {sorted(purged_ids)}"
                )
                live_close_settlement.release_orphaned_recovery_session_latches(
                    purged_ids,
                    broker=broker,
                    broker_position_ids=broker_ids,
                    reconcile_id=reconcile_id,
                    observed_at=observed_at,
                )
                recovery_ids = _lifecycle_recovery_active_position_ids(
                    live_close_settlement.list_active_recovery_positions(broker)
                )
                missing_recovery_ids = sorted(recovery_ids - broker_ids)
            # A broker-side close can race this first fresh snapshot.  Resolve
            # each missing durable row once through the existing close-deal
            # retirement contract before turning the observation into a
            # persistent recovery conflict.  Missing/ambiguous deal evidence
            # deliberately leaves the fail-closed conflict in place.
            retired_ids: list[int] = []
            for position_id in missing_recovery_ids:
                try:
                    if live_close_settlement.retire_broker_missing_position(
                        bridge,
                        position_id,
                        broker=broker,
                        strategy_name=_current_loop_strategy_name(),
                        reason="fresh_reconcile_missing_recovery_position",
                        persist_reconcile=False,
                    ):
                        retired_ids.append(position_id)
                except Exception as exc:
                    logger.warning(
                        "[live] missing recovery close reconciliation failed "
                        "for pos {}: {}",
                        position_id,
                        exc,
                    )
            if retired_ids:
                recovery_ids = _lifecycle_recovery_active_position_ids(
                    live_close_settlement.list_active_recovery_positions(broker)
                )
                missing_recovery_ids = sorted(recovery_ids - broker_ids)
        if missing_recovery_ids:
            conflict_reason = "broker_recovery_position_conflict:" + ",".join(
                str(position_id) for position_id in missing_recovery_ids
            )
    except Exception as exc:
        missing_recovery_ids = []
        conflict_reason = "recovery_position_state_unavailable"
        logger.warning(
            "[live] cannot validate fresh broker positions against recovery state: {}",
            exc,
        )

    cause = "position_reconcile_conflict"
    cause_id = "broker_recovery_state"
    latch = no_new_risk_latch_status(fail_closed=True)
    active_causes = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(latch.get("causes") or [])
        if isinstance(item, dict)
    }
    cause_key = (cause, cause_id)
    if conflict_reason:
        _mark_positions_reconcile_failed(conflict_reason)
        if cause_key not in active_causes:
            try:
                activate_no_new_risk_latch(
                    reason=conflict_reason,
                    actor="system:position_reconcile",
                    correlation_id=reconcile_id,
                    metadata={
                        "broker": broker,
                        "reconcile_id": reconcile_id,
                        "observed_at": observed_at,
                        "missing_recovery_position_ids": missing_recovery_ids,
                    },
                    cause=cause,
                    cause_id=cause_id,
                )
            except Exception as exc:
                logger.error(
                    "[live] failed to persist position reconcile conflict latch: {}",
                    exc,
                )
        live_state_update(
            accepting_new_risk=False,
            no_new_risk_latch=no_new_risk_latch_status(fail_closed=True),
        )
    elif cause_key in active_causes:
        try:
            release_no_new_risk_latch_cause(
                cause=cause,
                cause_id=cause_id,
                reason="broker_recovery_position_conflict_resolved",
                actor="system:position_reconcile",
                correlation_id=reconcile_id,
                evidence={
                    "broker": broker,
                    "reconcile_id": reconcile_id,
                    "observed_at": observed_at,
                    "broker_position_ids": sorted(broker_ids),
                },
            )
        except Exception as exc:
            logger.error(
                "[live] failed to release resolved position reconcile conflict latch: {}",
                exc,
            )
        live_state_update(no_new_risk_latch=no_new_risk_latch_status(fail_closed=True))
    return positions






def _plan_live_safety_candidates(
    *,
    bridge: Any,
    positions: list[dict[str, Any]],
    cfg: Any,
    account: dict[str, Any],
    current_price: float,
    atr_price: float,
    planned_at: float,
):
    """Wire read-only live projections into the pure v2 safety planner."""

    return plan_live_safety_candidates(
        positions=positions,
        cfg=cfg,
        account=account,
        current_price=current_price,
        atr_price=atr_price,
        planned_at=planned_at,
        entry_repair_cooldown_seconds=_ENTRY_PROTECTION_REPAIR_COOLDOWN_SECONDS,
        runtime=live_safety_planner.live_safety_planner_runtime(bridge),
    )


def _safety_candidate_execution_runtime(
    bridge: Any | None = None,
) -> SafetyCandidateExecutionRuntime:
    broker_schedule = _broker_schedule_from_bridge(bridge)
    return SafetyCandidateExecutionRuntime(
        enforce_holding_timeout=_enforce_holding_timeout,
        entry_protection_repair_source=_ENTRY_PROTECTION_REPAIR_SOURCE,
        runtime_config_anchor=_runtime_config_anchor,
        protection_candidate_cls=ProtectionCandidate,
        execute_protection_candidate=_execute_protection_candidate,
        evaluate_position_supervisor=lambda position, **kwargs: _evaluate_position_supervisor_for_position(
            position,
            broker_schedule=broker_schedule,
            **kwargs,
        ),
        build_safety_candidate=safety_candidate,
        run_position_supervision=_run_position_supervision,
    )


def _execute_live_safety_candidate(
    candidate: SafetyCandidate,
    *,
    bridge: Any,
    positions: list[dict[str, Any]],
    cfg: Any,
    account: dict[str, Any],
    pipeline: dict[str, Any],
    current_price: float,
    atr_price: float,
    tick: int,
    log,
    decision_ts: float,
) -> dict[str, Any]:
    del current_price, atr_price
    return _runtime_execute_safety_candidate(
        candidate,
        bridge=bridge,
        positions=positions,
        cfg=cfg,
        account=account,
        pipeline=pipeline,
        tick=tick,
        log=log,
        decision_ts=decision_ts,
        runtime=_safety_candidate_execution_runtime(bridge),
    )


def _run_live_safety_cycle(
    *,
    bridge: Any,
    broker: str,
    tick: int,
    log,
    generation_id: str = "",
    reconcile_result: Any | None = None,
    force_full_cycle: bool = False,
) -> dict[str, Any]:
    from config.runtime_config import shared as _runtime_config

    record_shadow_observation = build_safety_shadow_observer(
        generation_id=generation_id,
        broker=broker,
        tick=tick,
        get_live_state=live_state_get,
    )

    payload = _loop_v2_run_safety_cycle(
        bridge=bridge,
        broker=broker,
        tick=tick,
        log=log,
        generation_id=generation_id,
        reconcile_result=reconcile_result,
        force_full_cycle=force_full_cycle,
        runtime=LiveSafetyCycleRuntime(
            get_safety_plane=live_safety_plane.get_live_safety_plane,
            explicit_position_reconcile=_explicit_position_reconcile,
            publish_fresh_positions=partial(
                _publish_fresh_position_reconcile,
                bridge=bridge,
            ),
            get_live_state=live_state_get,
            update_live_state=live_state_update,
            runtime_config=_runtime_config,
            safety_reference_price=live_safety_planner.safety_reference_price,
            factor_pipeline=_factor_pipeline or {},
            plan_safety_candidates=_plan_live_safety_candidates,
            execute_safety_candidate=_execute_live_safety_candidate,
            run_position_protection_cycle=_run_position_protection_cycle,
            persist_safety_fail_closed=live_safety_watchdog.persist_safety_fail_closed,
            controller=_LIVE_LOOP_CONTROLLER,
            record_shadow_observation=record_shadow_observation,
        ),
    )
    if str(payload.get("reconciliation_state") or "") != "fresh":
        _mark_positions_reconcile_failed(
            str(payload.get("reconciliation_error") or "safety_positions_reconcile_failed")
        )
    return payload


def _recover_execution_outcomes_before_alpha(
    *,
    bridge: Any,
    broker: str,
    tick: int,
    log,
    generation_id: str,
    safety_result: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    return _loop_recover_execution_outcomes(
        bridge=bridge,
        broker=broker,
        tick=tick,
        log=log,
        generation_id=generation_id,
        generation_startup_pending=bool(
            generation_id and not _LIVE_LOOP_CONTROLLER.status().get("ready")
        ),
        safety_result=safety_result,
        runtime=ExecutionRecoveryRuntime(
            get_cached_recovery=lambda: live_state_get(
                "execution_recovery", {}, clone=True
            )
            or {},
            update_live_state=live_state_update,
            explicit_position_reconcile=_explicit_position_reconcile,
            run_safety_cycle=_run_live_safety_cycle,
            update_generation_health=_update_execution_recovery_generation_health,
        ),
    )


def _update_execution_recovery_generation_health(
    owner: str,
    blockers: tuple[str, ...],
) -> None:
    try:
        _LIVE_LOOP_CONTROLLER.update_runtime_health(owner, blockers=blockers)
    except RuntimeError:
        pass


def _attempt_generation_startup_barrier(
    *,
    generation_id: str,
    bridge: Any,
    broker: str,
    tick: int,
    log,
    account_reconcile: Any,
    positions_reconcile: Any,
    safety_result: dict[str, Any],
) -> bool:
    return _loop_v2_attempt_startup_barrier(
        generation_id=generation_id,
        bridge=bridge,
        broker=broker,
        tick=tick,
        log=log,
        account_reconcile=account_reconcile,
        positions_reconcile=positions_reconcile,
        safety_result=safety_result,
        runtime=StartupBarrierRuntime(
            controller=_LIVE_LOOP_CONTROLLER,
            update_live_state=live_state_update,
            get_live_state=live_state_get,
            explicit_position_reconcile=_explicit_position_reconcile,
            publish_fresh_positions=_publish_fresh_position_reconcile,
            run_safety_cycle=_run_live_safety_cycle,
            restore_session_state=live_close_settlement.restore_session_state_for_day,
            bootstrap_position_recovery=live_close_settlement.bootstrap_position_recovery,
            factor_pipeline=_factor_pipeline or {},
            strategy_name=_current_loop_strategy_name(),
        ),
    )


def _live_loop_tick_runtime() -> LiveLoopTickRuntime:
    return LiveLoopTickRuntime(
        get_ctrader=get_ctrader,
        reconcile_positions=_explicit_position_reconcile,
        run_safety_cycle=_run_live_safety_cycle,
        persist_safety_fail_closed=live_safety_watchdog.persist_safety_fail_closed,
        reconcile_account=_explicit_account_reconcile,
        reconcile_value=_reconcile_value,
        mark_account_reconcile_failed=_mark_account_reconcile_failed,
        live_state_update=live_state_update,
        loop_controller=_LIVE_LOOP_CONTROLLER,
        set_loop_diagnostic=_set_loop_diagnostic,
        recover_execution_outcomes=(
            _recover_execution_outcomes_before_alpha
        ),
        attempt_startup_barrier=_attempt_generation_startup_barrier,
        live_state_get=live_state_get,
        bootstrap_position_recovery=live_close_settlement.bootstrap_position_recovery,
        loop_strategy_name=_current_loop_strategy_name(),
        restore_session_state=live_close_settlement.restore_session_state_for_day,
        session_circuit_breaker_enforced=lambda: not bounded_demo_mode_active(),
        evaluate_daily_drawdown=live_close_settlement.evaluate_daily_drawdown,
        market_session_snapshot=market_session_snapshot,
        ensure_spot_subscription=_ensure_spot_subscription,
        get_live_bars=get_live_bars,
        ensure_decision_bars_fresh=live_bar_warmup.ensure_live_decision_bars_fresh,
        get_safety_plane=live_safety_plane.get_live_safety_plane,
        retry_pending_open=live_open_pipeline.retry_pending_open_trade,
        process_tick=_process_tick,
        update_risk_metrics=live_loop_tick_runtime.update_live_loop_risk_metrics,
    )


def _run_live_loop_tick_body(
    *,
    broker: str,
    bridge_cfg: Any,
    timeframe: str,
    tick: int,
    recovery_bootstrapped: bool,
    stop_requested,
    log,
    generation_id: str = "",
) -> dict[str, Any]:
    return _runtime_run_live_loop_tick_body(
        broker=broker,
        bridge_cfg=bridge_cfg,
        timeframe=timeframe,
        tick=tick,
        recovery_bootstrapped=recovery_bootstrapped,
        stop_requested=stop_requested,
        log=log,
        generation_id=generation_id,
        runtime=_live_loop_tick_runtime(),
    )








def _run_loop(
    broker: str,
    stop_flag: threading.Event,
    generation_id: str = "",
) -> None:
    """Generation-owned loop entrypoint with a single lifecycle exit path."""
    failed_reason = ""
    try:
        _run_loop_body(broker, stop_flag, generation_id=generation_id)
    except BaseException as exc:
        failed_reason = f"{type(exc).__name__}: {exc}"
        logger.exception("[live] generation {} failed", generation_id or "unowned")
        raise
    finally:
        live_state_update(accepting_new_risk=False)
        if generation_id:
            try:
                _LIVE_LOOP_CONTROLLER.acknowledge_exit(
                    generation_id,
                    failed_reason=failed_reason,
                )
            except RuntimeError as exc:
                logger.error("[live] loop exit ownership mismatch: {}", exc)
        # The scheduler is process-owned: readiness, health and data
        # maintenance must remain alive while a generation is stopped or being
        # recovered.  Generation-sensitive jobs guard on loop ownership.
        # Only BackendRuntimeLifecycle.stop() shuts the scheduler down.
        live_safety_watchdog.stop_live_safety_watchdog()
        if not _process_shutdown_requested:
            try:
                if schedule_auto_resume_loop():
                    logger.warning(
                        "[live] loop exited while desired state remained enabled; "
                        "auto-resume scheduled"
                    )
            except Exception as exc:
                logger.error("[live] failed to schedule loop auto-resume: {}", exc)


def _startup_safety_runtime() -> StartupSafetyRuntime:
    return StartupSafetyRuntime(
        get_ctrader=get_ctrader,
        reconcile_positions=_explicit_position_reconcile,
        run_safety_cycle=_run_live_safety_cycle,
        reconcile_account=_explicit_account_reconcile,
        reconcile_value=_reconcile_value,
        live_state_update=live_state_update,
        persist_safety_fail_closed=live_safety_watchdog.persist_safety_fail_closed,
    )


def _bar_warmup_runtime() -> BarWarmupRuntime:
    return BarWarmupRuntime(
        warmup_from_local_db=live_bar_warmup.warmup_from_local_db,
        get_ctrader=get_ctrader,
        wait_ctrader_ready=wait_ctrader_ready,
        fetch_bars_with_retry=live_bar_warmup.fetch_bars_with_retry,
        load_bar_cache=live_bar_warmup.load_bar_cache,
        publish_latest_price=_publish_latest_price,
        save_bar_cache=live_bar_warmup.save_bar_cache,
        logger_warning=logger.warning,
        now=time.time,
    )


def _factor_warmup_runtime() -> FactorWarmupRuntime:
    return FactorWarmupRuntime(
        build_warmup_feed=_loop_build_warmup_feed,
        build_factor_votes=_tick_build_factor_votes,
        build_snapshot_summary=_tick_build_factor_snapshot_summary,
        set_factor_snapshot=_set_factor_snapshot,
        acknowledge_projections=_loop_ack_prepared_factor_projections,
        now=time.time,
        build_low_frequency_snapshots=_build_low_frequency_factor_snapshots,
    )






def _factor_initialization_runtime() -> FactorInitializationRuntime:
    from alpha.attribution_engine import AttributionEngine
    from alpha.execution_gate import ExecutionGate
    from alpha.portfolio_compositor import PortfolioCompositor
    from alpha.runtime_factor_selection import select_runtime_factors
    from alpha.signal_normalizer import SignalNormalizer
    from alpha.streaming_factor_engine import StreamingFactorEngine
    from backend.services.runtime_factor_selection_projection import (
        RuntimeFactorSelectionProjectionService,
    )
    from config.runtime_config import shared, subscribe
    from risk.cross_asset import CrossAssetCovariance

    return FactorInitializationRuntime(
        config_factory=shared,
        engine_cls=StreamingFactorEngine,
        normalizer_cls=SignalNormalizer,
        compositor_cls=PortfolioCompositor,
        gate_cls=ExecutionGate,
        attribution_cls=AttributionEngine,
        selection_factory=select_runtime_factors,
        projection_service_factory=RuntimeFactorSelectionProjectionService,
        event_sizing_factory=live_factor_bootstrap.factor_event_sizing_factory,
        subscribe_config=subscribe,
        generation_active=live_factor_bootstrap.factor_generation_active,
        merge_portfolio_configs=_merge_portfolio_configs,
        execution_gate_config=_loop_execution_gate_config,
        unique_factor_pipelines=_loop_unique_factor_pipelines,
        apply_config_update=_loop_apply_factor_pipeline_config_update,
        acknowledge_projections=_loop_ack_prepared_factor_projections,
        enabled_symbols=_loop_enabled_symbols_from_config,
        build_extra_symbol_pipelines=(
            _loop_build_extra_symbol_factor_pipelines
        ),
        cross_asset_symbols=_loop_cross_asset_symbols_for_config,
        covariance_cls=CrossAssetCovariance,
        logger_warning=logger.warning,
        logger_debug=logger.debug,
    )


def _initialize_live_factor_pipelines(
    *,
    generation_id: str,
    log,
) -> FactorInitializationResult:
    try:
        runtime = _factor_initialization_runtime()
    except Exception as exc:
        log(f"Factor pipeline init failed: {exc}")
        log(f"  Traceback: {traceback.format_exc()[-600:]}")
        return FactorInitializationResult(
            config=None,
            pipeline=None,
            pipelines={},
            cross_asset_covariance=None,
            error=f"{type(exc).__name__}: {exc}",
        )
    return _bootstrap_initialize_factor_pipelines(
        generation_id=generation_id,
        log=log,
        runtime=runtime,
    )


def _serial_live_tick_runtime() -> SerialLiveTickRuntime:
    return SerialLiveTickRuntime(
        set_loop_diagnostic=_set_loop_diagnostic,
        run_tick_body=_run_live_loop_tick_body,
        factor_pipeline=lambda: _factor_pipeline,
        acknowledge_factor_projections=(
            _loop_ack_prepared_factor_projections
        ),
        live_state_update=live_state_update,
    )


def _run_loop_body(
    broker: str,
    stop_flag: threading.Event,
    *,
    generation_id: str = "",
) -> None:
    """Own the live-loop log resource around one generation."""
    from pathlib import Path
    from config.runtime_config import shared as _runtime_config

    cfg = _runtime_config()
    timeframe = cfg.timeframe
    project_root = Path(__file__).resolve().parent.parent.parent
    log_path = project_root / "logs" / "live_loop.log"
    log_path.parent.mkdir(exist_ok=True)
    log_handle = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(message: str) -> None:
        line = (
            f"{time.strftime('%H:%M:%S')} "
            f"[live_loop:{broker}] {message}"
        )
        log_handle.write(line + "\n")
        log_handle.flush()
        logger.info(line)

    try:
        _run_loop_body_active(
            broker,
            stop_flag,
            generation_id=generation_id,
            runtime_config=cfg,
            timeframe=timeframe,
            log=log,
        )
    finally:
        log_handle.close()


def _run_loop_body_active(
    broker: str,
    stop_flag: threading.Event,
    *,
    generation_id: str,
    runtime_config,
    timeframe: str,
    log,
) -> None:
    """Run one generation after its resources and config are bound."""
    global _factor_pipeline
    _factor_pipeline = None
    _rcfg = runtime_config
    TF = timeframe

    log(f"live loop started (broker={broker}, timeframe={TF})")

    _bootstrap_run_startup_safety(
        broker=broker,
        generation_id=generation_id,
        log=log,
        runtime=_startup_safety_runtime(),
    )

    warmup = _bootstrap_warmup_live_bars(
        broker=broker,
        timeframe=TF,
        log=log,
        runtime=_bar_warmup_runtime(),
        requested_bars=max(200, int(getattr(_rcfg, "var_window", 500) or 500) + 1),
    )
    if warmup is None:
        return
    df = warmup.frame

    global _LEDGER, _TRADE_REVIEWER, _EXPERIENCE_BUILDER, _POLICY_SUGGESTER
    global _factor_pipelines, _cross_asset_covar

    factor_bootstrap = _initialize_live_factor_pipelines(
        generation_id=generation_id,
        log=log,
    )
    _factor_pipeline = factor_bootstrap.pipeline
    _factor_pipelines = factor_bootstrap.pipelines
    _cross_asset_covar = factor_bootstrap.cross_asset_covariance
    if factor_bootstrap.config is not None:
        _rcfg = factor_bootstrap.config

    if _factor_pipeline is not None:
        try:
            if _LEDGER is None:
                _LEDGER = DecisionLedger()
            if _TRADE_REVIEWER is None:
                _TRADE_REVIEWER = TradeReviewer()
            if _EXPERIENCE_BUILDER is None:
                _EXPERIENCE_BUILDER = ExperienceBuilder()
            if _POLICY_SUGGESTER is None:
                _POLICY_SUGGESTER = PolicySuggester()
        except Exception as exc:
            log(f"Factor audit bootstrap failed closed: {exc}")
            _factor_pipeline = None
            _factor_pipelines = {}
            _cross_asset_covar = None

    _bootstrap_warmup_factor_pipeline(
        _factor_pipeline,
        df,
        cfg=_rcfg,
        timeframe=TF,
        generation_id=generation_id,
        log=log,
        runtime=_factor_warmup_runtime(),
    )

    # 订阅 cTrader 实时报价；warmup local_db 路径从 get_ctrader() 拿真 bridge 并短等 ready.
    if broker == "ctrader":
        try:
            _loop_subscribe_spot_once(
                get_ctrader=get_ctrader,
                wait_ctrader_ready=wait_ctrader_ready,
                log=log,
                timeout_sec=10.0, timeframe=TF, seed_frame=df,
            )
        except Exception as e:
            log(f"subscribe_spots failed (non-fatal): {e}")

    _runtime_run_serial_live_ticks(
        broker=broker,
        stop_flag=stop_flag,
        bridge_cfg=_rcfg,
        timeframe=TF,
        generation_id=generation_id,
        log=log,
        runtime=_serial_live_tick_runtime(),
    )


def _set_alpha_runtime_status(
    *,
    status: str,
    tick: int,
    reason: str = "",
) -> None:
    """Publish the generation-local alpha admission result.

    Safety remains the protection owner.  This status only prevents the
    failed alpha tick/generation from being presented as healthy; the next
    fresh Safety boundary may admit a retry, and a successful pipeline clears
    the transient blocker.
    """

    normalized_status = "failed" if str(status or "").lower() == "failed" else "healthy"
    now_ts = time.time()
    payload = {
        "schema_version": "alpha_runtime.v1",
        "status": normalized_status,
        "admission": "blocked" if normalized_status == "failed" else "allowed",
        "tick": int(tick or 0),
        "reason": str(reason or ""),
        "observed_at": now_ts,
    }
    generation = _LIVE_LOOP_CONTROLLER.current()
    if generation is None:
        live_state_update(
            alpha_runtime=payload,
            alpha_failed=normalized_status == "failed",
            accepting_new_risk=False,
        )
        return
    blockers = set(getattr(generation, "runtime_blockers", ()) or ())
    if normalized_status == "failed":
        blockers.add("alpha_failed")
    else:
        blockers.discard("alpha_failed")
    try:
        accepting = _LIVE_LOOP_CONTROLLER.update_runtime_health(
            generation.generation_id,
            blockers=tuple(sorted(blockers)),
        )
    except RuntimeError:
        # The generation may have started draining while the alpha callback
        # unwound.  Safety/stop ownership remains authoritative in that case.
        logger.debug("[live] alpha status update lost generation ownership")
        accepting = False
    live_state_update(
        alpha_runtime=payload,
        alpha_failed=normalized_status == "failed",
        accepting_new_risk=bool(accepting) if normalized_status == "healthy" else False,
    )


@record_timed("live.process_tick")
def _process_tick(
    bridge,
    strategy,
    df_new,
    last_bar,
    broker: str,
    tick: int,
    log,
    *,
    stop_requested=None,
    protection_already_run: bool = False,
) -> None:
    """处理一根新 bar — 全部由 Factor Takeover v4 因子管道驱动。"""
    global _factor_pipeline
    if _factor_pipeline is not None:
        try:
            result = _process_tick_factor_pipeline(
                bridge, _factor_pipeline, df_new, last_bar, broker, tick, log,
                stop_requested=stop_requested,
                protection_already_run=protection_already_run,
            )
            _set_alpha_runtime_status(status="healthy", tick=tick)
            return result
        except Exception as e:
            log(f"tick {tick}: factor pipeline error: {e}")
            _set_alpha_runtime_status(
                status="failed",
                tick=tick,
                reason=f"{type(e).__name__}: {e}",
            )

    # 保底: 无管道时只记 tick 不操作
    log(f"tick {tick}: no factor pipeline active, skipping")


# ═══════════════════════════════════════════════════════════
# Factor Takeover v4: 因子管道 _process_tick


# ═══════════════════════════════════════════════════════════
# Factor Takeover v4 管道状态
# ── Factor Takeover v4 管道 ──
# 由 _run_loop 初始化, _process_tick 读取
_factor_pipeline: dict | None = None  # {engine, normalizer, compositor, gate}
_factor_pipeline_lock = threading.Lock()

# Phase 4: 执行质量分析器
from execution.analytics import ExecutionQuality, TradeExecution as _ExecTrade
_exec_quality = ExecutionQuality(max_records=500)

# Phase 6: 多品种并行管道
_factor_pipelines: dict[str, dict] = {}  # {symbol: {engine, normalizer, ...}}
_cross_asset_covar: "CrossAssetCovariance | None" = None  # 跨品种协方差


# ═══════════════════════════════════════════════════════════
# Canonical decision and lifecycle facts
# ═══════════════════════════════════════════════════════════


def _should_send_orders(broker: str, *, log_blocking: bool = True) -> bool:
    """True = 真发单; False = dry-run; optionally suppress repeat read logs."""
    if broker == "ctrader":
        from backend.services.execution_semantics import current_execution_semantics

        semantics = current_execution_semantics()
        if semantics.blocking_reason and log_blocking:
            logger.warning("[live] send-orders blocked by execution semantics: {}", semantics.blocking_reason)
            return False
        return bool(semantics.effective_send_orders)
    return False


# 模块级,供 read_state_snapshot 读
_latest_price: float | None = None
_latest_price_updated_at: float = 0.0
_latest_bar_price_cache: tuple[float, float] | None = None


def _publish_latest_price(price: float | int | str | None, *, source: str = "unknown", ts: float | None = None) -> float | None:
    """Publish the latest known XAU price to the shared in-process state."""
    try:
        value = float(price or 0.0)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None

    global _latest_price, _latest_price_updated_at
    now_ts = float(ts or time.time())
    _latest_price = value
    _latest_price_updated_at = now_ts

    quote = live_state_get("spot_quote", None, clone=True)
    if not _quote_is_fresh(quote):
        live_state_update(
            spot_price=value,
            spot_quote={
                "bid": 0.0,
                "ask": 0.0,
                "mid": value,
                "ts": now_ts,
                "source": source,
            },
        )
    else:
        live_state_update(spot_price=value)
    return value


def _latest_bar_close_from_store() -> float | None:
    """Read the latest local XAU M5 bar close with a short TTL; no broker call."""
    global _latest_bar_price_cache
    now_ts = time.time()
    if _latest_bar_price_cache and now_ts - _latest_bar_price_cache[0] < 5.0:
        return _latest_bar_price_cache[1]
    try:
        from data.duckdb_store import DuckDBDataStore as DataStore

        store = DataStore()
        df = store.load_bars("XAUUSD+", "M5", limit=1)
        if df is None or len(df) == 0:
            return None
        price = float(df.iloc[-1]["close"] or 0.0)
        if price > 0:
            _latest_bar_price_cache = (now_ts, price)
            return price
    except Exception as exc:
        logger.debug("[live] latest bar close fallback failed: {}", exc)
    return None


def get_latest_price() -> float | None:
    """返回最新价. 优先共享缓存 (live loop 写), 其次 bridge spot, 最后本地 bar close."""
    quote = live_state_get("spot_quote", None, clone=True)
    if _quote_is_fresh(quote):
        spot = float((quote or {}).get("mid") or 0.0)
        if spot > 0:
            return spot
    cached_spot = live_state_get("spot_price", None)
    try:
        if cached_spot is not None and float(cached_spot or 0.0) > 0:
            return float(cached_spot)
    except (TypeError, ValueError):
        pass
    global _latest_price
    if _latest_price and _latest_price > 0:
        return _latest_price
    try:
        # audit 2026-06-10: 3-tuple; warming_up 时返旧价不阻塞
        bridge, err, warming = get_ctrader()
        if bridge is None or err or warming or not bridge.is_connected:
            fallback = _latest_bar_close_from_store()
            return _publish_latest_price(fallback, source="bar_close") if fallback else _latest_price
        quote = bridge.get_spot_quote() if hasattr(bridge, "get_spot_quote") else {}
        spot = float((quote or {}).get("mid") or 0.0) if _quote_is_fresh(quote) else 0.0
        if spot > 0:
            live_state_update(spot_quote=quote)
            return spot
    except Exception as _e2:
        logger.debug("[live] get_latest_price spot query failed: {}", _e2)
    fallback = _latest_bar_close_from_store()
    return _publish_latest_price(fallback, source="bar_close") if fallback else _latest_price


# ── Emergency close ──────────────────────────────────────────────────────

_EMERGENCY_POST_RECONCILE_TIMEOUT_SEC = 20.0
_EMERGENCY_POST_RECONCILE_INTERVAL_SEC = 0.5
_EMERGENCY_MONOTONIC = time.monotonic
_EMERGENCY_SLEEP = time.sleep


def _recover_emergency_execution_intents(bridge: Any) -> dict[str, Any]:
    return _runtime_recover_emergency_intents(bridge)


def emergency_close(broker: str, symbol: str | None = None) -> dict:
    """Wire process-local callbacks into the strict emergency domain service."""
    return _run_emergency_close(
        broker,
        symbol,
        runtime=EmergencyCloseRuntime(
            update_live_state=live_state_update,
            admission_lock=_OPEN_TRADE_ADMISSION_LOCK,
            get_ctrader=get_ctrader,
            wait_ctrader_ready=wait_ctrader_ready,
            reconcile_positions=_fresh_emergency_position_reconcile,
            position_volume=_position_api_volume,
            build_close_risk_context=_build_close_position_risk_context,
            risk_policy=_RISK_POLICY,
            remember_close_reason=live_close_settlement.remember_close_reason,
            remember_close_verdict=live_close_settlement.remember_close_verdict,
            recover_execution_intents=_recover_emergency_execution_intents,
            post_reconcile_timeout_sec=_EMERGENCY_POST_RECONCILE_TIMEOUT_SEC,
            post_reconcile_interval_sec=_EMERGENCY_POST_RECONCILE_INTERVAL_SEC,
            monotonic=_EMERGENCY_MONOTONIC,
            sleep=_EMERGENCY_SLEEP,
        ),
    )


# ═══════════════════════════════════════════════════════════
# Factor Takeover v4: 因子管道 _process_tick
# ═══════════════════════════════════════════════════════════










def _closed_position_processing_runtime() -> ClosedPositionProcessingRuntime:
    return ClosedPositionProcessingRuntime(
        consume_close_reason=live_close_settlement.consume_close_reason,
        consume_close_verdict=live_close_settlement.consume_close_verdict,
        classify_close_source=live_close_settlement.classify_close_source,
        select_close_total_pnl=_tick_select_close_total_pnl,
        open_api_volumes=_pos_open_api_volume,
        ledger=_LEDGER,
        ensure_open_ledger=live_close_settlement.ensure_open_ledger_for_recovered_close,
        lookup_context_integrity=live_close_settlement.lookup_recovery_context_integrity,
        build_close_ledger_payloads=_tick_build_close_ledger_payloads,
        get_session_pnl=lambda: live_state_get("session_pnl", 0),
        risk_state_with_verdict=live_close_settlement.risk_state_with_verdict_dict,
        trade_reviewer=_TRADE_REVIEWER,
        experience_builder=_EXPERIENCE_BUILDER,
        policy_suggester=_POLICY_SUGGESTER,
        build_trade_review_payload=_tick_build_trade_review_payload,
        mark_recovery_closed=live_close_settlement.mark_recovery_position_closed,
        entry_scores=_pos_entry_scores,
        entry_decisions=_pos_entry_decisions,
        pending_open_attach_until=_pending_open_attach_until,
        now=time.time,
        debug=logger.debug,
        info=logger.info,
        exception=logger.exception,
    )


def _collect_closed_position_attribution(
    *,
    cpid: int,
    real_pnl: dict | None,
    attr_engine: Any,
    tick: int,
    log,
) -> dict[str, Any]:
    return _runtime_collect_close_attribution(
        position_id=cpid,
        real_pnl=real_pnl,
        attr_engine=attr_engine,
        tick=tick,
        log=log,
        runtime=_closed_position_processing_runtime(),
    )


def _log_closed_position_ledger_after_tick(
    *,
    cpid: int,
    broker: str,
    close_ts: float,
    current_price: float,
    real_pnl: dict | None,
    close_reason: str,
    context_integrity: str,
    cfg: Any,
    bar: dict,
    acct: dict,
    total_pnl: float,
    tick: int,
    close_source: dict[str, Any] | str | None,
    attribution_integrity: str,
    close_verdict: dict,
    factor_contributions: dict,
) -> tuple[str, str]:
    return _runtime_log_closed_position_ledger(
        position_id=cpid,
        broker=broker,
        close_ts=close_ts,
        current_price=current_price,
        real_pnl=real_pnl,
        close_reason=close_reason,
        context_integrity=context_integrity,
        cfg=cfg,
        bar=bar,
        account=acct,
        total_pnl=total_pnl,
        tick=tick,
        close_source=close_source,
        attribution_integrity=attribution_integrity,
        close_verdict=close_verdict,
        factor_contributions=factor_contributions,
        runtime=_closed_position_processing_runtime(),
    )


def _run_closed_position_learning_after_tick(
    *,
    cpid: int,
    total_pnl: float,
    current_price: float,
    close_ts: float,
    factor_contributions: dict,
    exit_decision_id: str,
    real_pnl: dict | None,
    close_reason: str,
    context_integrity: str,
    attribution_integrity: str,
    close_source: dict[str, Any] | str | None,
) -> None:
    _runtime_run_closed_position_learning(
        position_id=cpid,
        total_pnl=total_pnl,
        current_price=current_price,
        close_ts=close_ts,
        factor_contributions=factor_contributions,
        exit_decision_id=exit_decision_id,
        real_pnl=real_pnl,
        close_reason=close_reason,
        context_integrity=context_integrity,
        attribution_integrity=attribution_integrity,
        close_source=close_source,
        runtime=_closed_position_processing_runtime(),
    )


def _cleanup_closed_position_after_tick(
    *,
    cpid: int,
    close_reason: str,
    total_pnl: float,
    close_ts: float,
    real_pnl: dict | None,
    factor_contributions: dict,
) -> bool:
    return _runtime_cleanup_closed_position(
        position_id=cpid,
        close_reason=close_reason,
        total_pnl=total_pnl,
        close_ts=close_ts,
        real_pnl=real_pnl,
        factor_contributions=factor_contributions,
        runtime=_closed_position_processing_runtime(),
    )


def _book_probation_outcomes(real_pnls: Any) -> None:
    """Probation 记账: 只认权威平仓成交 PnL;book 为空(未触发)时内部直接返回。"""
    for _pid, _pnl_payload in (real_pnls or {}).items():
        try:
            if not _authoritative_close_pnl(_pnl_payload):
                continue
            _record_probation_trade_outcome(
                float((_pnl_payload or {}).get("net", 0.0) or 0.0),
                position_id=int(_pid or 0),
            )
        except Exception:
            # 记账失败不得阻断平仓管线。
            continue


def _handle_closed_positions_after_tick(
    *,
    closed_pids: set[int],
    real_pnls: dict[int, dict],
    attr_engine: Any,
    bar: dict,
    cfg: Any,
    acct: dict,
    broker: str,
    tick: int,
    log,
    broker_open_position_ids: set[int] | None = None,
    bridge: Any | None = None,
    close_deal_cursors: dict[int, dict[str, Any]] | None = None,
) -> None:
    _book_probation_outcomes(real_pnls)
    _runtime_handle_closed_positions(
        closed_pids=closed_pids,
        real_pnls=real_pnls,
        attr_engine=attr_engine,
        bar=bar,
        cfg=cfg,
        account=acct,
        broker=broker,
        tick=tick,
        log=log,
        broker_open_position_ids=broker_open_position_ids,
        bridge=bridge,
        close_deal_cursors=close_deal_cursors,
        runtime=ClosedPositionCycleRuntime(
            authoritative_close_pnl=_authoritative_close_pnl,
            defer_close=live_close_settlement.defer_close_until_authoritative_deal,
            update_live_state=live_state_update,
            collect_attribution=_collect_closed_position_attribution,
            lookup_context_integrity=live_close_settlement.lookup_recovery_context_integrity,
            log_closed_position_ledger=_log_closed_position_ledger_after_tick,
            run_closed_position_learning=_run_closed_position_learning_after_tick,
            cleanup_closed_position=_cleanup_closed_position_after_tick,
            record_aux_failure=live_close_settlement.record_risk_reduction_aux_failure,
            mark_recovery_closed=live_close_settlement.mark_recovery_position_closed,
            reconcile_account=_explicit_account_reconcile,
            reconcile_value=_reconcile_value,
            restore_session_state=live_close_settlement.restore_session_state_for_day,
            release_close_latch=live_close_settlement.release_session_close_deal_latch,
            trade_date=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            now=time.time,
            full_context=_RECOVERY_CONTEXT_FULL,
        ),
    )














































































def _process_tick_existing_decision_bar(
    *,
    bridge,
    pipeline: dict,
    cfg: Any,
    bar: dict[str, Any],
    last_bar,
    broker: str,
    tick: int,
    log,
    last_processed_ts: float,
    protection_already_run: bool = False,
) -> None:
    """Run observe/protection work without feeding a duplicate decision bar."""
    global _prev_position_ids

    acct = live_state_get("account", {}, clone=True) or {}
    positions_payload = live_state_get("positions", [], clone=True) or []
    _positions_probe = (
        (positions_payload.get("positions", []) or [])
        if isinstance(positions_payload, dict)
        else positions_payload
    )
    if _positions_probe and not isinstance(_positions_probe[0], dict):
        from backend.ws.endpoints import position_to_dict
    else:
        position_to_dict = None
    pos = _tick_normalize_live_positions_payload(
        positions_payload,
        position_to_dict=position_to_dict,
    )

    current_price = float(last_bar["close"])
    signal_decision_id = ""
    if bridge is not None and hasattr(bridge, "get_spot_quote"):
        price_guard = _tick_guard_current_price_with_spot_quote(
            current_price=current_price,
            get_spot_quote=bridge.get_spot_quote,
            quote_is_fresh=_quote_is_fresh,
        )
        current_price = float(price_guard["current_price"])
        if price_guard["error"] is not None:
            logger.debug("[live] spot price guard failed for tick {}: {}", tick, price_guard["error"])

    current_pids = _tick_collect_position_ids(pos)
    attr_engine = pipeline.get("attribution")
    positions_snapshot_ready = bool(live_state_get("positions_updated_at", 0.0))
    closed_pids, current_pids, close_detection_deferred = _tick_resolve_closed_position_ids(
        previous_position_ids=_prev_position_ids,
        current_position_ids=current_pids,
        positions_snapshot_ready=positions_snapshot_ready,
        tracked_position_ids=(
            live_close_settlement.active_recovery_position_ids_for_close_detection(broker)
        ),
    )
    if close_detection_deferred:
        log(f"tick {tick}: positions cache not ready, defer close detection")
    restored_attributions = _restore_attribution_for_positions(attr_engine, pos)
    if restored_attributions:
        log(f"tick {tick}: attribution restored open contexts={restored_attributions}")

    real_pnls: dict[int, dict] = {}
    close_deal_cursors: dict[int, dict[str, Any]] = {}
    if closed_pids and bridge is not None:
        try:
            real_pnls = live_close_settlement.sync_closed_position_deals_for_tick(
                bridge,
                closed_pids,
                observed_close_cursor_out=close_deal_cursors,
            )
        except Exception as _ds_err:
            log(f"tick {tick}: deal_sync error: {_ds_err}")

    _handle_closed_positions_after_tick(
        closed_pids=closed_pids,
        real_pnls=real_pnls,
        attr_engine=attr_engine,
        bar=bar,
        cfg=cfg,
        acct=acct,
        broker=broker,
        tick=tick,
        log=log,
        broker_open_position_ids=current_pids,
        bridge=bridge,
        close_deal_cursors=close_deal_cursors,
    )
    for p in pos:
        pid = p.get("position_id") or p.get("ticket")
        if pid is not None and int(pid) not in _pos_open_prices:
            _pos_open_prices[int(pid)] = float(p.get("open_price", current_price))

    factor_values = dict(pipeline.get("last_factor_values") or {})
    atr_val = factor_values.get("atr_ratio", 0)
    atr_price = atr_val * current_price if atr_val and atr_val > 0 else 0
    if not protection_already_run and pos and bridge is not None and cfg is not None:
        _run_position_protection_cycle(
            bridge,
            pos,
            cfg=cfg,
            acct=acct,
            pipeline=pipeline,
            current_price=current_price,
            atr_price=atr_price,
            tick=tick,
            log=log,
        )

    log(
        f"tick {tick}: decision bar already processed "
        f"bar_ts={float(bar.get('time') or 0.0):.0f} last={last_processed_ts:.0f}; skip open decision"
    )
    log(f"tick {tick}: price={current_price:.2f} "
        f"balance={acct.get('balance', 0):.2f} "
        f"equity={acct.get('equity', 0):.2f} "
        f"pos={len(pos)} "
        f"pnl_session={live_state_get('session_pnl', 0):.2f}")
    _check_business_alerts(tick, acct, pos, log)
    _prev_position_ids = current_pids
    _publish_latest_price(current_price, source="loop_tick")


def _process_tick_factor_pipeline(
    bridge, pipeline: dict, df_new, last_bar, broker: str,
    tick: int, log, *, stop_requested=None, protection_already_run: bool = False,
) -> None:
    """使用 Factor Takeover v4 管道处理一根新 bar。

    流程:
        engine.append_bar → normalizer.normalize → compositor.compose
        → gate.filter → _execute_factor_signal
    """
    global _prev_position_ids
    from config.runtime_config import shared as _rc
    _tf = "M5"  # safe default before config access
    try:
        cfg = _rc()
        _tf = getattr(cfg, 'timeframe', 'M5')
    except Exception:
        cfg = None

    # 1. 构造 bar dict
    bar = _tick_build_factor_bar(last_bar, df_new, _tf)
    bar_progress = _factor_state_resolve_bar_progress(
        bar,
        live_state_get("last_processed_decision_bar_ts", 0.0),
    )
    if bar_progress.already_processed:
        _process_tick_existing_decision_bar(
            bridge=bridge,
            pipeline=pipeline,
            cfg=cfg,
            bar=bar,
            last_bar=last_bar,
            broker=broker,
            tick=tick,
            log=log,
            last_processed_ts=bar_progress.last_processed_ts,
            protection_already_run=protection_already_run,
        )
        return

    engine = pipeline["engine"]
    normalizer = pipeline["normalizer"]
    compositor = pipeline["compositor"]
    gate = pipeline["gate"]

    # 2. 流式因子计算 → 归一化 → 组合 → context policy → 闸门
    decision_frame = _decision_run_live_decision_pipeline(
        engine=engine,
        normalizer=normalizer,
        compositor=compositor,
        gate=gate,
        bar=bar,
        cfg=cfg,
    )
    if not decision_frame.ready:
        log(f"tick {tick}: {decision_frame.reason}")
        return

    committed_decision = _factor_state_commit_ready_decision(
        decision_frame=decision_frame,
        progress=bar_progress,
        pipeline=pipeline,
        update_live_state=live_state_update,
        set_factor_snapshot=_set_factor_snapshot,
        tick=tick,
        log=log,
    )
    factor_values = committed_decision.factor_values
    signals = committed_decision.signals
    composite = committed_decision.composite
    gate_result = committed_decision.gate_result
    # 3. 发单 (仅非 dry_run 且门通过)
    send = _should_send_orders(broker)

    signal_str = _tick_build_signal_log_suffix(composite, gate_result)

    # ── 读 account/positions 缓存 ──
    acct = live_state_get("account", {}, clone=True) or {}
    positions_payload = live_state_get("positions", [], clone=True) or []
    # ★ P0 fix: 统一转 dict — 支持 dataclass / protobuf / 任意非 dict
    _positions_probe = (
        (positions_payload.get("positions", []) or [])
        if isinstance(positions_payload, dict)
        else positions_payload
    )
    if _positions_probe and not isinstance(_positions_probe[0], dict):
        from backend.ws.endpoints import position_to_dict
    else:
        position_to_dict = None
    pos = _tick_normalize_live_positions_payload(
        positions_payload,
        position_to_dict=position_to_dict,
    )
    current_price = float(last_bar["close"])
    signal_decision_id = ""
    if _LEDGER:
        try:
            # Signal events are the root of the durable factor lineage. Bind
            # them to the same runtime selection projection later used by the
            # open-intent record; otherwise the ledger has a contribution
            # snapshot but cannot prove which factor set produced it.
            runtime_binding = live_open_pipeline.open_runtime_factor_lineage()
            factor_set_version = str(
                runtime_binding.get("selection_fingerprint") or ""
            )
            policy_version = str(getattr(cfg, "policy_version", "") or "")
            signal_decision_id = _LEDGER.log_composite_decision(
                event_type="signal",
                composite=composite,
                gate_result=gate_result,
                symbol="XAUUSD+",
                timeframe=str(getattr(cfg, "timeframe", "") or ""),
                decision_ts=bar.get("time", time.time()),
                portfolio_state={
                    "balance": acct.get("balance", 0),
                    "equity": acct.get("equity", 0),
                    "n_positions": len(pos),
                    "session_pnl": live_state_get("session_pnl", 0),
                },
                risk_state=live_state_get("risk", {}, clone=True) or {},
                policy_version=policy_version,
                factor_set_version=factor_set_version,
                action_reason="signal_detected",
                action_json={
                    "tick": tick,
                    "runtime_binding": runtime_binding,
                    "factor_set_version": factor_set_version,
                    "policy_version": policy_version,
                    **_lifecycle_build_open_decision_replay_payload(
                        cfg=cfg,
                        composite=composite,
                        include_risk=False,
                    ),
                },
            )
            pipeline["last_signal_decision_id"] = str(signal_decision_id or "")
        except Exception as _ledger_err:
            logger.warning("[live] ledger signal failed: {}", _ledger_err)
            pipeline["last_signal_decision_id"] = ""
    else:
        pipeline["last_signal_decision_id"] = ""

    # ── 平仓检测: 对比 _prev_position_ids 找出被 broker 关闭的仓位 ──
    current_pids = _tick_collect_position_ids(pos)
    pending_open_attach_ids = _active_pending_open_attach_ids(current_pids)
    attr_engine = pipeline.get("attribution")
    positions_snapshot_ready = bool(live_state_get("positions_updated_at", 0.0))
    closed_pids, current_pids, close_detection_deferred = _tick_resolve_closed_position_ids(
        previous_position_ids=_prev_position_ids,
        current_position_ids=current_pids,
        positions_snapshot_ready=positions_snapshot_ready,
        tracked_position_ids=(
            live_close_settlement.active_recovery_position_ids_for_close_detection(broker)
        ),
    )
    if close_detection_deferred:
        log(f"tick {tick}: positions cache not ready, defer close detection")
    restored_attributions = _restore_attribution_for_positions(attr_engine, pos)
    if restored_attributions:
        log(f"tick {tick}: attribution restored open contexts={restored_attributions}")

    # ── 获取真实 PnL (从 cTrader deals) ──
    _real_pnls: dict[int, dict] = {}
    _close_deal_cursors: dict[int, dict[str, Any]] = {}
    if closed_pids and bridge is not None:
        try:
            _real_pnls = live_close_settlement.sync_closed_position_deals_for_tick(
                bridge,
                closed_pids,
                observed_close_cursor_out=_close_deal_cursors,
            )
        except Exception as _ds_err:
            log(f"tick {tick}: deal_sync error: {_ds_err}")

    _handle_closed_positions_after_tick(
        closed_pids=closed_pids,
        real_pnls=_real_pnls,
        attr_engine=attr_engine,
        bar=bar,
        cfg=cfg,
        acct=acct,
        broker=broker,
        tick=tick,
        log=log,
        broker_open_position_ids=current_pids,
        bridge=bridge,
        close_deal_cursors=_close_deal_cursors,
    )
    # 记录当前仓位 open price (供下次 close 使用)
    for p in pos:
        pid = p.get("position_id") or p.get("ticket")
        if pid is not None and int(pid) not in _pos_open_prices:
            _pos_open_prices[int(pid)] = float(p.get("open_price", current_price))

    # ★ v9-fix: 价格僵死检测 — same price for >30 ticks → DataStore 可能断更
    _price_key = f"{broker}:{getattr(cfg, 'timeframe', '?')}"
    _prev_price = _PRICE_STUCK_WARNED.get(_price_key)
    if _prev_price is not None and abs(current_price - _prev_price) < 0.01:
        _PRICE_STUCK_WARNED[_price_key] = current_price
    else:
        _PRICE_STUCK_WARNED.pop(_price_key, None)  # 价格变了, 解除告警
    # 如果超过 30 tick 没变价就报警 (每 60s 仅报一次)
    if _prev_price is not None and abs(current_price - _prev_price) < 0.01:
        _stuck_count = sum(1 for k, v in list(_PRICE_STUCK_WARNED.items())
                           if k.startswith(f"{broker}:") and abs(v - current_price) < 0.01)
        if _stuck_count >= 30 and _stuck_count % 30 == 0:
            log(f"WARN: price stuck at {current_price:.2f} for {_stuck_count} ticks — "
                f"DataStore may be stale, check CTraderPuller")

    # 价格守卫
    if bridge is not None and hasattr(bridge, "get_spot_quote"):
        price_guard = _tick_guard_current_price_with_spot_quote(
            current_price=current_price,
            get_spot_quote=bridge.get_spot_quote,
            quote_is_fresh=_quote_is_fresh,
        )
        current_price = float(price_guard["current_price"])
        if price_guard["error"] is not None:
            logger.debug("[live] spot price guard failed for tick {}: {}", tick, price_guard["error"])

    # ── 开仓执行流水线: candidate -> risk verdict -> broker order -> post-fill audit.
    atr_val = factor_values.get("atr_ratio", 0)
    atr_price = atr_val * current_price if atr_val and atr_val > 0 else 0
    signal_gate_result = gate_result
    gate_result = live_open_pipeline.run_open_trade_pipeline(
        bridge=bridge,
        pipeline=pipeline,
        broker=broker,
        cfg=cfg,
        bar=bar,
        factor_values=factor_values,
        composite=composite,
        gate_result=gate_result,
        account=acct,
        positions=pos,
        attr_engine=attr_engine,
        current_price=current_price,
        atr_price=atr_price,
        pending_open_attach_ids=pending_open_attach_ids,
        send=send,
        tick=tick,
        log=log,
        signal_decision_id=signal_decision_id,
        stop_requested=stop_requested,
    )
    live_open_pipeline.remember_or_clear_pending_open_retry(
        pipeline=pipeline,
        bar=bar,
        factor_values=factor_values,
        composite=composite,
        signal_gate_result=signal_gate_result,
        open_result=gate_result,
    )

    # ── 日志 ──
    log(f"tick {tick}: price={current_price:.2f} "
        f"balance={acct.get('balance', 0):.2f} "
        f"equity={acct.get('equity', 0):.2f} "
        f"pos={len(pos)} "
        f"pnl_session={live_state_get('session_pnl', 0):.2f}"
        f"{signal_str}")

    # ── 业务告警检查 ──
    _check_business_alerts(tick, acct, pos, log)

    # ── 统一持仓保护仲裁: timeout > governed supervisor > entry repair ──
    if not protection_already_run and pos and bridge is not None and cfg is not None:
        _run_position_protection_cycle(
            bridge,
            pos,
            cfg=cfg,
            acct=acct,
            pipeline=pipeline,
            current_price=current_price,
            atr_price=atr_price,
            tick=tick,
            log=log,
        )

    # ── 更新上一 tick 持仓 ID, 供下次平仓检测 ──
    _prev_position_ids = current_pids

    _publish_latest_price(current_price, source="loop_tick")


def _entry_protection_repair_candidates(
    pos: list,
    *,
    current_price: float,
    tick: int,
    decision_ts: float | None = None,
) -> list[ProtectionCandidate]:
    now_ts = float(decision_ts if decision_ts is not None else time.time())
    candidates: list[ProtectionCandidate] = []
    for p in pos or []:
        if not isinstance(p, dict):
            continue
        try:
            pid = int(p.get("position_id") or p.get("ticket") or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            continue
        row = live_close_settlement.load_recovery_position_row(pid)
        meta = dict((row or {}).get("recovery_meta") or {})
        plan = dict(meta.get("entry_protection_plan") or {})
        if plan.get("schema_version") != _ENTRY_PROTECTION_PLAN_SCHEMA:
            # 恢复仓/计划持久化失败导致的裸仓: 用 preflight 同款回撤距离
            # (atr 缺失时 price*2%/3%)补一份 plan, 让下方修复机制在冷却
            # 约束下自动挂保护; 只处理 broker 侧确无 SL 的仓位。
            if row and _float_payload_value(p, "sl", "stop_loss", "stopLoss") <= 0:
                direction = int(_direction_from_position(p) or 0)
                entry_price = float(p.get("open_price") or current_price or 0.0)
                if direction and entry_price > 0:
                    sl_dist = entry_price * 0.02
                    tp_dist = entry_price * 0.03
                    recovery_plan = _entry_protection_plan_payload(
                        position_id=pid,
                        direction=direction,
                        entry_price=entry_price,
                        target_stop_loss=(
                            entry_price - sl_dist if direction > 0 else entry_price + sl_dist
                        ),
                        target_take_profit=(
                            entry_price + tp_dist if direction > 0 else entry_price - tp_dist
                        ),
                        requested_volume=float(p.get("volume") or 0.0),
                        actual_api_volume=float(p.get("volume") or 0.0),
                        tick=int(tick or 0),
                        status="pending",
                        source="recovered_no_protection",
                    )
                    try:
                        live_close_settlement.merge_recovery_position_meta(pid, {"entry_protection_plan": recovery_plan})
                        plan = dict(recovery_plan)
                    except Exception as exc:
                        logger.error(
                            "[live] recovered protection plan persist failed pos={}: {}",
                            pid,
                            exc,
                        )
            if plan.get("schema_version") != _ENTRY_PROTECTION_PLAN_SCHEMA:
                continue
        target_sl = float(plan.get("target_stop_loss") or 0.0)
        target_tp = float(plan.get("target_take_profit") or 0.0)
        if target_sl <= 0 and target_tp <= 0:
            continue
        last_attempt_ts = float(plan.get("last_attempt_ts") or 0.0)
        if last_attempt_ts > 0 and now_ts - last_attempt_ts < _ENTRY_PROTECTION_REPAIR_COOLDOWN_SECONDS:
            continue
        direction = int(plan.get("direction") or _direction_from_position(p) or 0)
        current_sl = _float_payload_value(p, "sl", "stop_loss", "stopLoss")
        current_tp = _float_payload_value(p, "tp", "take_profit", "takeProfit")
        needs_sl = False
        if target_sl > 0:
            if current_sl <= 0:
                needs_sl = True
            elif direction > 0 and target_sl > current_sl + 0.01:
                needs_sl = True
            elif direction < 0 and target_sl < current_sl - 0.01:
                needs_sl = True
        needs_tp = bool(target_tp > 0 and current_tp <= 0)
        if not needs_sl and not needs_tp:
            if str(plan.get("status") or "") != "applied":
                try:
                    _update_entry_protection_plan_status(
                        pid,
                        status="applied",
                        applied_sl=current_sl,
                        applied_tp=current_tp,
                    )
                except Exception as exc:
                    logger.debug("[live] entry protection applied-state update failed pos={}: {}", pid, exc)
            continue
        anchor = _runtime_config_anchor()
        candidates.append(
            ProtectionCandidate(
                source=_ENTRY_PROTECTION_REPAIR_SOURCE,
                action="repair_entry_protection",
                priority=10,
                position_id=pid,
                risk_action="tighten_position",
                controls={
                    "target_stop_loss": round(target_sl, 2) if target_sl > 0 else 0.0,
                    "target_take_profit": round(target_tp, 2) if target_tp > 0 else 0.0,
                    "close_reason": _ENTRY_PROTECTION_REPAIR_SOURCE,
                    "protection_mode": "entry_sltp_repair",
                },
                evidence={
                    "tick": int(tick or 0),
                    "current_price": round(float(current_price or 0.0), 2),
                    "current_sl": round(float(current_sl or 0.0), 2),
                    "current_tp": round(float(current_tp or 0.0), 2),
                    "target_sl": round(float(target_sl or 0.0), 2),
                    "target_tp": round(float(target_tp or 0.0), 2),
                    "needs_sl": needs_sl,
                    "needs_tp": needs_tp,
                    "plan_status": str(plan.get("status") or ""),
                    "plan_attempts": int(plan.get("attempts") or 0),
                    "confidence": 1.0,
                },
                reason="entry_protection_missing_on_broker",
                position=dict(p),
                config_version=int(anchor.get("config_version") or 0),
                config_hash=str(anchor.get("config_hash") or ""),
            )
        )
    return candidates


def _log_protection_execution_payloads(
    *,
    position: dict[str, Any],
    verdict_payload: dict[str, Any],
    cfg: Any,
    tick: int,
    result_payloads: dict[str, Any],
    acct: dict | None,
    log_position_event: bool = True,
) -> None:
    if log_position_event and result_payloads.get("position_event_type"):
        _log_supervisor_position_event(
            position=position,
            event_type=result_payloads["position_event_type"],
            details=result_payloads["position_event_details"],
        )
    _log_supervisor_trace(
        position=position,
        verdict=verdict_payload,
        cfg=cfg,
        tick=tick,
        **result_payloads["trace_fields"],
        acct=acct,
    )


def _handle_protection_execution_skip(
    *,
    candidate: ProtectionCandidate,
    position: dict[str, Any],
    verdict_payload: dict[str, Any],
    risk_verdict: dict[str, Any],
    decision_id: str,
    candidate_payload: dict[str, Any],
    sl_plan: dict[str, Any],
    cfg: Any,
    tick: int,
    log,
    acct: dict | None,
) -> bool:
    result_payloads = _lifecycle_build_protection_execution_result_payloads(
        result="skipped",
        source=candidate.source,
        action=candidate.action,
        reason=candidate.reason,
        risk_action=candidate.risk_action,
        risk_verdict=risk_verdict,
        decision_id=decision_id,
        candidate_payload=candidate_payload,
        sl_plan=sl_plan,
        controls=candidate.controls,
    )
    _log_protection_execution_payloads(
        position=position,
        verdict_payload=verdict_payload,
        cfg=cfg,
        tick=tick,
        result_payloads=result_payloads,
        acct=acct,
    )
    log(f"tick {tick}: protection {candidate.source} SKIP pos={candidate.position_id} reason={sl_plan.get('reason')}")
    return True


def _mark_entry_protection_plan_after_execution(
    *,
    candidate: ProtectionCandidate,
    pid: int,
    status: str,
    attempted: bool,
    applied_sl: float = 0.0,
    applied_tp: float = 0.0,
    error: str = "",
) -> None:
    if candidate.source != _ENTRY_PROTECTION_REPAIR_SOURCE:
        return
    try:
        _update_entry_protection_plan_status(
            pid,
            status=status,
            attempted=attempted,
            applied_sl=applied_sl,
            applied_tp=applied_tp,
            error=error,
        )
    except Exception as exc:
        logger.debug("[live] entry protection {} update failed pos={}: {}", status, pid, exc)


def _handle_protection_execution_applied(
    *,
    candidate: ProtectionCandidate,
    position: dict[str, Any],
    verdict_payload: dict[str, Any],
    risk_verdict: dict[str, Any],
    decision_id: str,
    candidate_payload: dict[str, Any],
    sl_plan: dict[str, Any],
    target_sl: float,
    planned_sl: float,
    current_tp: float,
    cfg: Any,
    tick: int,
    log,
    acct: dict | None,
) -> bool:
    pid = int(candidate.position_id or 0)
    _track_local_sl_tp(pid, sl=planned_sl, tp=current_tp)
    _mark_entry_protection_plan_after_execution(
        candidate=candidate,
        pid=pid,
        status="applied",
        attempted=True,
        applied_sl=planned_sl,
        applied_tp=current_tp,
    )
    _remember_protection_state(
        position,
        verdict_payload,
        source=candidate.source,
        action_applied=candidate.action,
        broker="ctrader",
        strategy_name=_current_loop_strategy_name(),
    )
    result_payloads = _lifecycle_build_protection_execution_result_payloads(
        result="applied",
        source=candidate.source,
        action=candidate.action,
        reason=candidate.reason,
        risk_action=candidate.risk_action,
        risk_verdict=risk_verdict,
        decision_id=decision_id,
        candidate_payload=candidate_payload,
        sl_plan=sl_plan,
        controls=candidate.controls,
        target_stop_loss_original=target_sl,
        target_stop_loss_sent=planned_sl,
        target_take_profit_sent=current_tp,
    )
    _log_protection_execution_payloads(
        position=position,
        verdict_payload=verdict_payload,
        cfg=cfg,
        tick=tick,
        result_payloads=result_payloads,
        acct=acct,
    )
    log(f"tick {tick}: protection {candidate.source} pos={pid} sl->{planned_sl:.2f} tp->{current_tp:.2f}")
    return True


def _handle_protection_execution_failed(
    *,
    candidate: ProtectionCandidate,
    position: dict[str, Any],
    verdict_payload: dict[str, Any],
    risk_verdict: dict[str, Any],
    decision_id: str,
    candidate_payload: dict[str, Any],
    sl_plan: dict[str, Any],
    reason: str,
    cfg: Any,
    tick: int,
    log,
    acct: dict | None,
) -> bool:
    pid = int(candidate.position_id or 0)
    _mark_entry_protection_plan_after_execution(
        candidate=candidate,
        pid=pid,
        status="failed",
        attempted=True,
        error=reason,
    )
    result_payloads = _lifecycle_build_protection_execution_result_payloads(
        result="failed",
        source=candidate.source,
        action=candidate.action,
        reason=candidate.reason,
        risk_action=candidate.risk_action,
        risk_verdict=risk_verdict,
        decision_id=decision_id,
        candidate_payload=candidate_payload,
        sl_plan=sl_plan,
        controls=candidate.controls,
        failure_reason=reason,
    )
    _log_protection_execution_payloads(
        position=position,
        verdict_payload=verdict_payload,
        cfg=cfg,
        tick=tick,
        result_payloads=result_payloads,
        acct=acct,
    )
    log(f"tick {tick}: protection {candidate.source} AMEND FAILED pos={pid}: {reason}")
    return True


def _prepare_protection_candidate_execution(
    *,
    candidate: ProtectionCandidate,
    bridge: Any,
    cfg: Any,
    tick: int,
    acct: dict | None,
) -> dict[str, Any] | None:
    position = dict(candidate.position or {})
    pid = int(candidate.position_id or 0)
    if pid <= 0 or not position:
        return None
    verdict_payload = _candidate_verdict(candidate)
    close_context = _build_close_position_risk_context(
        position_id=pid,
        close_reason=str(candidate.controls.get("close_reason") or candidate.source),
        mode="live",
        broker="ctrader",
        symbol=str(position.get("symbol") or "XAUUSD+"),
        position=position,
        cfg=cfg,
        broker_schedule=_broker_schedule_from_bridge(bridge),
    )
    risk_context = _lifecycle_build_protection_candidate_risk_context_from_candidate(
        close_context=close_context,
        position=position,
        candidate=candidate,
        loop_running=bool(live_state_get("loop_running", True)),
        bridge_connected=bool(getattr(bridge, "is_connected", False)),
    )
    risk_verdict = _evaluate_risk_reduction_policy(candidate.risk_action, risk_context).to_dict()
    decision_id = _log_supervisor_decision(
        position=position,
        verdict=verdict_payload,
        risk_verdict=risk_verdict,
        acct=acct,
        cfg=cfg,
        event_type=candidate.source,
        tick=tick,
    )
    return {
        "position": position,
        "pid": pid,
        "verdict_payload": verdict_payload,
        "risk_verdict": risk_verdict,
        "decision_id": decision_id,
        "candidate_payload": asdict(candidate),
    }


def _execute_protection_candidate(
    candidate: ProtectionCandidate,
    *,
    bridge,
    cfg,
    tick: int,
    log,
    acct: dict | None = None,
) -> bool:
    # AWE trailing is historical evidence only. Keep an explicit guard at
    # the last generic candidate boundary so a stale caller cannot create a
    # new decision/trace or reach RiskPolicy/broker execution.
    if candidate.source == "legacy_awe_trailing":
        log(f"tick {tick}: retired protection candidate ignored pos={candidate.position_id}")
        return False
    prepared = _prepare_protection_candidate_execution(
        candidate=candidate,
        bridge=bridge,
        cfg=cfg,
        tick=tick,
        acct=acct,
    )
    if not prepared:
        return False
    position = prepared["position"]
    pid = int(prepared["pid"])
    verdict_payload = prepared["verdict_payload"]
    risk_verdict = prepared["risk_verdict"]
    decision_id = prepared["decision_id"]
    candidate_payload = prepared["candidate_payload"]
    if not risk_verdict.get("allowed", False):
        result_payloads = _lifecycle_build_protection_execution_result_payloads(
            result="risk_rejected",
            source=candidate.source,
            action=candidate.action,
            reason=candidate.reason,
            risk_action=candidate.risk_action,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            candidate_payload=candidate_payload,
        )
        _log_protection_execution_payloads(
            position=position,
            verdict_payload=verdict_payload,
            cfg=cfg,
            tick=tick,
            result_payloads=result_payloads,
            acct=acct,
            log_position_event=False,
        )
        return True

    quote = bridge.get_spot_quote() if hasattr(bridge, "get_spot_quote") else {}
    execution_plan = _lifecycle_build_protection_execution_plan(
        position=position,
        controls=candidate.controls,
        source=candidate.source,
        entry_protection_repair_source=_ENTRY_PROTECTION_REPAIR_SOURCE,
        quote=quote,
    )
    target_sl = float(execution_plan.get("target_sl") or 0.0)
    current_tp = float(execution_plan.get("current_tp") or 0.0)
    planned_sl = float(execution_plan.get("planned_sl") or 0.0)
    sl_plan = execution_plan.get("sl_plan") or {}
    if not sl_plan["allowed"]:
        return _handle_protection_execution_skip(
            candidate=candidate,
            position=position,
            verdict_payload=verdict_payload,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            candidate_payload=candidate_payload,
            sl_plan=sl_plan,
            cfg=cfg,
            tick=tick,
            log=log,
            acct=acct,
        )

    try:
        amend_res = bridge.amend_position_sltp(pid, sl=planned_sl, tp=current_tp)
    except Exception as exc:
        amend_res = type("AmendResult", (), {"success": False, "comment": str(exc)})()
    if getattr(amend_res, "success", False):
        projection = _explicit_position_reconcile(bridge)
        verification = _verify_position_protection_projection(
            projection,
            position_id=pid,
            expected_stop_loss=planned_sl,
            expected_take_profit=current_tp,
            precision=int(position.get("digits", 2) or 2),
        )
        if bool(verification.get("ok")):
            _publish_fresh_position_reconcile(projection, broker="ctrader")
            return _handle_protection_execution_applied(
                candidate=candidate,
                position=position,
                verdict_payload=verdict_payload,
                risk_verdict=risk_verdict,
                decision_id=decision_id,
                candidate_payload=candidate_payload,
                sl_plan=sl_plan,
                target_sl=target_sl,
                planned_sl=planned_sl,
                current_tp=current_tp,
                cfg=cfg,
                tick=tick,
                log=log,
                acct=acct,
            )

        projection_reason = str(
            verification.get("reason") or "position_reconcile_failed"
        )
        failure_reason = f"amend_projection_unverified:{projection_reason}"
        live_close_settlement.record_risk_reduction_aux_failure(
            "protection_amend_projection_unverified",
            position_id=pid,
            action=candidate.action,
            error=failure_reason,
            payload={
                "source": candidate.source,
                "verification": verification,
            },
        )
        live_safety_watchdog.persist_safety_fail_closed(
            blockers=("amend_projection_unverified",),
            source="protection_amend",
            error=failure_reason,
        )
        return _handle_protection_execution_failed(
            candidate=candidate,
            position=position,
            verdict_payload=verdict_payload,
            risk_verdict=risk_verdict,
            decision_id=decision_id,
            candidate_payload=candidate_payload,
            sl_plan=sl_plan,
            reason=failure_reason,
            cfg=cfg,
            tick=tick,
            log=log,
            acct=acct,
        )
    reason = str(getattr(amend_res, "comment", "") or getattr(amend_res, "error", "") or "amend_failed")
    return _handle_protection_execution_failed(
        candidate=candidate,
        position=position,
        verdict_payload=verdict_payload,
        risk_verdict=risk_verdict,
        decision_id=decision_id,
        candidate_payload=candidate_payload,
        sl_plan=sl_plan,
        reason=reason,
        cfg=cfg,
        tick=tick,
        log=log,
        acct=acct,
    )


_MARKET_CLOSED_ERROR_PATTERNS = (
    "MARKET_CLOSED",
    "OFF_QUOTES",
    "NO_QUOTES",
    "MARKET IS CLOSED",
)

# Recovery-meta keys shared by every risk-reducing close path: the first
# deterministic MARKET_CLOSED-style rejection records them, and repeats stay
# suppressed until the hourly heartbeat falls due (then one bounded attempt
# re-checks the broker).  Broker-side SL/TP protection stays active meanwhile.
MARKET_CLOSED_DEFER_REASON_KEY = "market_closed_defer_reason"
MARKET_CLOSED_DEFER_TS_KEY = "market_closed_defer_ts"
MARKET_CLOSED_DEFER_HEARTBEAT_SECONDS = 3600.0


def is_deterministic_market_closed_rejection(reason: str) -> bool:
    text = str(reason or "").upper()
    return any(pattern in text for pattern in _MARKET_CLOSED_ERROR_PATTERNS)


def market_closed_deferral_active(recovery_meta: Mapping[str, Any], now_ts: float) -> bool:
    """True when a recent deterministic market-closed rejection is suppressing retries."""

    meta = dict(recovery_meta or {})
    if str(meta.get(MARKET_CLOSED_DEFER_REASON_KEY) or "") != "market_closed_pending":
        return False
    try:
        last_ts = float(meta.get(MARKET_CLOSED_DEFER_TS_KEY, 0.0) or 0.0)
        elapsed = float(now_ts) - last_ts
    except (TypeError, ValueError):
        return False
    return last_ts > 0 and 0.0 <= elapsed < MARKET_CLOSED_DEFER_HEARTBEAT_SECONDS


def _defer_market_closed_holding_timeout(
    *,
    position: dict,
    pid: int,
    cfg,
    tick: int,
    now_ts: float,
    holding_seconds: float,
    max_holding_seconds: float,
    market_open_holding: float,
) -> None:
    """Trace a timeout deferral once per closed-market episode.

    The verdict stays "close when the market reopens"; only the futile
    per-tick repetition is suppressed.  A single trace is emitted when the
    deferral starts, then again only if the wall-clock holding time grows by
    more than an hour (a bounded heartbeat) — never one row per tick.
    """

    verdict_payload = _lifecycle_build_holding_timeout_verdict_payload(
        position_id=pid,
        decision_ts=now_ts,
        holding_seconds=holding_seconds,
        max_holding_seconds=max_holding_seconds,
    )
    meta = dict(
        (_load_recovery_row_for_risk_reduction(pid, operation="timeout_defer") or {}).get(
            "recovery_meta"
        )
        or {}
    )
    if market_closed_deferral_active(meta, now_ts):
        return
    execution = {
        "close_deferred": True,
        "defer_reason": "market_closed_pending",
        "wall_clock_holding_seconds": round(holding_seconds, 3),
        "market_open_holding_seconds": round(market_open_holding, 3),
        "max_holding_seconds": round(max_holding_seconds, 3),
        "applied_controls": {"close_reason": "holding_timeout"},
        "duplicate_audit": False,
    }
    _log_supervisor_trace(
        position=position,
        verdict=verdict_payload,
        cfg=cfg,
        tick=tick,
        stage="execution_deferred",
        outcome="skipped",
        risk_action="close_position",
        execution_status="deferred",
        execution_reason="market_closed_pending",
        execution=execution,
    )
    try:
        live_close_settlement.merge_recovery_position_meta(
            pid,
            {
                MARKET_CLOSED_DEFER_REASON_KEY: "market_closed_pending",
                MARKET_CLOSED_DEFER_TS_KEY: now_ts,
                "market_closed_defer_wall_holding_seconds": round(holding_seconds, 3),
                "market_closed_defer_open_holding_seconds": round(market_open_holding, 3),
            },
        )
    except Exception as exc:
        live_close_settlement.record_risk_reduction_aux_failure(
            "risk_reduction_state_persist_failed",
            position_id=pid,
            action="holding_timeout_defer",
            error=exc,
        )


def _enforce_holding_timeout(
    bridge,
    pos: list,
    *,
    cfg,
    tick: int,
    log,
    decision_ts: float | None = None,
    candidate_recorder=None,
) -> set[int]:
    handled: set[int] = set()
    max_holding_bars = int(getattr(cfg, "risk_max_holding_bars", 0) or 0)
    if max_holding_bars <= 0:
        return handled

    now_ts = float(decision_ts if decision_ts is not None else time.time())
    for p in pos or []:
        try:
            pid = int(p.get("position_id") or p.get("ticket") or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            continue

        close_context = _build_close_position_risk_context(
            position_id=pid,
            close_reason="holding_timeout",
            mode="live",
            broker="ctrader",
            symbol=str(p.get("symbol") or "XAUUSD+"),
            position=p,
            cfg=cfg,
            decision_ts=now_ts,
            broker_schedule=_broker_schedule_from_bridge(bridge),
        )
        max_holding_seconds = float(close_context.get("max_holding_seconds", 0.0) or 0.0)
        holding_seconds = float(close_context.get("holding_seconds", 0.0) or 0.0)

        # A previously recorded market-closed deferral/rejection stays
        # suppressed until its hourly heartbeat falls due; the position keeps
        # its broker-side SL/TP protection meanwhile.
        defer_meta = dict(
            (
                _load_recovery_row_for_risk_reduction(
                    pid, operation="timeout_defer"
                )
                or {}
            ).get("recovery_meta")
            or {}
        )
        if market_closed_deferral_active(defer_meta, now_ts):
            handled.add(pid)
            continue

        # Timeout is a wall-clock budget, but the close must be executable to
        # be a protection.  A position whose wall-clock holding time crossed
        # the limit only because the market was closed for part of the window
        # is deferred, not closed: every attempt during the closure is
        # deterministically rejected (MARKET_CLOSED) and would otherwise
        # retry every tick until reopen.
        market_budget = dict(close_context.get("market_time_budget") or {})
        entry_ts_for_budget = float(close_context.get("entry_ts", 0.0) or 0.0)
        market_open_holding = float(
            market_budget.get("market_open_holding_seconds", 0.0) or 0.0
        )
        if not market_budget and entry_ts_for_budget > 0:
            market_open_holding = _lifecycle_market_open_seconds_between(
                entry_ts_for_budget,
                now_ts,
                symbol=str(p.get("symbol") or "XAUUSD+"),
                broker_schedule=_broker_schedule_from_bridge(bridge),
            )
        if (
            market_budget.get("market_closed_pending")
            and market_open_holding < max_holding_seconds
        ):
            _defer_market_closed_holding_timeout(
                position=dict(p),
                pid=pid,
                cfg=cfg,
                tick=tick,
                now_ts=now_ts,
                holding_seconds=holding_seconds,
                max_holding_seconds=max_holding_seconds,
                market_open_holding=market_open_holding,
            )
            handled.add(pid)
            continue

        if not _lifecycle_holding_timeout_is_expired(close_context):
            continue

        if candidate_recorder is not None:
            candidate_recorder(
                safety_candidate(
                    action="timeout",
                    position_id=pid,
                    source="holding_timeout",
                    controls={"close_reason": "holding_timeout"},
                )
            )

        close_verdict = _evaluate_risk_reduction_policy("close_position", close_context)
        verdict_payload = _lifecycle_build_holding_timeout_verdict_payload(
            position_id=pid,
            decision_ts=now_ts,
            holding_seconds=holding_seconds,
            max_holding_seconds=max_holding_seconds,
        )
        decision_id = _log_supervisor_decision(
            position=dict(p),
            verdict=verdict_payload,
            risk_verdict=close_verdict.to_dict(),
            acct=None,
            cfg=cfg,
            event_type="holding_timeout",
            tick=tick,
        )
        if not close_verdict.allowed:
            logger.warning("[live] holding timeout close blocked pos={} reason={}", pid, close_verdict.reason)
            _log_supervisor_trace(
                position=dict(p),
                verdict=verdict_payload,
                cfg=cfg,
                tick=tick,
                **_lifecycle_build_holding_timeout_result_trace_fields(
                    result="risk_rejected",
                    decision_id=decision_id,
                    risk_verdict=close_verdict.to_dict(),
                    execution_reason=close_verdict.reason,
                ),
            )
            handled.add(pid)
            continue
        try:
            # ``pos`` is the broker snapshot consumed by the safety cycle.
            # Pass its API volume so a transient auxiliary reconcile failure
            # cannot suppress the close-only timeout escape hatch.
            result = bridge.close_position(
                pid,
                volume=float(_position_api_volume(p) or 0.0),
            )
        except Exception as exc:
            logger.warning("[live] holding timeout close exception pos={}: {}", pid, exc)
            _log_supervisor_trace(
                position=dict(p),
                verdict=verdict_payload,
                cfg=cfg,
                tick=tick,
                **_lifecycle_build_holding_timeout_result_trace_fields(
                    result="exception",
                    decision_id=decision_id,
                    risk_verdict=close_verdict.to_dict(),
                    execution_reason=str(exc),
                ),
            )
            handled.add(pid)
            continue
        if getattr(result, "success", False):
            live_close_settlement.remember_close_reason(pid, "holding_timeout")
            live_close_settlement.remember_close_verdict(pid, close_verdict)
            handled.add(pid)
            _log_supervisor_trace(
                position=dict(p),
                verdict=verdict_payload,
                cfg=cfg,
                tick=tick,
                **_lifecycle_build_holding_timeout_result_trace_fields(
                    result="applied",
                    decision_id=decision_id,
                    risk_verdict=close_verdict.to_dict(),
                ),
            )
            log(
                f"tick {tick}: holding timeout close sent pos={pid} "
                f"held={holding_seconds:.0f}s limit={max_holding_seconds:.0f}s"
            )
        else:
            handled.add(pid)
            failure_reason = str(
                getattr(result, "comment", "") or getattr(result, "error", "") or "close_failed"
            )
            error_code = str(getattr(result, "error_code", "") or "")
            rejection_text = f"{error_code} {failure_reason}"
            if is_deterministic_market_closed_rejection(rejection_text):
                try:
                    live_close_settlement.merge_recovery_position_meta(
                        pid,
                        {
                            MARKET_CLOSED_DEFER_REASON_KEY: "market_closed_pending",
                            MARKET_CLOSED_DEFER_TS_KEY: now_ts,
                            "market_closed_close_rejection": rejection_text[:300],
                        },
                    )
                except Exception as exc:
                    live_close_settlement.record_risk_reduction_aux_failure(
                        "risk_reduction_state_persist_failed",
                        position_id=pid,
                        action="holding_timeout_market_closed_rejection",
                        error=exc,
                    )
            _log_supervisor_trace(
                position=dict(p),
                verdict=verdict_payload,
                cfg=cfg,
                tick=tick,
                **_lifecycle_build_holding_timeout_result_trace_fields(
                    result="failed",
                    decision_id=decision_id,
                    risk_verdict=close_verdict.to_dict(),
                    execution_reason=str(getattr(result, "comment", "") or getattr(result, "error", "") or "close_failed"),
                ),
            )
    return handled


def _run_position_protection_cycle(
    bridge,
    pos: list,
    *,
    cfg,
    acct: dict,
    pipeline: dict,
    current_price: float,
    atr_price: float,
    tick: int,
    log,
    decision_ts: float | None = None,
) -> dict[str, Any]:
    runtime = PositionProtectionCycleRuntime(
        enforce_holding_timeout=_enforce_holding_timeout,
        entry_protection_repair_candidates=_entry_protection_repair_candidates,
        log_candidate_superseded=_log_protection_candidate_superseded,
        execute_candidate=_execute_protection_candidate,
        run_position_supervision=_run_position_supervision,
        protection_candidate_to_safety=protection_candidate_to_safety,
        build_cycle_result=_lifecycle_build_position_protection_cycle_result,
        record_aux_failure=live_close_settlement.record_risk_reduction_aux_failure,
        warning=logger.warning,
        now=time.time,
    )
    return _runtime_run_position_protection_cycle(
        bridge,
        pos,
        cfg=cfg,
        account=acct,
        pipeline=pipeline,
        current_price=current_price,
        atr_price=atr_price,
        tick=tick,
        log=log,
        runtime=runtime,
        decision_ts=decision_ts,
    )


# ── 业务告警 ─────────────────────────────────────────────

# 告警边沿状态 (2026-08-25): session_max_drawdown_pct 是当日只涨不降的水位,
# consec 在持仓未平前也不变, 按 tick % 10 重发会把同一条告警每 5 分钟刷一遍
# 直到日切。改为边沿触发: 状态恶化跨过阈值那一刻发一次, 回落后重新武装。
_business_alert_armed: dict[str, bool] = {}
_BUSINESS_ALERT_ARM_LOCK = threading.Lock()


def _business_alert_should_send(key: str, active: bool) -> bool:
    """Edge trigger: True only when ``active`` just turned on since last call."""
    with _BUSINESS_ALERT_ARM_LOCK:
        was_active = _business_alert_armed.get(key, False)
        _business_alert_armed[key] = active
        return active and not was_active


def _check_business_alerts(tick: int, acct: dict, pos: list, log) -> None:
    """每 tick 检查业务告警规则, 通过 Alerter 发送。

    规则 (全部边沿触发, 状态恶化才发一次, 回落重新武装):
      1. 连亏 ≥ 3 笔 → WARNING
      2. 当日回撤 ≥ 3% → WARNING, ≥ 5% → ERROR
      3. 熔断触发 → CRITICAL (已在 circuit 逻辑中触发, 此处仅补发)
    """
    try:
        from monitor.alerter import Alerter
        _alerter = Alerter({"log_file": "logs/alerts.log", "min_level": "WARNING"})

        # 规则 1: 连亏 — 边沿触发且按笔数升级: 进入连亏状态发一次,
        # 连亏加深(3→4→5)每档再发一次; 回落到 <3 后重新武装。
        consec = int(live_state_get("session_consecutive_loss", 0))
        if consec >= 3:
            with _BUSINESS_ALERT_ARM_LOCK:
                _last_notified = _business_alert_armed.get(
                    "consecutive_loss_value"
                )
                _business_alert_armed["consecutive_loss_value"] = consec
            if _last_notified != consec:
                _alerter.send("WARNING", f"⚠️ 连续亏损 {consec} 笔",
                              f"Tick: {tick}\nConsecutive Loss: {consec}\n"
                              f"Session PnL: ${live_state_get('session_pnl', 0):.2f}")
                log(f"[alerts] consecutive-loss warning sent: streak={consec}")
        else:
            with _BUSINESS_ALERT_ARM_LOCK:
                _business_alert_armed.pop("consecutive_loss_value", None)

        # 规则 2: 当日回撤 — 水位只涨不降, 按水位抬升分级:
        #   首次越过 5% → ERROR; 已在 ERROR 区间内继续抬高不重发;
        #   回落到 <3% 后重新武装。
        dd_pct = float(live_state_get("session_max_drawdown_pct", 0))
        balance = float(acct.get("balance", 0))
        if dd_pct >= 5.0:
            # 水位进入 ERROR 区: 发一次升级告警, 并解除 WARNING 武装
            # (水位当日不回落, 此分支后不会再发)
            if _business_alert_should_send("dd_error", True):
                _alerter.send("ERROR", f"🔴 当日回撤 {dd_pct:.1f}%",
                              f"Tick: {tick}\nDrawdown: {dd_pct:.1f}%\n"
                              f"Balance: ${balance:.2f}\n"
                              f"Session PnL: ${live_state_get('session_pnl', 0):.2f}")
                log(f"[alerts] drawdown ERROR sent: dd={dd_pct:.1f}%")
            _business_alert_should_send("dd_warn", False)
        elif 3.0 <= dd_pct < 5.0 and _business_alert_should_send("dd_warn", True):
            _alerter.send("WARNING", f"⚠️ 当日回撤 {dd_pct:.1f}%",
                          f"Tick: {tick}\nDrawdown: {dd_pct:.1f}%\n"
                          f"Balance: ${balance:.2f}")
            log(f"[alerts] drawdown WARNING sent: dd={dd_pct:.1f}%")
        elif dd_pct < 3.0:
            # 回落到安全区, 重新武装两条回撤告警
            _business_alert_should_send("dd_error", False)
            _business_alert_should_send("dd_warn", False)

        # 规则 3: 熔断确认
        if _business_alert_should_send(
            "circuit_breaker",
            bool(live_state_get("circuit_breaker")),
        ):
            reason = live_state_get("circuit_reason", "unknown")
            _alerter.send("CRITICAL", "🔴 熔断触发",
                          f"Tick: {tick}\nReason: {reason}\n"
                          f"Session PnL: ${live_state_get('session_pnl', 0):.2f}")

        # 每 50 tick 输出执行质量摘要
        if tick > 0 and tick % 50 == 0:
            summary = _exec_quality.summary()
            if _exec_quality.report().get("n_filled", 0) > 0:
                log(f"tick {tick}: {summary}")

    except Exception as _e:
        logger.debug("[live] _check_business_alerts failed: {}", _e)

# Live state writer hooks (registered once at import): WS wake after writes and
# pending-close bookkeeping owned by the settlement module.
live_state_store.set_update_hooks(
    pre_update=live_close_settlement.consume_pending_close_kwargs,
    post_update=_notify_live_state_change,
)