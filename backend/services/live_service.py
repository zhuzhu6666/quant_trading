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
import json
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
from backend.services.live_safety_plane import LiveSafetyPlane, SafetyCandidate
from backend.services.live_safety_planner import (
    SafetyPlannerRuntime,
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
    safety_v2_forced_shadow_status,
)
from backend.services.live_safety_shadow_observation import (
    build_safety_shadow_observer,
    safety_shadow_gate_status,
)
from backend.services.live_safety_watchdog import (
    LiveSafetyWatchdog,
    SafetyFreshnessResult,
    evaluate_safety_freshness,
)
from backend.core.db import state_table_columns
from backend.services.canonical_v2_reader import (
    canonical_ready,
    iter_decision_rows,
    iter_review_rows,
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
    classify_decision_bar_freshness as _sync_classify_decision_bar_freshness,
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
    adaptive_weight_config as _loop_adaptive_weight_config,
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
from backend.services.supervisor_payload_contract import (
    compact_supervisor_mapping as _lifecycle_compact_supervisor_mapping,
)
from backend.services.position_metrics import normalize_path_state, update_position_path_metrics
from backend.services.position_supervisor import (
    evaluate_position_supervisor,
    is_hard_supervisor_action,
)
from backend.services.stability import record_timed
_LEDGER: DecisionLedger | None = None
_TRADE_REVIEWER: TradeReviewer | None = None
_EXPERIENCE_BUILDER: ExperienceBuilder | None = None
_POLICY_SUGGESTER: PolicySuggester | None = None
_POSITION_QUALITY_ADVISOR: Any = None
_OPEN_QUALITY_ADVISOR: Any = None
_RISK_POLICY = RiskPolicyService.shared()
_RUNTIME_KV_PENDING_PATH = Path("data/charts/runtime_kv.pending.jsonl")
_RUNTIME_KV_PENDING_LOCK = threading.Lock()
_ENTRY_CLUSTER_POLICY_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": {}}
_ENTRY_CLUSTER_POLICY_CACHE_LOCK = threading.Lock()
_EVENT_WINDOW_POLICY_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": {}}
_EVENT_WINDOW_POLICY_CACHE_LOCK = threading.Lock()
_ENTRY_QUALITY_POLICY_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": {}}
_ENTRY_QUALITY_POLICY_CACHE_LOCK = threading.Lock()


def _append_runtime_kv_pending(key: str, value, error: str = "") -> None:
    _RUNTIME_KV_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "queued_at": time.time(),
        "error": str(error or ""),
        "key": str(key),
        "value": value,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _RUNTIME_KV_PENDING_LOCK:
        with _RUNTIME_KV_PENDING_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _rewrite_runtime_kv_pending_unlocked(lines: list[str]) -> None:
    if lines:
        tmp_path = _RUNTIME_KV_PENDING_PATH.with_suffix(".pending.tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp_path.replace(_RUNTIME_KV_PENDING_PATH)
    else:
        _RUNTIME_KV_PENDING_PATH.unlink(missing_ok=True)

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
class ProtectionCandidate:
    source: str
    action: str
    priority: int
    position_id: int
    risk_action: str
    controls: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    position: dict[str, Any] = field(default_factory=dict)
    config_version: int = 0
    config_hash: str = ""


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

_RUNTIME_KV_LOOP_DESIRED = "live.loop.desired_state"
_RUNTIME_KV_LAST_SHUTDOWN = "live.loop.last_shutdown"
_RUNTIME_KV_SESSION_STATE_PREFIX = "live.session_state."
_RECOVERY_CONTEXT_PARTIAL = "partial"
_RECOVERY_CONTEXT_FULL = "full"
_RECOVERY_REPLAY_LOOKBACK_SEC = 7 * 24 * 3600
_RECOVERY_ZERO_CONFIRMATIONS_REQUIRED = 2
_recovery_zero_confirmations: dict[str, int] = {}
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
    kelly_data = _live_state_get("risk", {}, clone=True).get("kelly", {})
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
    recovery_row = _load_recovery_position_row(int(position_id))
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
        state_get=_live_state_get,
        collect_runtime_health=partial(
            _loop_collect_open_risk_runtime_health,
            decision_freshness_provider=partial(_live_state_get, "decision_bar_freshness", {}, clone=True),
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
        loss_streak_ladder_facts=_loss_streak_ladder_facts,
    )


def _event_filter_context_for_risk_policy(
    *,
    cfg,
    direction: int,
    bar: dict[str, Any],
    factor_values: dict[str, Any],
) -> dict[str, Any]:
    gate_config = _loop_execution_gate_config(cfg)
    if not (
        bool(gate_config.get("risk_enable_nfp_skip", False))
        or bool(gate_config.get("strategy_enable_nfp_skip", False))
        or bool(gate_config.get("risk_enable_gvz_gate", False))
        or bool(gate_config.get("strategy_enable_gvz_gate", False))
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
    state = _live_state_get("risk", {}, clone=True) or {}
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
    quote = _live_state_get("spot_quote", None, clone=True) or {}
    if bridge is not None and hasattr(bridge, "get_spot_quote"):
        try:
            fresh_quote = bridge.get_spot_quote() or {}
            if fresh_quote:
                quote = fresh_quote
                _live_state_update(spot_quote=fresh_quote)
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
    if not policy:
        return {"passed": True, "reason": "model_open_influence_inactive"}
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
    score = _OPEN_QUALITY_ADVISOR.score_open_context({
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
    }, artifact_path=str(policy.get("artifact_path") or ""))
    subject_id = f"XAUUSD+:{int(float(bar.get('time') or time.time()))}:{direction}"
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
) -> dict[str, Any]:
    runtime = OpenLearningContextRuntime(
        build_entry_cluster_context=_build_entry_cluster_context,
        market_micro_context_snapshot=_market_micro_context_snapshot,
        state_get=_live_state_get,
        build_entry_timing_context=build_entry_timing_context,
        build_payload=_lifecycle_build_open_learning_context_payload,
        tracked_total_api_volume=_tracked_total_api_volume,
        now=time.time,
    )
    return _runtime_build_open_learning_context(
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
    )


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
        lookup_open_decision_context=_lookup_open_decision_context,
        temporal_context_for_trade=_temporal_context_for_trade,
        build_close_context_payload=(
            _lifecycle_build_close_position_risk_context_payload
        ),
        load_recovery_position_row=_load_recovery_position_row,
        lookup_entry_decision_id=_lookup_entry_decision_id,
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


def _record_risk_reduction_aux_failure(
    event_type: str,
    *,
    position_id: int = 0,
    action: str = "",
    error: Exception | str,
    payload: dict[str, Any] | None = None,
) -> None:
    _risk_reduction_record_aux_failure(
        event_type,
        position_id=position_id,
        action=action,
        error=error,
        payload=payload,
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


def _recovery_position_store() -> RecoveryPositionStore:
    return RecoveryPositionStore(
        RecoveryPositionStoreRuntime(
            get_read_connection=_get_state_read_conn,
            get_write_connection=_get_state_pg_conn,
            execute=_state_execute,
            normalize_position=_normalize_position_snapshot,
            normalize_row=_lifecycle_normalize_recovery_position_row,
            lookup_entry_decision_id=_lookup_entry_decision_id,
            build_meta_update_payload=_lifecycle_build_recovery_meta_update_payload,
            build_closed_update_payload=_lifecycle_build_recovery_closed_update_payload,
            now=time.time,
            local_open_volumes=_pos_open_api_volume,
            full_context=_RECOVERY_CONTEXT_FULL,
            partial_context=_RECOVERY_CONTEXT_PARTIAL,
        )
    )


def _load_recovery_position_row(position_id: int) -> dict[str, Any]:
    return _recovery_position_store().load(position_id)


def _merge_recovery_position_meta(position_id: int, meta: dict[str, Any] | None) -> None:
    _recovery_position_store().merge_meta(position_id, meta)


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
    )


def _update_entry_protection_plan_status(
    position_id: int,
    *,
    status: str,
    error: str = "",
    attempted: bool = False,
    applied_sl: float = 0.0,
    applied_tp: float = 0.0,
) -> None:
    row = _load_recovery_position_row(int(position_id))
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
    _merge_recovery_position_meta(int(position_id), meta)


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
        live_state_update=_live_state_update,
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
        load_recovery_row=_load_recovery_position_row,
        debug_log=lambda pid, exc: logger.debug(
            "[live] attribution restore skipped for pos %s: %s",
            pid,
            exc,
        ),
    )


def _current_regime_hint() -> str:
    return _lifecycle_current_regime_hint_from_composite(
        _live_state_get("last_composite", clone=True) or {}
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
        default_context_integrity=_RECOVERY_CONTEXT_FULL,
        build_update=_lifecycle_build_position_path_metrics_update,
        normalize_path_state=normalize_path_state,
        update_path_metrics=update_position_path_metrics,
        upsert_recovery_position=_upsert_recovery_position_state,
        record_aux_failure=_record_risk_reduction_aux_failure,
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
    context_inputs = _lifecycle_build_position_supervisor_context_inputs(
        position=position,
        cfg=cfg,
        positions=positions,
        account=acct,
        entry_decision_id=_lookup_entry_decision_for_risk_reduction(
            int(position.get("position_id") or position.get("ticket") or 0),
            operation="position_supervisor_context",
        ),
        risk_snapshot=_live_state_get("risk", {}, clone=True) or {},
        total_api_volume=_tracked_total_api_volume(positions or []),
        market_context=_live_state_get("last_composite", {}, clone=True) or {},
        supervisor_state=supervisor_state,
        loop_running=bool(_live_state_get("loop_running", True)),
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

    runtime = PositionSupervisorEvaluationRuntime(
        build_context=lambda position, **kwargs: _build_position_supervisor_context(
            position,
            broker_schedule=broker_schedule,
            **kwargs,
        ),
        evaluate_rule=evaluate_position_supervisor,
        get_quality_advisor=_get_position_quality_advisor,
        set_quality_advisor=_set_position_quality_advisor,
        quality_advisor_factory=_create_position_quality_advisor,
        model_influence_service=shared_model_influence_service,
        build_model_tighten_controls=build_model_tighten_controls,
        load_recovery_row=_load_recovery_row_for_risk_reduction,
        upsert_recovery_position=_upsert_recovery_position_state,
        build_state_upsert_payload=_lifecycle_build_supervisor_state_upsert_payload,
        loop_strategy_name=_current_loop_strategy_name(""),
        default_context_integrity=_RECOVERY_CONTEXT_FULL,
        record_aux_failure=_record_risk_reduction_aux_failure,
    )
    return _runtime_evaluate_position(
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
        _upsert_recovery_position_state(
            position,
            **_lifecycle_build_supervisor_state_upsert_payload(
                recovery_row=row,
                verdict=verdict,
                broker=broker,
                strategy_name=strategy_name,
                loop_strategy_name=_current_loop_strategy_name(),
                default_context_integrity=_RECOVERY_CONTEXT_FULL,
                action_applied=action_applied,
                applied_ts=time.time() if action_applied else 0.0,
            ),
        )
    except Exception as exc:
        _record_risk_reduction_aux_failure(
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
        _upsert_recovery_position_state(
            position,
            **_lifecycle_build_protection_state_upsert_payload(
                recovery_row=row,
                verdict=verdict,
                source=source,
                broker=broker,
                strategy_name=strategy_name,
                loop_strategy_name=_current_loop_strategy_name(),
                default_context_integrity=_RECOVERY_CONTEXT_FULL,
                action_applied=action_applied,
                applied_ts=time.time() if action_applied else 0.0,
            ),
        )
    except Exception as exc:
        _record_risk_reduction_aux_failure(
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
    _merge_recovery_position_meta(
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
                risk_state=_risk_state_with_verdict_dict(risk_verdict or {}),
                risk_verdict=risk_verdict,
                account=acct,
                cfg=cfg,
                event_type=event_type,
                tick=tick,
                session_pnl=_live_state_get("session_pnl", 0.0),
                fallback_decision_ts=time.time(),
            )
        )
    except Exception as exc:
        logger.warning("[live] supervisor ledger failed for pos %s: %s", position.get("position_id"), exc)
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
        logger.debug("[live] supervisor position event %s failed for pos %s: %s", event_type, position.get("position_id"), exc)


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
        logger.warning("[live] supervisor trace failed for pos %s: %s", position.get("position_id"), exc)
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
        record_aux_failure=_record_risk_reduction_aux_failure,
        log_trace=_log_supervisor_trace,
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
        live_state_get=_live_state_get,
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
        result_is_position_not_found=_result_is_position_not_found,
        retire_broker_missing_position=_retire_broker_missing_position,
        reconcile_positions=_explicit_position_reconcile,
        verify_protection_projection=_verify_position_protection_projection,
        publish_fresh_positions=lambda result: _publish_fresh_position_reconcile(
            result,
            broker="ctrader",
        ),
        persist_safety_fail_closed=_persist_safety_fail_closed,
        plan_reduce=lambda **kwargs: _plan_supervisor_reduce_action(
            **kwargs,
            floor_api_volume_to_step=_floor_api_volume_to_step,
            should_full_close_untradeable_reduce=(
                _should_full_close_untradeable_reduce
            ),
        ),
        normalize_reduce=_normalize_supervisor_reduce_verdict,
        remember_close_reason=_remember_close_reason,
        remember_close_verdict=_remember_close_verdict,
        capture_partial_close_session_cursor=lambda **kwargs: (
            _capture_partial_close_deal_cursor(**kwargs)
        ),
        sync_partial_close_session_fact=lambda **kwargs: (
            _sync_partial_close_session_fact(
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
_live_state: dict = default_live_state()

# ★ 保护 _live_state 的读-改-写操作 (多线程: HTTP handler + live loop + scheduler)
_LIVE_STATE_LOCK = threading.Lock()
_DATA_SYNC_LOCK = threading.Lock()


def _live_state_get(key: str, default=None, *, clone: bool = False):
    return _runtime_state_get(_live_state, _LIVE_STATE_LOCK, key, default, clone=clone)


def _live_state_snapshot() -> dict:
    """Return one immutable projection for API/WS serialization.

    Related fields are published in one locked state update. API readers must
    copy that projection once; reading ``_live_state`` field by field can
    otherwise combine the previous value with the next update and manufacture
    a mixed freshness envelope.
    """
    with _LIVE_STATE_LOCK:
        return _safe_container_snapshot(_live_state)


def _live_state_set(key: str, value) -> None:
    _runtime_state_set(_live_state, _LIVE_STATE_LOCK, key, value)
    _notify_live_state_change()


def _live_state_update(**kwargs) -> None:
    _runtime_state_update(_live_state, _LIVE_STATE_LOCK, **kwargs)
    _notify_live_state_change()


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
    _live_state_update(
        account_reconcile_failed_at=time.time(),
        account_reconcile_error=str(error or "account_reconcile_failed")[:500],
    )


def _mark_positions_reconcile_failed(error: str) -> None:
    _live_state_update(
        positions_reconcile_failed_at=time.time(),
        positions_reconcile_error=str(error or "positions_reconcile_failed")[:500],
    )


def _get_state_pg_conn():
    from backend.core.db import get_state_pg_conn

    return get_state_pg_conn()


def _get_state_read_conn():
    from backend.core.db import get_state_pg_conn

    return get_state_pg_conn(read_only=True)


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


def _state_conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _state_sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _state_conn_is_pg(conn) else sql


def _state_execute(conn, sql: str, params=None):
    if params is None:
        return conn.execute(_state_sql(conn, sql))
    return conn.execute(_state_sql(conn, sql), params)


def _runtime_kv_get(key: str, default=None):
    conn = _get_state_read_conn()
    try:
        row = _state_execute(
            conn,
            "SELECT value_json FROM runtime_kv WHERE key=?",
            (key,),
        ).fetchone()
    except Exception:
        return default
    finally:
        conn.close()
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except Exception:
        return default


def _runtime_kv_write_on_conn(conn, key: str, value, updated_at: float | None = None) -> None:
    ts = float(updated_at or time.time())
    value_json = json.dumps(value, ensure_ascii=False, default=str)
    _state_execute(
        conn,
        """
        INSERT INTO runtime_kv(key, value_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value_json=excluded.value_json,
            updated_at=excluded.updated_at
        """,
        (key, value_json, ts),
    )
def _drain_runtime_kv_pending(conn, limit: int = 100) -> int:
    if not _RUNTIME_KV_PENDING_PATH.exists():
        return 0
    with _RUNTIME_KV_PENDING_LOCK:
        lines = _RUNTIME_KV_PENDING_PATH.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        if not lines:
            _RUNTIME_KV_PENDING_PATH.unlink(missing_ok=True)
            return 0
        drained = 0
        remaining: list[str] = []
        for idx, raw in enumerate(lines):
            if drained >= limit:
                remaining.extend(lines[idx:])
                break
            try:
                record = json.loads(raw)
                key = str(record.get("key") or "")
                if not key:
                    remaining.append(raw)
                    continue
                _runtime_kv_write_on_conn(
                    conn,
                    key,
                    record.get("value"),
                    updated_at=float(record.get("queued_at") or time.time()),
                )
                drained += 1
            except Exception:
                remaining.append(raw)
                remaining.extend(lines[idx + 1:])
                break
        _rewrite_runtime_kv_pending_unlocked(remaining)
        return drained


def _runtime_kv_set(key: str, value) -> None:
    conn = _get_state_pg_conn()
    try:
        drained = _drain_runtime_kv_pending(conn)
        if drained:
            logger.info("[live] runtime_kv pending drained: {}", drained)
        _runtime_kv_write_on_conn(conn, key, value)
        conn.commit()
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            _append_runtime_kv_pending(key, value, str(exc))
            logger.warning("[live] runtime_kv write queued: {}", exc)
        except Exception as queue_exc:
            logger.error("[live] runtime_kv write failed and queue failed: write={} queue={}", exc, queue_exc)
    finally:
        conn.close()


_POSITION_DECISION_INDEX_CACHE: dict[str, dict[str, Any]] | None | bool = False
_POSITION_DECISION_INDEX_PATH = (
    Path(__file__).resolve().parents[2] / "run_artifacts" / "canonical_v2_position_decision_index.json"
)


def _position_decision_index() -> dict[str, dict[str, Any]] | None:
    """Lazily load the materialized position->entry decision index (once).

    Returns None when the file is missing/invalid; never writes.  The
    projection is rebuilt independently
    (scripts/canonical_v2_position_decision_index.py) and is stale-tolerant:
    a position missing from the index is resolved from the live recovery
    snapshot, without consulting a retired fact store.
    """
    global _POSITION_DECISION_INDEX_CACHE
    if _POSITION_DECISION_INDEX_CACHE is False:
        _POSITION_DECISION_INDEX_CACHE = load_position_decision_index(_POSITION_DECISION_INDEX_PATH)
    return _POSITION_DECISION_INDEX_CACHE  # type: ignore[return-value]


def _lookup_entry_decision_id(position_id: int) -> str:
    """Entry decision for a position via the canonical position-decision index.

    The materialized index is a rebuildable file projection
    (scripts/canonical_v2_position_decision_index.py); positions missing from
    it (e.g. newer than the last rebuild) resolve to "".
    """
    index = _position_decision_index()
    if index is None:
        return ""
    entry = index.get(str(position_id))
    if entry is None:
        return ""
    return str(entry.get("parent_decision_id") or entry.get("decision_id") or "")


def _lookup_open_decision_context(position_id: int) -> dict:
    """Latest open decision context (canonical position-decision index first)."""
    index = _position_decision_index()
    if index is not None:
        entry = index.get(str(position_id))
        if entry is not None:
            return {
                "entry_ts": float(entry.get("decision_ts") or 0.0),
                "timeframe": str(entry.get("timeframe") or ""),
                "source": "canonical_position_decision_index",
            }
    conn = _get_state_read_conn()
    try:
        recovery = _state_execute(
            conn,
            """
            SELECT first_seen_at FROM recovery_position_state
            WHERE position_id=?
            ORDER BY first_seen_at DESC LIMIT 1
            """,
            (str(int(position_id)),),
        ).fetchone()
        if recovery:
            return {
                "entry_ts": float(recovery["first_seen_at"] or 0.0),
                "timeframe": "",
                "source": "recovery_position_state",
            }
        return {"entry_ts": 0.0, "timeframe": "", "source": ""}
    finally:
        conn.close()


def _ensure_open_ledger_for_recovered_close(
    position_id: int,
    *,
    broker: str,
    close_ts: float,
    close_price: float,
    real_pnl: dict | None = None,
    close_reason: str = "broker_close",
) -> str:
    """Create minimal open evidence for a recovered broker position before close review."""
    if position_id <= 0:
        return ""
    existing = _lookup_entry_decision_id(position_id)
    if existing:
        return existing
    if not _LEDGER:
        return ""

    conn = _get_state_read_conn()
    try:
        row = _state_execute(
            conn,
            "SELECT * FROM recovery_position_state WHERE position_id=?",
            (str(int(position_id)),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return ""

    payloads = _lifecycle_build_recovered_open_ledger_payloads(
        position_id=position_id,
        recovery_row=row,
        broker=broker,
        close_ts=close_ts,
        close_price=close_price,
        risk_state=_live_state_get("risk", {}, clone=True) or {},
        real_pnl=real_pnl or {},
        close_reason=close_reason,
        fallback_strategy_name=_current_loop_strategy_name(),
        context_integrity_default=_RECOVERY_CONTEXT_PARTIAL,
        fallback_now_ts=time.time(),
    )

    try:
        decision_id = _LEDGER.log_decision(**payloads["decision_payload"])
        _LEDGER.log_position_event(
            decision_id=decision_id,
            **payloads["position_event_payload"],
        )
        recovery_state_payload = dict(payloads["recovery_state_payload"])
        recovery_state_payload["entry_decision_id"] = decision_id
        recovery_state_meta = dict(payloads["recovery_state_meta"])
        recovery_state_meta["open_repair_decision_id"] = decision_id
        _upsert_recovery_position_state(
            recovery_state_payload,
            **payloads["recovery_state_kwargs"],
            meta=recovery_state_meta,
        )
        logger.info("[live] repaired missing open ledger before close pos=%s decision=%s", position_id, decision_id)
        return decision_id
    except Exception as exc:
        logger.debug("[live] open ledger repair before close failed for pos %s: %s", position_id, exc)
        return ""


def _lookup_recovery_context_integrity(position_id: int, default: str = _RECOVERY_CONTEXT_FULL) -> str:
    return _recovery_position_store().context_integrity(
        position_id,
        default=default,
    )


def _persist_loop_desired_state(
    enabled: bool,
    *,
    broker: str = "ctrader",
    strategy_name: str = "factor_v4",
    reason: str = "manual",
) -> None:
    _runtime_kv_set(
        _RUNTIME_KV_LOOP_DESIRED,
        {
            "enabled": bool(enabled),
            "broker": broker,
            "strategy_name": strategy_name,
            "reason": reason,
            "updated_at": time.time(),
        },
    )


def _read_loop_desired_state() -> dict:
    state = _runtime_kv_get(_RUNTIME_KV_LOOP_DESIRED, {}) or {}
    return state if isinstance(state, dict) else {}


def _session_state_key(trade_date: str | None = None) -> str:
    if not trade_date:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{_RUNTIME_KV_SESSION_STATE_PREFIX}{trade_date}"


def _session_state_snapshot(trade_date: str | None = None) -> dict:
    if not trade_date:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    window_start, window_end = _session_trade_window(trade_date)
    calendar_day = _calendar_day_trade_summary("Asia/Shanghai")
    return {
        "schema_version": "live_session_state.v3",
        "trade_date": trade_date,
        "session_date_basis": "utc_risk_day",
        "session_timezone": "UTC",
        "session_window_start": window_start,
        "session_window_end": window_end,
        "calendar_day": calendar_day,
        "source": str(_live_state_get("session_state_source", "runtime_incremental") or "runtime_incremental"),
        "status": str(_live_state_get("session_state_status", "unknown") or "unknown"),
        "session_pnl": float(_live_state_get("session_pnl", 0.0) or 0.0),
        "session_trades": int(_live_state_get("session_trades", 0) or 0),
        "session_winning": int(_live_state_get("session_winning", 0) or 0),
        "session_losing": int(_live_state_get("session_losing", 0) or 0),
        "session_trade_pnls": list(_live_state_get("session_trade_pnls", [], clone=True) or [])[-200:],
        "session_realized_pnl_legs": list(
            _live_state_get("session_realized_pnl_legs", [], clone=True) or []
        )[-500:],
        "session_realized_legs": int(
            _live_state_get("session_realized_legs", 0) or 0
        ),
        "session_recorded_position_ids": list(
            _live_state_get("session_recorded_position_ids", [], clone=True) or []
        )[-1000:],
        "session_consecutive_loss": int(_live_state_get("session_consecutive_loss", 0) or 0),
        "session_max_drawdown_pct": float(_live_state_get("session_max_drawdown_pct", 0.0) or 0.0),
        "session_peak_equity": float(_live_state_get("session_peak_equity", 0.0) or 0.0),
        "session_start_balance": float(_live_state_get("session_start_balance", 0.0) or 0.0),
        "session_last_trade_ts": float(_live_state_get("session_last_trade_ts", 0.0) or 0.0),
        "session_observed_at": float(_live_state_get("session_observed_at", 0.0) or 0.0),
        "circuit_breaker": bool(_live_state_get("circuit_breaker", False)),
        "circuit_reason": str(_live_state_get("circuit_reason", "") or ""),
        "session_circuit_observation": dict(
            _live_state_get("session_circuit_observation", {}, clone=True) or {}
        ),
        "trade_equity_history": list(_live_state_get("trade_equity_history", [], clone=True) or [])[-500:],
        "updated_at": time.time(),
    }


def _persist_session_state(trade_date: str | None = None) -> None:
    try:
        snapshot = _session_state_snapshot(trade_date)
        _runtime_kv_set(_session_state_key(snapshot["trade_date"]), snapshot)
    except Exception as exc:
        logger.debug("[live] session state persist failed: %s", exc)


def _session_trade_window(trade_date: str, timezone_name: str = "UTC") -> tuple[float, float]:
    return _session_restore_trade_window(trade_date, timezone_name)


def _load_authoritative_session_trades(
    trade_date: str,
    timezone_name: str = "UTC",
    *,
    broker_open_position_ids: set[int] | None = None,
    confirmed_closed_position_ids: set[int] | None = None,
) -> list[dict] | None:
    """Load one PnL row per position whose final close happened on a date.

    ``runtime_kv`` is a recovery cache, not the trade fact source. Broker
    deals are grouped by position so partial-close legs remain one trade and
    their aggregate net PnL matches ``execution.deal_sync``.

    ``None`` means the authoritative query or completeness proof failed and
    callers may use the persisted cache only as a degraded display fallback.
    An empty list is a valid no-trades result only when the broker-open set is
    itself a fresh explicit fact and every system-tracked missing position has
    a concrete close deal.
    """
    facts = _load_authoritative_session_deal_facts(
        trade_date,
        timezone_name,
        broker_open_position_ids=broker_open_position_ids,
        confirmed_closed_position_ids=confirmed_closed_position_ids,
    )
    if facts is None:
        return None
    return list(facts.get("completed_position_trades") or [])


def _load_authoritative_session_deal_facts(
    trade_date: str,
    timezone_name: str = "UTC",
    *,
    broker_open_position_ids: set[int] | None = None,
    confirmed_closed_position_ids: set[int] | None = None,
) -> dict[str, Any] | None:
    return _session_load_authoritative_deal_facts(
        trade_date,
        timezone_name,
        broker_open_position_ids=broker_open_position_ids,
        confirmed_closed_position_ids=confirmed_closed_position_ids,
        connection_factory=_get_state_read_conn,
        execute=_state_execute,
        warning=logger.warning,
    )


def _calendar_day_trade_summary(timezone_name: str = "Asia/Shanghai") -> dict:
    """Read-only operator-day view; never drives the UTC risk circuit."""
    tz = ZoneInfo(timezone_name)
    trade_date = datetime.now(tz).strftime("%Y-%m-%d")
    open_position_ids = _fresh_cached_broker_open_position_ids()
    trades = (
        _load_authoritative_session_trades(
            trade_date,
            timezone_name,
            broker_open_position_ids=open_position_ids,
        )
        if open_position_ids is not None
        else None
    )
    if trades is None:
        return {
            "status": "unavailable",
            "trade_date": trade_date,
            "timezone": timezone_name,
            "risk_authoritative": False,
        }
    pnls = [float(item.get("net", 0.0) or 0.0) for item in trades]
    window_start, window_end = _session_trade_window(trade_date, timezone_name)
    return {
        "status": "available",
        "trade_date": trade_date,
        "timezone": timezone_name,
        "window_start": window_start,
        "window_end": window_end,
        "trade_count": len(pnls),
        "winning_count": sum(1 for pnl in pnls if pnl > 0),
        "losing_count": sum(1 for pnl in pnls if pnl < 0),
        "net_pnl": sum(pnls),
        "risk_authoritative": False,
        "source": "ctrader_deals.final_close_calendar_view.v1",
    }


def _build_session_state_from_authoritative_trades(
    *,
    trade_date: str,
    trades: list[dict],
    realized_close_legs: list[dict] | None = None,
) -> dict:
    """Project fresh broker account/deal facts into the live risk session.

    Cache-derived peak/equity history is intentionally excluded: only fresh
    broker account and deal facts may reconstruct the risk session.
    """
    account = _live_state_get("account", {}, clone=True) or {}
    limits = RiskLimitSnapshot.from_runtime_config()
    return _session_build_authoritative_state(
        trade_date=trade_date,
        completed_position_trades=trades,
        realized_close_legs=(
            None if realized_close_legs is None else list(realized_close_legs)
        ),
        current_balance=float(account.get("balance", 0.0) or 0.0),
        max_consecutive_losses=int(limits.max_consecutive_losses),
        max_daily_loss_pct=float(limits.max_daily_loss_pct),
        enforce_circuit_breaker=not bounded_demo_mode_active(),
    )


def _restore_session_state_for_day(
    trade_date: str | None = None,
    *,
    broker_open_position_ids: set[int] | None = None,
    confirmed_closed_position_ids: set[int] | None = None,
) -> bool:
    if not trade_date:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_state = _runtime_kv_get(_session_state_key(trade_date), {}) or {}
    authoritative_facts = _load_authoritative_session_deal_facts(
        trade_date,
        broker_open_position_ids=broker_open_position_ids,
        confirmed_closed_position_ids=confirmed_closed_position_ids,
    )
    account = _live_state_get("account", {}, clone=True) or {}
    limits = RiskLimitSnapshot.from_runtime_config()
    decision = _session_resolve_restore(
        trade_date=trade_date,
        raw_cache=raw_state,
        authoritative_facts=authoritative_facts,
        current_balance=account.get("balance", 0.0),
        max_consecutive_losses=int(limits.max_consecutive_losses),
        max_daily_loss_pct=float(limits.max_daily_loss_pct),
        observed_at=time.time(),
        enforce_circuit_breaker=not bounded_demo_mode_active(),
    )
    if decision.get("authoritative_error"):
        logger.warning(
            "[live] authoritative session projection unavailable for %s: %s",
            trade_date,
            decision["authoritative_error"],
        )
    if not decision.get("authoritative") and decision.get("restored"):
        logger.warning(
            "[live] restoring cached session projection for %s because broker close facts are unavailable",
            trade_date,
        )
    _live_state_update(**dict(decision.get("state") or {}))
    if decision.get("authoritative"):
        _persist_session_state(trade_date)
        _evaluate_daily_drawdown()
    return bool(decision.get("restored"))


def _defer_close_until_authoritative_deal(
    position_id: int,
    *,
    broker: str,
    tick: int,
    reason: str = "close_deal_missing_or_delayed",
    recovery_evidence: dict[str, Any] | None = None,
) -> None:
    """Keep broker-close evidence pending without inventing realized PnL."""

    pid = int(position_id or 0)
    detected_at = time.time()
    evidence = {
        "position_id": pid,
        "broker": str(broker or ""),
        "tick": int(tick),
        "reason": str(reason or "close_deal_missing_or_delayed"),
        "detected_at": detected_at,
        "expected_position_volume": float(
            _pos_open_api_volume.get(pid, 0.0) or 0.0
        ),
        **dict(recovery_evidence or {}),
    }
    try:
        _merge_recovery_position_meta(
            pid,
            {
                "close_deal_pending": {
                    "status": "pending",
                    **evidence,
                }
            },
        )
    except Exception as exc:
        evidence["recovery_projection_error"] = f"{type(exc).__name__}:{exc}"

    latch = no_new_risk_latch_status(fail_closed=True)
    cause_key = ("session_risk_unavailable", str(pid))
    active_causes = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(latch.get("causes") or [])
        if isinstance(item, dict)
    }
    if cause_key not in active_causes:
        try:
            activate_no_new_risk_latch(
                reason="session_risk_close_deal_unavailable",
                actor="system:session_restore",
                correlation_id=str(pid),
                metadata=evidence,
                cause=cause_key[0],
                cause_id=cause_key[1],
            )
        except Exception as exc:
            evidence["latch_error"] = f"{type(exc).__name__}:{exc}"
    latch = no_new_risk_latch_status(fail_closed=True)
    _live_state_update(
        session_state_status="unavailable",
        session_state_source="close_deal_pending",
        session_risk_blockers=[f"close_deal_pending:{pid}"],
        session_observed_at=0.0,
        accepting_new_risk=False,
        no_new_risk_latch=latch,
    )
    try:
        append_safety_outbox(
            event_type="session_close_deal_pending",
            payload=evidence,
            error=str(reason or "close_deal_missing_or_delayed"),
        )
    except Exception:
        pass


def _release_session_close_deal_latch(position_id: int, real_pnl: dict[str, Any]) -> None:
    """Release one missing-deal cause only with concrete cTrader deal evidence."""

    pid = int(position_id or 0)
    if not _authoritative_close_pnl(real_pnl):
        raise ValueError("authoritative_close_deal_required")
    latch = no_new_risk_latch_status(fail_closed=True)
    if ("session_risk_unavailable", str(pid)) not in {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(latch.get("causes") or [])
        if isinstance(item, dict)
    }:
        return
    evidence = {
        "position_id": pid,
        "deal_id": real_pnl.get("deal_id"),
        "deal_ids": list(real_pnl.get("deal_ids") or []),
        "exec_timestamp": float(real_pnl.get("exec_timestamp") or 0.0),
        "net": float(real_pnl.get("net") or 0.0),
        "source": str(real_pnl.get("source") or "ctrader_deals"),
    }
    try:
        release_no_new_risk_latch_cause(
            cause="session_risk_unavailable",
            cause_id=str(pid),
            reason="authoritative_close_deal_recovered",
            actor="system:session_restore",
            correlation_id=str(real_pnl.get("deal_id") or pid),
            evidence=evidence,
        )
    except Exception as exc:
        # Broker/deal truth remains usable; a release write failure simply keeps
        # the deployment conservatively latched until operator repair.
        try:
            append_safety_outbox(
                event_type="session_close_deal_latch_release_failed",
                payload=evidence,
                error=f"{type(exc).__name__}:{exc}",
            )
        except Exception:
            pass


def _release_orphaned_recovery_session_latches(
    position_ids: list[int] | set[int] | tuple[int, ...],
    *,
    broker: str,
    broker_position_ids: set[int],
    reconcile_id: str,
    observed_at: float,
) -> None:
    """Release close-deal latches for recovery rows proven to be orphaned.

    This is deliberately distinct from ``_release_session_close_deal_latch``:
    no broker close deal is being asserted here.  The only fact used is that
    ``RecoveryPositionStore.purge_unbrokered`` already verified that the row
    had no entry lineage and was absent from the same fresh broker snapshot.
    Keeping this release separate prevents a cleanup of synthetic/test state
    from becoming a false close outcome or a supervisor learning sample.
    """

    normalized_ids = sorted({int(position_id) for position_id in position_ids if int(position_id) > 0})
    if not normalized_ids:
        return
    active_causes = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(
            no_new_risk_latch_status(fail_closed=True).get("causes") or []
        )
        if isinstance(item, dict)
    }
    for position_id in normalized_ids:
        cause_key = ("session_risk_unavailable", str(position_id))
        if cause_key not in active_causes:
            continue
        evidence = {
            "position_id": position_id,
            "broker": str(broker or "ctrader"),
            "broker_position_ids": sorted(int(item) for item in broker_position_ids),
            "reconcile_id": str(reconcile_id or ""),
            "observed_at": float(observed_at or 0.0),
            "source": "fresh_ctrader_reconcile",
            "classification": "orphaned_or_test_recovery_state",
            "broker_close_deal_asserted": False,
        }
        try:
            release_no_new_risk_latch_cause(
                cause=cause_key[0],
                cause_id=cause_key[1],
                reason="orphaned_recovery_row_purged",
                actor="system:position_reconcile",
                correlation_id=str(reconcile_id or position_id),
                evidence=evidence,
            )
            logger.warning(
                f"[live] released orphaned recovery latch for position {position_id} "
                f"after fresh broker reconcile (no close-deal claim)"
            )
        except Exception as exc:
            # A failed release must remain fail-closed.  The recovery row has
            # already been purged, so surface the durable-latch repair issue
            # loudly for the next operator/reconcile cycle.
            logger.error(
                f"[live] failed to release orphaned recovery latch for position "
                f"{position_id}: {type(exc).__name__}: {exc}"
            )

    # Remove only the stale display blockers produced by the same synthetic
    # rows.  Do not mark the session available here: session_restore owns that
    # authority and may still have an independent session_not_restored cause.
    stale_blockers = {
        f"close_deal_pending:{position_id}" for position_id in normalized_ids
    }
    current_blockers = list(
        _live_state_get("session_risk_blockers", [], clone=True) or []
    )
    filtered_blockers = [
        blocker for blocker in current_blockers if str(blocker) not in stale_blockers
    ]
    if filtered_blockers != current_blockers:
        _live_state_update(session_risk_blockers=filtered_blockers)


def _pending_session_close_causes() -> dict[int, dict[str, Any]]:
    """Return durable/local close-deal cursors keyed by broker position."""

    try:
        causes = list(
            no_new_risk_latch_status(fail_closed=True).get("causes") or []
        )
    except Exception:
        return {}
    result: dict[int, dict[str, Any]] = {}
    for item in causes:
        if not isinstance(item, dict) or str(item.get("cause") or "") != (
            "session_risk_unavailable"
        ):
            continue
        try:
            position_id = int(item.get("cause_id") or 0)
        except (TypeError, ValueError):
            continue
        if position_id > 0:
            metadata = item.get("metadata")
            result[position_id] = {
                **(dict(metadata) if isinstance(metadata, dict) else {}),
                "latch_created_at": float(item.get("created_at") or 0.0),
            }
    return result


def _pending_session_close_position_ids() -> set[int]:
    return set(_pending_session_close_causes())


def _pending_close_fallback_state(
    position_id: int,
    *,
    broker: str,
    recovery_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pid = int(position_id or 0)
    evidence = dict(recovery_evidence or {})
    return {
        "position_id": pid,
        "broker": str(broker or "ctrader"),
        "symbol": "XAUUSD+",
        "open_price": float(_pos_open_prices.get(pid, 0.0) or 0.0),
        "volume": float(
            _pos_open_api_volume.get(pid, 0.0)
            or evidence.get("expected_position_volume", 0.0)
            or 0.0
        ),
        "close_pnl": 0.0,
        "context_integrity": _RECOVERY_CONTEXT_PARTIAL,
    }


def _pending_close_requirements(
    position_state: dict[str, Any],
    *,
    latch_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = dict(latch_evidence or {})
    raw_meta = position_state.get("recovery_meta_json")
    try:
        recovery_meta = (
            json.loads(raw_meta)
            if isinstance(raw_meta, str) and raw_meta
            else dict(raw_meta or {})
        )
    except Exception:
        recovery_meta = {}
    pending = recovery_meta.get("close_deal_pending")
    if isinstance(pending, dict):
        evidence = {**evidence, **pending}
    return evidence


def _pending_close_cursor_overrides(
    position_ids: set[int],
    *,
    active_rows_by_id: dict[int, dict[str, Any]],
    pending_close_causes: dict[int, dict[str, Any]],
    broker: str,
) -> dict[int, dict[str, Any]]:
    """Recover the original pre-RPC cursor for delayed-deal retries."""

    result: dict[int, dict[str, Any]] = {}
    for pid in position_ids:
        state = active_rows_by_id.get(pid) or _pending_close_fallback_state(
            pid,
            broker=broker,
            recovery_evidence=pending_close_causes.get(pid),
        )
        requirements = _pending_close_requirements(
            state,
            latch_evidence=pending_close_causes.get(pid),
        )
        pending_kind = str(requirements.get("pending_kind") or "")
        pending_reason = str(requirements.get("reason") or "")
        expected_volume = float(
            requirements.get("expected_position_volume") or 0.0
        )
        is_fallback_final_close = bool(
            not pending_kind
            and pending_reason == "close_deal_missing_or_delayed"
            and expected_volume > 0.0
        )
        if (
            pending_kind != "partial_close"
            and (
                pid in active_rows_by_id
                or pending_kind == "final_close"
                or is_fallback_final_close
            )
        ):
            # A durable position that has disappeared at the broker is a
            # final-close recovery, not a new reduction RPC.  A close deal
            # already fetched by an earlier retry remains valid evidence; do
            # not promote it to the retry baseline and wait for a nonexistent
            # second close leg.  Timestamp and required-volume checks still
            # guard against accepting an old partial close.
            #
            # Must run BEFORE the generic baseline passthrough below: the
            # no_new_risk_latch is durable and may still carry baseline_deal_ids
            # captured by an earlier (pre-fix) defer that pointed at the very
            # close deal now in the store.  Using that stale baseline makes
            # observed_ids - baseline_ids empty forever and deadlocks close
            # confirmation (281067702 stuck 2026-08-05).
            result[pid] = {
                "baseline_cursor_available": True,
                "baseline_deal_ids": [],
                "baseline_closed_volume": 0.0,
            }
        elif (
            "baseline_deal_ids" in requirements
            or "baseline_closed_volume" in requirements
        ):
            result[pid] = {
                "baseline_cursor_available": requirements.get(
                    "baseline_cursor_available",
                    True,
                ),
                "baseline_deal_ids": list(
                    requirements.get("baseline_deal_ids") or []
                ),
                "baseline_closed_volume": float(
                    requirements.get("baseline_closed_volume") or 0.0
                ),
            }
    return result


def _pending_close_required_volume_delta(
    position_id: int,
    *,
    active_rows_by_id: dict[int, dict[str, Any]],
    pending_close_causes: dict[int, dict[str, Any]],
    broker: str,
) -> float:
    state = active_rows_by_id.get(position_id) or _pending_close_fallback_state(
        position_id,
        broker=broker,
        recovery_evidence=pending_close_causes.get(position_id),
    )
    requirements = _pending_close_requirements(
        state,
        latch_evidence=pending_close_causes.get(position_id),
    )
    return max(
        0.0,
        float(
            requirements.get("required_closed_volume_delta")
            or state.get("volume")
            or 0.0
        ),
    )


def _pending_close_result_complete(
    real_pnl: dict[str, Any] | None,
    *,
    position_state: dict[str, Any],
    require_volume_proof: bool,
    recovery_requirements: dict[str, Any] | None = None,
) -> bool:
    """Reject an old partial leg as proof of a broker-missing final close."""

    if not _authoritative_close_pnl(real_pnl):
        return False
    requirements = dict(recovery_requirements or {})
    if str(requirements.get("pending_kind") or "") == "partial_close":
        # When the pre-RPC cursor could not be captured, a close leg already
        # present in PostgreSQL cannot be proven to belong to this reduction.
        # Keep the cause latched instead of treating an arbitrary historical
        # partial as recovery evidence.
        if requirements.get("baseline_cursor_available") is False:
            return False
        baseline_ids = {
            int(item)
            for item in list(requirements.get("baseline_deal_ids") or [])
            if int(item or 0) > 0
        }
        observed_ids = {
            int(item)
            for item in list((real_pnl or {}).get("deal_ids") or [])
            if int(item or 0) > 0
        }
        required_delta = float(
            requirements.get("required_closed_volume_delta") or 0.0
        )
        baseline_volume = float(
            requirements.get("baseline_closed_volume") or 0.0
        )
        observed_volume = float((real_pnl or {}).get("closed_volume") or 0.0)
        return bool(
            required_delta > 0.0
            and observed_ids - baseline_ids
            and observed_volume - baseline_volume + 1e-9 >= required_delta
        )
    if not require_volume_proof:
        return True
    expected_volume = float(position_state.get("volume") or 0.0)
    closed_volume = float((real_pnl or {}).get("closed_volume") or 0.0)
    return bool(
        expected_volume > 0.0
        and closed_volume > 0.0
        and closed_volume + 1e-9 >= expected_volume
    )


def _capture_partial_close_deal_cursor(position_id: int) -> dict[str, Any]:
    """Capture the durable close-deal cursor immediately before broker RPC."""

    pid = int(position_id or 0)
    captured_at = time.time()
    try:
        from execution.deal_sync import find_close_deal

        conn = _get_state_pg_conn()
        try:
            before = find_close_deal(conn, pid) or {}
        finally:
            conn.close()
        return {
            "status": "captured",
            "captured_at": captured_at,
            "baseline_cursor_available": True,
            "baseline_deal_ids": sorted(
                {
                    int(item)
                    for item in list(before.get("deal_ids") or [])
                    if int(item or 0) > 0
                }
            ),
            "baseline_closed_volume": float(
                before.get("closed_volume") or 0.0
            ),
        }
    except Exception as exc:
        _record_risk_reduction_aux_failure(
            "partial_close_deal_cursor_unavailable",
            position_id=pid,
            action="reduce_position",
            error=exc,
        )
        return {
            "status": "unavailable",
            "captured_at": captured_at,
            "baseline_cursor_available": False,
            "baseline_deal_ids": [],
            "baseline_closed_volume": 0.0,
            "error": f"{type(exc).__name__}:{exc}",
        }


def _sync_partial_close_session_fact(
    bridge: Any,
    *,
    broker: str,
    position_id: int,
    close_ts: float,
    volume: float,
    tick: int,
    deal_cursor: dict[str, Any] | None = None,
) -> bool:
    from execution.deal_sync import (
        fetch_deals_since_result,
        find_close_deal,
        store_deals,
    )

    runtime = PartialCloseSessionFactRuntime(
        get_state_connection=_get_state_pg_conn,
        fetch_deals_since_result=fetch_deals_since_result,
        store_deals=store_deals,
        find_close_deal=find_close_deal,
        authoritative_close_pnl=_authoritative_close_pnl,
        defer_close=_defer_close_until_authoritative_deal,
        record_aux_failure=_record_risk_reduction_aux_failure,
        release_close_latch=_release_session_close_deal_latch,
        update_live_state=_live_state_update,
        no_new_risk_latch_status=no_new_risk_latch_status,
        open_api_volumes=_pos_open_api_volume,
        now=time.time,
    )
    return _session_sync_partial_close_fact(
        bridge,
        broker=broker,
        position_id=position_id,
        close_ts=close_ts,
        volume=volume,
        tick=tick,
        runtime=runtime,
        deal_cursor=deal_cursor,
    )


def _fresh_cached_broker_open_position_ids(
    *,
    now_ts: float | None = None,
    stale_after_sec: float = _LIVE_SAFETY_FRESHNESS_SEC,
) -> set[int] | None:
    """Return broker-open position IDs only from a fresh position fact.

    ``None`` means the open-position set is unknown.  Passing an unknown set
    into deals-first restore as an empty set could incorrectly classify a
    partially closed, still-open broker position as a completed trade.
    """

    observed_at = float(_live_state_get("positions_updated_at", 0.0) or 0.0)
    reconcile_id = str(_live_state_get("positions_reconcile_id", "") or "")
    checked_at = float(time.time() if now_ts is None else now_ts)
    if (
        observed_at <= 0.0
        or not reconcile_id
        or checked_at < observed_at
        or checked_at - observed_at > max(0.0, float(stale_after_sec))
    ):
        return None
    positions = _live_state_get("positions_reconciled", [], clone=True)
    if not isinstance(positions, list):
        return None
    position_ids: set[int] = set()
    for position in positions:
        try:
            position_id = int(
                _payload_get(position, "position_id", 0)
                or _payload_get(position, "ticket", 0)
                or 0
            )
        except (TypeError, ValueError):
            return None
        if position_id > 0:
            position_ids.add(position_id)
    return position_ids


def _remember_close_reason(position_id: int, reason: str) -> None:
    try:
        _lifecycle_remember_close_reason(
            pending_reasons=_pending_close_reasons,
            merge_recovery_meta=_merge_recovery_position_meta,
            position_id=position_id,
            reason=reason,
        )
    except Exception as exc:
        # The lifecycle helper stores the process-local reason before PG
        # projection.  Preserve that broker-adjacent fact and defer the audit.
        _pending_close_reasons[int(position_id)] = str(reason or "")
        _record_risk_reduction_aux_failure(
            (
                "emergency_close_audit_deferred"
                if str(reason or "") == "emergency_close"
                else "close_reason_projection_failed"
            ),
            position_id=position_id,
            action="close_position",
            error=exc,
            payload={"reason": str(reason or "")},
        )


def _consume_close_reason(position_id: int, default: str = "broker_close") -> str:
    return _lifecycle_consume_close_reason(
        pending_reasons=_pending_close_reasons,
        load_recovery_row=_load_recovery_position_row,
        position_id=position_id,
        default=default,
    )


def _remember_close_verdict(position_id: int, verdict) -> None:
    try:
        _lifecycle_remember_close_verdict(
            pending_verdicts=_pending_close_verdicts,
            merge_recovery_meta=_merge_recovery_position_meta,
            position_id=position_id,
            verdict=verdict,
        )
    except Exception as exc:
        try:
            from backend.services.live_position_lifecycle import serialize_close_verdict

            _pending_close_verdicts[int(position_id)] = serialize_close_verdict(verdict)
        except Exception:
            pass
        _record_risk_reduction_aux_failure(
            "close_verdict_projection_failed",
            position_id=position_id,
            action="close_position",
            error=exc,
        )


def _consume_close_verdict(position_id: int, close_reason: str) -> dict:
    return _lifecycle_consume_close_verdict(
        pending_verdicts=_pending_close_verdicts,
        load_recovery_row=_load_recovery_position_row,
        build_close_context=_build_close_position_risk_context,
        risk_evaluate=_RISK_POLICY.evaluate,
        position_id=int(position_id),
        close_reason=close_reason,
    )


def _latest_supervisor_event_before_close(position_id: int, close_ts: float, lookback_sec: float = 3600.0) -> dict[str, Any]:
    conn = _get_state_read_conn()
    try:
        # Bounded window scan (reverse keyset); canonical events carry the
        # position inside the payload, so the filter is applied here.
        lower = float(close_ts or time.time()) - max(1.0, lookback_sec)
        upper = float(close_ts or time.time())
        for candidate in iter_decision_rows(
            conn,
            min_observed_epoch=lower,
            max_observed_epoch=upper,
            reverse=True,
        ):
            if (
                str(candidate.get("position_id") or "") == str(position_id)
                and (
                    str(candidate.get("event_type") or "").startswith("supervisor_")
                    or str(candidate.get("event_type") or "") == "holding_timeout"
                )
            ):
                return _lifecycle_normalize_supervisor_event_row(candidate, close_ts=close_ts)
        return {}
    finally:
        conn.close()


def _latest_protection_trace_before_close(position_id: int, close_ts: float, lookback_sec: float = 3600.0) -> dict[str, Any]:
    conn = _get_state_read_conn()
    try:
        upper = float(close_ts or time.time())
        lower = upper - max(1.0, lookback_sec)
        rows = [
            item
            for item in iter_supervisor_trace_rows(
                conn,
                limit=0,
                position_id=str(position_id),
                reverse=True,
            )
            if lower <= float(item.get("event_ts") or item.get("observed_at") or 0.0) <= upper
            and str(item.get("action") or "").strip().lower() in {"tighten", "reduce", "close"}
        ]
        row = rows[0] if rows else None
        return _lifecycle_normalize_protection_trace_row(row, close_ts=close_ts)
    finally:
        conn.close()


def _classify_close_source(position_id: int, close_reason: str, close_ts: float) -> dict[str, Any]:
    ledger_latest = _latest_supervisor_event_before_close(position_id, close_ts)
    trace_latest = _latest_protection_trace_before_close(position_id, close_ts)
    latest = _lifecycle_latest_close_evidence(ledger_latest, trace_latest)
    return _lifecycle_classify_close_source_from_evidence(
        close_reason=close_reason,
        evidence=latest,
    )


def _risk_state_with_verdict_dict(verdict: dict) -> dict:
    state = _live_state_get("risk", {}, clone=True) or {}
    return _lifecycle_build_risk_state_with_policy_verdict(
        state,
        verdict,
        serialized=True,
    )


def _normalize_position_snapshot(raw: Any) -> dict:
    return _lifecycle_normalize_position_snapshot(raw)


def _upsert_recovery_position_state(
    raw_position: Any,
    *,
    broker: str,
    strategy_name: str,
    status: str = "open",
    context_integrity: str | None = None,
    meta: dict | None = None,
) -> None:
    _recovery_position_store().upsert(
        raw_position,
        broker=broker,
        strategy_name=strategy_name,
        status=status,
        context_integrity=context_integrity,
        meta=meta,
    )


def _list_active_recovery_positions(broker: str) -> list[dict]:
    return _recovery_position_store().list_active(broker)


def _active_recovery_position_ids_for_close_detection(broker: str) -> set[int]:
    """Keep durable open rows in close detection even if memory lost the ID."""

    try:
        return _lifecycle_recovery_active_position_ids(
            _list_active_recovery_positions(broker)
        )
    except Exception as exc:
        logger.debug(
            "[live] durable recovery IDs unavailable for close detection: %s",
            exc,
        )
        return set()


def _recovery_last_seen_by_position(position_ids: set[int]) -> dict[int, float]:
    """Return the last broker-open observation used to reject stale partial deals."""
    return _recovery_position_store().last_seen_by_position(position_ids)


def _recovery_remaining_volume_by_position(
    position_ids: set[int],
) -> dict[int, float]:
    """Return the last fresh broker-open volume for close completeness proof."""
    return _recovery_position_store().remaining_volume_by_position(position_ids)


def _mark_recovery_position_closed(
    position_id: int,
    *,
    close_reason: str,
    close_pnl: float,
    closed_at: float,
    meta: dict | None = None,
) -> None:
    _recovery_position_store().mark_closed(
        position_id,
        close_reason=close_reason,
        close_pnl=close_pnl,
        closed_at=closed_at,
        meta=meta,
    )


def _replay_recovered_close(
    *,
    broker: str,
    position_id: int,
    position_state: dict,
    real_pnl: dict | None,
    strategy_name: str,
) -> bool:
    return _runtime_replay_recovered_close(
        broker=broker,
        position_id=position_id,
        position_state=position_state,
        real_pnl=real_pnl,
        strategy_name=strategy_name,
        runtime=RecoveredCloseReplayRuntime(
            authoritative_close_pnl=_authoritative_close_pnl,
            defer_close=_defer_close_until_authoritative_deal,
            build_payloads=_lifecycle_build_replayed_close_payloads,
            mark_recovery_closed=_mark_recovery_position_closed,
            release_close_latch=_release_session_close_deal_latch,
            get_risk_state=lambda: (
                _live_state_get("risk", {}, clone=True) or {}
            ),
            now=time.time,
            partial_context=_RECOVERY_CONTEXT_PARTIAL,
            ledger=_LEDGER,
            trade_reviewer=_TRADE_REVIEWER,
            experience_builder=_EXPERIENCE_BUILDER,
            policy_suggester=_POLICY_SUGGESTER,
            attr_engine=(_factor_pipeline or {}).get("attribution"),
            debug=logger.debug,
        ),
    )


def _result_is_position_not_found(result: Any) -> bool:
    text = " ".join(
        str(part or "")
        for part in (
            getattr(result, "error_code", ""),
            getattr(result, "comment", ""),
            getattr(result, "error", ""),
        )
    ).upper()
    return "POSITION_NOT_FOUND" in text or "POSITION NOT FOUND" in text


def _remove_live_position_state(position_id: int) -> None:
    global _prev_position_ids
    pid = int(position_id)
    # The caller has already completed and published a fresh broker
    # reconciliation.  Local cleanup may trim the advisory event projection,
    # but must never advance the authoritative reconcile timestamp.
    positions = _live_state_get("positions_event", [], clone=True) or []
    payload = _lifecycle_filter_removed_live_position(positions, position_id=pid)
    if payload["removed"]:
        _live_state_update(
            positions_event=payload["positions"],
            positions_event_reason="local_closed_position_cleanup",
        )
    _prev_position_ids.discard(pid)
    _pos_open_prices.pop(pid, None)
    _pos_open_api_volume.pop(pid, None)
    _lifecycle_forget_pending_close_state(
        pending_reasons=_pending_close_reasons,
        pending_verdicts=_pending_close_verdicts,
        position_id=pid,
    )


def _retire_broker_missing_position(
    bridge,
    position_id: int,
    *,
    broker: str,
    strategy_name: str,
    reason: str,
    persist_reconcile: bool = True,
    log=None,
) -> bool:
    from execution.deal_sync import sync_close_deals_batch

    return _runtime_retire_missing_position(
        bridge,
        position_id,
        broker=broker,
        strategy_name=strategy_name,
        reason=reason,
        log=log,
        runtime=MissingPositionRetirementRuntime(
            read_positions=lambda current_bridge: _read_positions_for_recovery(
                current_bridge,
                persist=persist_reconcile,
            ),
            normalize_position=_normalize_position_snapshot,
            load_recovery_position=_load_recovery_position_row,
            open_prices=_pos_open_prices,
            get_state_connection=_get_state_pg_conn,
            sync_close_deals_batch=sync_close_deals_batch,
            authoritative_close_pnl=_authoritative_close_pnl,
            defer_close=_defer_close_until_authoritative_deal,
            replay_close=_replay_recovered_close,
            mark_recovery_closed=_mark_recovery_position_closed,
            remove_live_position_state=_remove_live_position_state,
            now=time.time,
            replay_lookback_seconds=_RECOVERY_REPLAY_LOOKBACK_SEC,
            partial_context=_RECOVERY_CONTEXT_PARTIAL,
            debug=logger.debug,
        ),
    )


def _read_positions_for_recovery(
    bridge,
    *,
    persist: bool = True,
) -> list[Any]:
    result = _explicit_position_reconcile(bridge)
    if str(_reconcile_value(result, "status", "failed") or "failed") != "fresh":
        raise RuntimeError(
            str(_reconcile_value(result, "error_code", "") or "fresh broker reconcile unavailable")
        )
    return list(
        _publish_fresh_position_reconcile(
            result,
            broker="ctrader",
            persist=persist,
        )
    )


def _bootstrap_position_recovery(
    bridge,
    *,
    broker: str,
    strategy_name: str,
    log,
) -> bool:
    from execution.deal_sync import sync_close_deals_batch

    runtime = PositionRecoveryRuntime(
        read_positions=_read_positions_for_recovery,
        normalize_position=_normalize_position_snapshot,
        list_active_positions=_list_active_recovery_positions,
        pending_session_close_causes=_pending_session_close_causes,
        pending_close_fallback_state=_pending_close_fallback_state,
        pending_close_requirements=_pending_close_requirements,
        get_state_connection=_get_state_pg_conn,
        sync_close_deals_batch=sync_close_deals_batch,
        pending_close_cursor_overrides=_pending_close_cursor_overrides,
        pending_close_result_complete=_pending_close_result_complete,
        release_session_close_latch=_release_session_close_deal_latch,
        defer_close=_defer_close_until_authoritative_deal,
        previous_position_ids=_prev_position_ids,
        zero_confirmations=_recovery_zero_confirmations,
        zero_confirmations_required=_RECOVERY_ZERO_CONFIRMATIONS_REQUIRED,
        replay_lookback_seconds=_RECOVERY_REPLAY_LOOKBACK_SEC,
        recovery_replay_lookback_from=_lifecycle_recovery_replay_lookback_from,
        pending_close_required_volume_delta=(
            _pending_close_required_volume_delta
        ),
        replay_recovered_close=_replay_recovered_close,
        recovery_missing_position_ids=_lifecycle_recovery_missing_position_ids,
        open_prices=_pos_open_prices,
        open_api_volumes=_pos_open_api_volume,
        upsert_recovery_position=_upsert_recovery_position_state,
        now=time.time,
    )
    return _runtime_bootstrap_position_recovery(
        bridge,
        broker=broker,
        strategy_name=strategy_name,
        log=log,
        runtime=runtime,
    )


def _reset_session_state_for_new_day() -> None:
    # 从当前 account 中读取实际余额作为熔断器基准
    acct = _live_state_get("account", {}) or {}
    start_balance = float(acct.get("balance", 0) or 0)
    if start_balance <= 0:
        start_balance = 0.0  # 没有 account 信息时置 0, 熔断器 fallback 不再硬编码 1000
    _live_state_update(
        circuit_breaker=False,
        circuit_reason="",
        session_circuit_observation={
            "triggered": False,
            "reason": "",
            "enforced": False,
        },
        session_pnl=0.0,
        session_trades=0,
        session_winning=0,
        session_losing=0,
        session_trade_pnls=[],
        session_realized_pnl_legs=[],
        session_realized_legs=0,
        session_recorded_position_ids=[],
        session_consecutive_loss=0,
        session_max_drawdown_pct=0.0,
        session_peak_equity=start_balance,
        session_start_balance=start_balance,
        session_last_trade_ts=0.0,
        session_state_source="unavailable",
        session_state_status="unavailable",
        session_risk_blockers=["session_not_restored"],
        session_observed_at=0.0,
        accepting_new_risk=False,
        trade_equity_history=[start_balance] if start_balance > 0.0 else [],
    )
    _persist_session_state()


def _repair_session_start_balance_from_account(*, persist: bool = True) -> float:
    """Fill a startup-time zero baseline once broker balance becomes available."""
    existing = float(_live_state_get("session_start_balance", 0.0) or 0.0)
    if existing > 0:
        return existing
    account = _live_state_get("account", {}, clone=True) or {}
    current_balance = float(account.get("balance", 0.0) or 0.0)
    if current_balance <= 0:
        return 0.0
    session_pnl = float(_live_state_get("session_pnl", 0.0) or 0.0)
    reconstructed = current_balance - session_pnl
    if reconstructed <= 0:
        return 0.0
    _live_state_update(session_start_balance=reconstructed)
    if persist:
        _persist_session_state()
    return reconstructed


def _evaluate_daily_drawdown(risk_limits: RiskLimitSnapshot | None = None) -> dict:
    limits = risk_limits or RiskLimitSnapshot.from_runtime_config()
    session_pnl = float(_live_state_get("session_pnl", 0.0) or 0.0)
    consecutive_loss = int(
        _live_state_get("session_consecutive_loss", 0) or 0
    )
    start_balance = float(_live_state_get("session_start_balance", 0.0) or 0.0)
    if start_balance <= 0:
        return {
            "tripped": False,
            "dd_pct": 0.0,
            "reason": "",
            "session_pnl": session_pnl,
            "start_balance": 0.0,
            "risk_limits": limits.to_dict(),
        }
    dd_pct = abs(session_pnl) / start_balance * 100 if start_balance > 0 else 0.0
    prev_dd = float(_live_state_get("session_max_drawdown_pct", 0.0) or 0.0)
    updates = {"session_max_drawdown_pct": max(prev_dd, dd_pct)}
    consecutive_limit = int(limits.max_consecutive_losses)
    consecutive_tripped = (
        consecutive_limit > 0 and consecutive_loss >= consecutive_limit
    )
    drawdown_tripped = (
        limits.max_daily_loss_pct > 0
        and session_pnl < 0
        and dd_pct >= limits.max_daily_loss_pct
    )
    observed_tripped = bool(consecutive_tripped or drawdown_tripped)
    if consecutive_tripped:
        observed_reason = f"consecutive losses {consecutive_loss}"
    elif drawdown_tripped:
        observed_reason = f"daily drawdown {dd_pct:.1f}%"
    else:
        observed_reason = ""
    enforced = not bounded_demo_mode_active()
    tripped = bool(observed_tripped and enforced)
    reason = observed_reason if tripped else ""
    updates["session_circuit_observation"] = {
        "triggered": observed_tripped,
        "reason": observed_reason,
        "enforced": tripped,
    }
    if tripped:
        updates["circuit_breaker"] = True
        updates["circuit_reason"] = reason
        _maybe_update_loss_streak_book(tripped=True, reason=reason)
    elif not enforced:
        updates["circuit_breaker"] = False
        updates["circuit_reason"] = ""
        _maybe_update_loss_streak_book(tripped=False)
    _live_state_update(**updates)
    if updates:
        _persist_session_state()
    return {
        "tripped": tripped,
        "dd_pct": dd_pct,
        "reason": reason,
        "observed_tripped": observed_tripped,
        "observed_reason": observed_reason,
        "enforced": tripped,
        "session_pnl": session_pnl,
        "start_balance": start_balance,
        "risk_limits": limits.to_dict(),
    }


# ── Loss-streak probation ladder (risk/loss_streak.py owns the math) ──

def _loss_streak_ladder_facts() -> dict[str, Any]:
    """Assemble the observed facts the ladder needs, from live state.

    Session timestamps come from the broker schedule projection already
    published by market_session (single session authority); the review
    statement flag is written by the learning loop's forced review step.
    """
    book = dict(_live_state_get("loss_streak_book", {}, clone=True) or {})
    if not book:
        return {}
    session = _live_state_get("market_session", {}, clone=True) or {}
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
        "next_session_open_ts": next_open if not is_open else 0.0,
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
            trip_date=trip_date, kv_reader=_runtime_kv_get
        )
        return statement is not None
    except Exception:
        # Review pipeline unavailable -> do not hold the lock hostage.
        grace = 5400.0
        return now_ts - tripped_at >= grace


def _maybe_update_loss_streak_book(*, tripped: bool, reason: str = "") -> None:
    """Track the daily-loss trip and reset the ladder on broker-day rollover."""
    book = dict(_live_state_get("loss_streak_book", {}, clone=True) or {})
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
            _live_state_update(loss_streak_book=book)
            logger.info(
                "[loss_streak] daily limit tripped: streak=%s reason=%s",
                streak,
                reason,
            )
    elif book and str(book.get("trip_date") or "") != today:
        # Broker day rolled over without a new trip: clear the ladder so a
        # fresh day starts unconditionally clean (legacy behaviour).
        _live_state_update(loss_streak_book={})


def _record_probation_trade_outcome(pnl: float, *, position_id: int = 0) -> None:
    """Book one closed position into the probation ledger when it is armed."""
    with _LIVE_STATE_LOCK:
        book = dict(_live_state.get("loss_streak_book", {}) or {})
        if not book:
            return
        book["probation_pnl"] = float(
            book.get("probation_pnl", 0.0) or 0.0
        ) + float(pnl or 0.0)
        if position_id:
            ids = list(book.get("probation_position_ids", []) or [])
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



def _record_session_trade(
    total_pnl: float,
    *,
    position_id: int = 0,
) -> dict:
    with _LIVE_STATE_LOCK:
        pid = int(position_id or 0)
        recorded_ids = {
            int(item)
            for item in list(
                _live_state.get("session_recorded_position_ids", []) or []
            )
            if int(item or 0) > 0
        }
        if pid > 0 and pid in recorded_ids:
            return {
                "session_trades": int(_live_state.get("session_trades", 0) or 0),
                "session_winning": int(_live_state.get("session_winning", 0) or 0),
                "session_losing": int(_live_state.get("session_losing", 0) or 0),
                "session_trade_pnls": list(_live_state.get("session_trade_pnls", []) or []),
                "session_consecutive_loss": int(
                    _live_state.get("session_consecutive_loss", 0) or 0
                ),
                "session_pnl": float(_live_state.get("session_pnl", 0.0) or 0.0),
                "session_last_trade_ts": float(
                    _live_state.get("session_last_trade_ts", 0.0) or 0.0
                ),
                "session_observed_at": float(
                    _live_state.get("session_observed_at", 0.0) or 0.0
                ),
                "duplicate_position": True,
                "position_id": pid,
            }
        trades = int(_live_state.get("session_trades", 0)) + 1
        winning = int(_live_state.get("session_winning", 0))
        losing = int(_live_state.get("session_losing", 0))
        consecutive_loss = int(_live_state.get("session_consecutive_loss", 0))
        session_pnl = float(_live_state.get("session_pnl", 0.0)) + float(total_pnl)
        trade_pnls = list(_live_state.get("session_trade_pnls", []) or [])
        trade_pnls.append(float(total_pnl))
        trade_pnls = trade_pnls[-200:]
        if total_pnl > 0:
            winning += 1
            consecutive_loss = 0
        elif total_pnl < 0:
            losing += 1
            consecutive_loss += 1
        observed_at = time.time()
        if pid > 0:
            recorded_ids.add(pid)
        _live_state.update(
            session_trades=trades,
            session_winning=winning,
            session_losing=losing,
            session_trade_pnls=trade_pnls,
            session_consecutive_loss=consecutive_loss,
            session_pnl=session_pnl,
            session_last_trade_ts=observed_at,
            session_observed_at=observed_at,
            session_recorded_position_ids=sorted(recorded_ids)[-1000:],
        )
        # Probation ledger: only counts while a loss-streak book is armed.
        if _live_state.get("loss_streak_book"):
            book = dict(_live_state["loss_streak_book"])
            book["probation_pnl"] = float(
                book.get("probation_pnl", 0.0) or 0.0
            ) + float(total_pnl or 0.0)
            book["probation_trade_count"] = int(
                book.get("probation_trade_count", 0) or 0
            ) + 1
            if pid > 0:
                ids = list(book.get("probation_position_ids", []) or [])
                ids.append(int(pid))
                book["probation_position_ids"] = ids[-50:]
            _live_state["loss_streak_book"] = book
    _notify_live_state_change()
    _persist_session_state()
    return {
        "session_trades": trades,
        "session_winning": winning,
        "session_losing": losing,
        "session_trade_pnls": trade_pnls,
        "session_consecutive_loss": consecutive_loss,
        "session_pnl": session_pnl,
        "session_last_trade_ts": float(_live_state.get("session_last_trade_ts", 0.0) or 0.0),
        "session_observed_at": float(_live_state.get("session_observed_at", 0.0) or 0.0),
        "duplicate_position": False,
        "position_id": int(position_id or 0),
    }


def _get_risk_state() -> dict:
    return _live_state_get("risk", {}, clone=True) or {}


def _set_factor_snapshot(votes: dict, composite: dict) -> None:
    _live_state_update(last_factor_votes=votes, last_composite=composite)


def _set_loop_diagnostic(tick: int, bridge_status: str | None = None, *, bridge_ready: bool | None = None) -> None:
    """Record loop phase without confusing tick start with tick completion.

    ``ts`` is the public loop liveness observation and therefore advances only
    after the serial tick has completed.  Phase updates are useful diagnostics
    but must not keep ``live.loop.v2`` green while a broker/RPC call is stuck.
    """
    previous = _live_state_get("_diag", {}, clone=True) or {}
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
    _live_state_set("_diag", snapshot)


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
    _live_state_update(
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
        open_position_ids = _fresh_cached_broker_open_position_ids()
        restored = bool(
            open_position_ids is not None
            and _restore_session_state_for_day(
                today_str,
                broker_open_position_ids=open_position_ids,
            )
        )
        session_status = str(
            _live_state_get("session_state_status", "unknown") or "unknown"
        )
        if not restored or session_status != "available":
            # Preserve the last known risk projection.  A missing/corrupt
            # cache or unavailable PostgreSQL deal stream is an explicit
            # authority failure, never evidence for a zero-risk new day.
            _live_state_update(
                session_state_status=(
                    session_status
                    if session_status in {"unavailable", "degraded_cache"}
                    else "unavailable"
                ),
                session_state_source=(
                    _live_state_get("session_state_source", "unavailable")
                    if session_status == "degraded_cache"
                    else "unavailable"
                ),
                accepting_new_risk=False,
            )



def _mark_loop_stopped_for_display() -> None:
    _loop_mark_stopped_for_display(state_update=_live_state_update)


def schedule_auto_resume_loop(delay_sec: float = _AUTO_RESUME_DELAY_SEC) -> bool:
    desired = _read_loop_desired_state()
    if not desired or not desired.get("enabled"):
        return False
    if loop_status().get("running"):
        return False

    broker = str(desired.get("broker") or "ctrader")
    strategy_name = str(desired.get("strategy_name") or "factor_v4")

    def _resume():
        time.sleep(max(0.0, delay_sec))
        try:
            latest_desired = _read_loop_desired_state()
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
            logger.info("[live] auto-resume attempted: %s", result)
        except Exception as exc:
            logger.warning("[live] auto-resume failed: %s", exc)

    threading.Thread(target=_resume, name="live_loop_auto_resume", daemon=True).start()
    return True

# ── cTrader 缓存 (防 WS 1s 推送反复击中 Twisted reactor)
# audit 2026-06-08: WS _read_state_snapshot 每 1s 调 get_account/get_positions,
# 每次都走 _get_ctrader → bridge.account_info → _send (Twisted deferred) .
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
        logger.debug("load_env failed (non-critical): %s", _e)
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
                    previous_quote = _live_state_get("spot_quote", None, clone=True) or {}
                    previous_changed_at = float(_live_state_get("spot_quote_changed_at", 0.0) or 0.0)
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
                        _live_state_get("positions_event", [], clone=True)
                        or _live_state_get("positions_reconciled", [], clone=True)
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
                    _live_state_update(
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
                _live_state_update(
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
                _live_state_update(
                    positions_event=enriched,
                    positions_event_updated_at=now_ts,
                    positions_event_reason=str(payload.get("reason") or "positions_event"),
                )
                return
        except Exception as exc:
            logger.debug("[ctrader] live listener ignored %s: %s", event_type, exc)

    bridge.add_event_listener(_listener)
    setattr(bridge, "_live_service_listener_installed", True)


# ── cTrader 连接管理 ──────────────────────────────────────────────
# Twisted reactor 是全局单例, 不能 stop/restart. 每次 create+connect+destroy
# bridge 会导致 reactor 状态污染 (旧 protocol 残留).
# 方案: 进程级长连接 bridge, 所有 cTrader API 复用同一个连接.
# audit 2026-06-10: connect() 之前是同步阻塞 (reactor.startService 等回包 +
# 3 次 _send 每次 10s, 总 5-50s), 切 cTrader broker 占满 FastAPI 线程池 40 线程
# 之一, 全部其它 API 排队. 改造: _get_ctrader() 非阻塞 — 首次启动后台线程做
# 真 connect, 立刻返 (bridge, None, warming_up=True); 后续调用查 is_connected
# 属性(瞬时), 连好了返 warming_up=False, 没好返 warming_up=True.
_CTRADER_RUNTIME = CTraderRuntime(
    lock_path=Path(__file__).resolve().parent.parent.parent / "runtime" / "ctrader_session.lock",
)


def _get_ctrader():
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
        logger.debug("load_env failed (non-critical): %s", _e)
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
    bridge, err, warming = _get_ctrader()
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


def _market_session_snapshot(bridge=None, *, broker_error: str = "") -> dict[str, Any]:
    quote = {}
    now_ts = time.time()
    if bridge is not None and hasattr(bridge, "get_spot_quote"):
        try:
            quote = bridge.get_spot_quote() or {}
        except Exception:
            quote = {}
    if not quote:
        stored_quote = _live_state_get("spot_quote", None, clone=True)
        if isinstance(stored_quote, dict):
            quote = stored_quote
    quote_changed_at = float((quote or {}).get("changed_at") or _live_state_get("spot_quote_changed_at", 0.0) or 0.0)
    if quote:
        quote = {**quote, "changed_at": quote_changed_at}
    positions = _live_state_get("positions_reconciled", [], clone=True) or []
    if isinstance(positions, dict):
        positions = positions.get("positions", []) or []
    account_updated_at = float(_live_state_get("account_updated_at", 0.0) or 0.0)
    positions_updated_at = float(_live_state_get("positions_updated_at", 0.0) or 0.0)
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
            latest_market_data_ts = _df_latest_epoch(online_frame)
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
    _live_state_update(
        market_session=state,
        spot_quote=quote or _live_state_get("spot_quote", None, clone=True),
    )
    try:
        from backend.services.runtime_health_projection import RuntimeHealthProjectionService

        RuntimeHealthProjectionService().publish(
            market_session=state,
            ctrader_connected=broker_connected,
            live_loop_running=bool(_live_state_get("loop_running", False)),
            source="live_market_session",
        )
    except Exception as projection_exc:
        logger.debug("[live] runtime health projection publish failed: %s", projection_exc)
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
        logger.debug("[market_session] spot subscription refresh failed: %s", exc)


def _wait_ctrader_ready(bridge, timeout_sec: float = 30.0) -> str | None:
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
    return {
        "ctrader": {"status": ctrader_status, "error": ctrader_error},
        "loop": loop_status(),
        "readiness": get_live_readiness("ctrader"),
        "market_session": _live_state_get("market_session", {}, clone=True) or {},
        "spot_quote": _live_state_get("spot_quote", None, clone=True),
    }


def _probe_ctrader() -> tuple[str, str | None]:
    global _probe_ctrader_cache
    now = time.time()
    if _probe_ctrader_cache and (now - _probe_ctrader_cache[0]) < _CTRADER_PROBE_TTL:
        return _probe_ctrader_cache[1], _probe_ctrader_cache[2]
    # audit 2026-06-10: _get_ctrader 现在返 3-tuple; warming_up 不算 error
    bridge, err, warming = _get_ctrader()
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
        from backend.ws.endpoints import _position_to_dict
        pos_list = [_position_to_dict(p) for p in pos_list]
    # Broker snapshots are JSON projections.  Copy them at this boundary so a
    # stale/enriched compatibility dict cannot retain a recursive nested
    # reference and poison every readiness/API response built from it.
    return [
        item
        if not isinstance(item, (dict, list, tuple, set, frozenset))
        else _safe_container_snapshot(item)
        for item in list(pos_list or [])
    ]


def get_live_readiness(broker: str = "ctrader") -> dict:
    state = {
        "diag": _live_state_get("_diag", {}, clone=True) or {},
        "account_reconciled": (
            _live_state_get("account_reconciled", {}, clone=True) or {}
        ),
        "account_updated_at": _live_state_get("account_updated_at", 0.0),
        "positions_reconciled": _live_state_get(
            "positions_reconciled", [], clone=True
        ),
        "positions_updated_at": _live_state_get("positions_updated_at", 0.0),
        "account_reconcile_id": _live_state_get("account_reconcile_id", ""),
        "positions_reconcile_id": _live_state_get("positions_reconcile_id", ""),
        "account_reconcile_failed_at": _live_state_get(
            "account_reconcile_failed_at", 0.0
        ),
        "positions_reconcile_failed_at": _live_state_get(
            "positions_reconcile_failed_at", 0.0
        ),
        "account_reconcile_error": _live_state_get(
            "account_reconcile_error", ""
        ),
        "positions_reconcile_error": _live_state_get(
            "positions_reconcile_error", ""
        ),
        "account_event_updated_at": _live_state_get(
            "account_event_updated_at", 0.0
        ),
        "positions_event_updated_at": _live_state_get(
            "positions_event_updated_at", 0.0
        ),
        "account_event_reason": _live_state_get("account_event_reason", None),
        "positions_event_reason": _live_state_get(
            "positions_event_reason", None
        ),
        "positions_component_facts": (
            _live_state_get("positions_component_facts", {}, clone=True) or {}
        ),
    }
    positions = _coerce_live_positions(
        _live_state_get("positions_reconciled", [], clone=True)
    )
    broker_status = "unknown"
    broker_error = None
    if broker == "ctrader":
        broker_status, broker_error = _probe_ctrader()
    return build_live_readiness(
        loop=loop_status(),
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
    if _live_state_get("loop_running") and _live_state_get("broker") == broker:
        acct = _live_state_get("account_reconciled", clone=True)
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
            # audit 2026-06-10: _get_ctrader 返 3-tuple, warming_up 短路
            bridge, err, warming = _get_ctrader()
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
                cached = _live_state_get("account_reconciled", {}, clone=True) or {}
                cached_at = float(_live_state_get("account_updated_at", 0.0) or 0.0)
                cached_id = str(_live_state_get("account_reconcile_id", "") or "")
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
            _live_state_update(
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
            _live_state_get("loop_running")
            and _live_state_get("broker") == broker
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

    if _live_state_get("loop_running") and _live_state_get("broker") == broker:
        cached_at = float(_live_state_get("positions_updated_at", 0.0) or 0.0)
        cached_id = str(_live_state_get("positions_reconcile_id", "") or "")
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
        cached_positions = _live_state_get("positions_reconciled", clone=True)
        if cached_positions is not None and _live_state_get("loop_running"):
            return {"ok": True, "broker": "ctrader", "positions": _visible_positions(cached_positions), "readiness": readiness}
        # 缓存空 fallback
        def _fetch():
            # audit 2026-06-10: _get_ctrader 返 3-tuple, warming_up 短路
            bridge, err, warming = _get_ctrader()
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
                cached_at = float(_live_state_get("positions_updated_at", 0.0) or 0.0)
                cached_id = str(_live_state_get("positions_reconcile_id", "") or "")
                cached = _coerce_live_positions(
                    _live_state_get("positions_reconciled", [], clone=True)
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
_live_safety_plane: LiveSafetyPlane | None = None
_live_safety_plane_owner: str = ""
_live_safety_watchdog: LiveSafetyWatchdog | None = None
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


def _get_live_safety_plane(generation_id: str = "") -> LiveSafetyPlane:
    global _live_safety_plane, _live_safety_plane_owner
    owner = str(generation_id or "unowned")
    mode = str(_phase2_feature_flags().live_safety_plane_v2_mode)
    if (
        _live_safety_plane is None
        or _live_safety_plane_owner != owner
        or _live_safety_plane.mode != mode
    ):
        _live_safety_plane = LiveSafetyPlane(mode=mode)
        _live_safety_plane_owner = owner
    if mode == "enforce" and not _live_safety_plane.forced_shadow:
        # Re-read the persisted fail-closed cause for an existing generation;
        # a restart or another process must not silently clear it.
        persisted_override = safety_v2_forced_shadow_status()
        if bool(persisted_override.get("active")):
            _live_safety_plane.force_shadow(
                str(
                    persisted_override.get("reason")
                    or "persisted_safety_v2_forced_shadow"
                )
            )
    return _live_safety_plane


def _live_safety_watchdog_probe() -> dict[str, Any]:
    """Return process facts only; the watchdog never calls the broker."""

    safety = _live_state_get("safety_plane", {}, clone=True) or {}
    heartbeat_at = float(safety.get("heartbeat_at", 0.0) or 0.0)
    controller = _LIVE_LOOP_CONTROLLER.status()
    thread_alive = bool(controller.get("thread_alive"))
    heartbeat_at = float(controller.get("safety_heartbeat_at", 0.0) or 0.0)
    unknown_raw = safety.get("unknown_execution_count")
    return {
        # The generation controller is the authoritative lifecycle owner; the
        # watchdog only observes its heartbeat and never calls the broker.
        "enabled": bool(
            thread_alive
            or controller.get("phase") in {"starting", "running", "degraded", "draining"}
        ),
        "running": thread_alive,
        "started_at": float(controller.get("created_at") or 0.0),
        "safety_heartbeat_at": heartbeat_at,
        # This is only a liveness hint for an active serial cycle.  It is not
        # a completed Safety fact and is never used by the open admission
        # boundary as proof of fresh account/positions.
        "safety_cycle_active": bool(
            _live_state_get("safety_cycle_active", False)
        ),
        "safety_cycle_progress_at": float(
            _live_state_get("safety_cycle_progress_at", 0.0) or 0.0
        ),
        "account_updated_at": float(_live_state_get("account_updated_at", 0.0) or 0.0),
        "positions_updated_at": float(_live_state_get("positions_updated_at", 0.0) or 0.0),
        "unknown_execution_count": unknown_raw,
    }


def _persist_safety_fail_closed(
    *,
    blockers: list[str] | tuple[str, ...],
    source: str,
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Durably block new risk without changing any broker action result."""

    normalized = sorted({str(item) for item in blockers if str(item)}) or [
        "safety_state_unavailable"
    ]
    latch = no_new_risk_latch_status(fail_closed=True)
    forced_shadow = "safety_v2_forced_shadow" in normalized
    # A normal freshness failure has no bearing on the V2 candidate authority.
    # Avoid replaying the separate forced-shadow projection (and its history)
    # on every watchdog probe; only the forced-shadow path needs it.
    persisted_forced_shadow = (
        safety_v2_forced_shadow_status()
        if forced_shadow
        else {"active": False}
    )
    latch_cause = "safety_v2_forced_shadow" if forced_shadow else "safety_freshness"
    latch_cause_id = (
        "candidate_comparison" if forced_shadow else str(source or "safety")
    )
    active_cause_keys = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(latch.get("causes") or [])
        if isinstance(item, dict)
    }
    needs_latch_record = (latch_cause, latch_cause_id) not in active_cause_keys or (
        forced_shadow and not bool(persisted_forced_shadow.get("active"))
    )
    if needs_latch_record:
        try:
            latch = activate_no_new_risk_latch(
                reason=(
                    "safety_v2_forced_shadow"
                    if forced_shadow
                    else "safety_freshness_failed"
                ),
                actor=f"system:{source or 'safety'}",
                metadata={
                    "blockers": normalized,
                    "error": str(error or "")[:1000],
                    "forced_shadow": forced_shadow,
                    **dict(metadata or {}),
                },
                cause=latch_cause,
                cause_id=latch_cause_id,
            )
        except Exception as exc:
            # activate_no_new_risk_latch installs an in-process fail-closed
            # latch before raising on storage failure.
            latch = no_new_risk_latch_status(fail_closed=True)
            error = f"{error}; latch_error={type(exc).__name__}:{exc}".strip("; ")
    payload = {
        "schema_version": "live_safety_failure.v1",
        "status": "no_new_risk_latched",
        "source": str(source or "safety"),
        "blockers": normalized,
        "error": str(error or "")[:2000],
        "detected_at": time.time(),
        "latch": dict(latch or {}),
    }
    _live_state_update(
        accepting_new_risk=False,
        safety_failure=payload,
        no_new_risk_latch=latch,
    )
    current = _LIVE_LOOP_CONTROLLER.current()
    if current is not None:
        try:
            _LIVE_LOOP_CONTROLLER.update_runtime_health(
                current.generation_id,
                blockers=tuple(normalized),
            )
        except RuntimeError:
            pass
    if needs_latch_record or error:
        try:
            append_safety_outbox(
                event_type="live_safety_fail_closed",
                payload=payload,
                error=str(error or ""),
            )
        except Exception as outbox_exc:
            logger.error("[live] safety fail-closed outbox unavailable: %s", outbox_exc)
    return payload


def _on_live_safety_watchdog_violation(result: SafetyFreshnessResult) -> None:
    _persist_safety_fail_closed(
        blockers=result.blockers,
        source="safety_watchdog",
    )


def _on_live_safety_watchdog_recovery(result: SafetyFreshnessResult) -> None:
    """Release freshness causes only after sustained, cause-specific recovery."""

    latch = no_new_risk_latch_status(fail_closed=True)
    active_causes = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or "")): item
        for item in list(latch.get("causes") or [])
        if isinstance(item, dict)
    }
    base_evidence = {
        "state": result.state,
        "ages": dict(result.ages),
        "blockers": list(result.blockers),
        "recovery_checks": 3,
    }
    released = latch
    watchdog_cause = ("safety_freshness", "safety_watchdog")
    if watchdog_cause in active_causes:
        released = release_no_new_risk_latch_cause(
            cause=watchdog_cause[0],
            cause_id=watchdog_cause[1],
            reason="safety_freshness_sustained_recovery",
            actor="system:safety_watchdog",
            evidence=base_evidence,
        )

    # ``live_loop`` is a separate cause from the watchdog observation itself.
    # A fresh watchdog snapshot alone is not enough to release it: the
    # canonical tick owner must have completed a normal Safety cycle and both
    # account/position reconciles must be identifiable and current.  Keep the
    # check here, at the existing latch owner, so no caller can accidentally
    # turn a healthy timestamp into a blanket thaw.
    live_loop_cause = ("safety_freshness", "live_loop")
    live_loop_record = active_causes.get(live_loop_cause) or {}
    live_loop_probe: dict[str, Any] = {}
    safety_payload: dict[str, Any] = {}
    account_snapshot: dict[str, Any] = {}
    positions_snapshot: Any = None
    live_loop_recovered = False
    reconciliation_blockers: list[str] = []
    independent_safety_causes = {
        key
        for key in active_causes
        if key[0] == "safety_freshness"
        and key not in {watchdog_cause, live_loop_cause}
    }
    if (
        live_loop_record
        and not independent_safety_causes
        and result.enabled
        and result.running
        and result.ok
        and result.state == "current"
    ):
        try:
            live_loop_probe = dict(_live_safety_watchdog_probe() or {})
            safety_payload = dict(
                _live_state_get("safety_plane", {}, clone=True) or {}
            )
            account_snapshot = dict(
                _live_state_get("account_reconciled", {}, clone=True) or {}
            )
            positions_snapshot = _live_state_get(
                "positions_reconciled", None, clone=True
            )
            reconciliation_blockers = _new_risk_reconciliation_blockers()
            account_id = str(
                _live_state_get("account_reconcile_id", "") or ""
            )
            positions_id = str(
                _live_state_get("positions_reconcile_id", "") or ""
            )
            live_loop_recovered = bool(
                live_loop_probe.get("running")
                and bool(_live_state_get("loop_running", False))
                and str(
                    _live_state_get("session_state_status", "unknown")
                    or "unknown"
                )
                == "available"
                and bool(account_snapshot.get("ok"))
                and bool(account_id)
                and isinstance(positions_snapshot, list)
                and bool(positions_id)
                and bool(safety_payload.get("accepting_new_risk"))
                and not bool(live_loop_probe.get("safety_cycle_active"))
                and not reconciliation_blockers
                and str(safety_payload.get("status") or "")
                not in {"", "exception", "failed", "unavailable"}
            )
        except Exception as exc:
            logger.debug(
                "[live] live-loop latch recovery evidence unavailable: %s",
                exc,
            )
            live_loop_recovered = False
    if live_loop_record and live_loop_recovered:
        released = release_no_new_risk_latch_cause(
            cause=live_loop_cause[0],
            cause_id=live_loop_cause[1],
            reason="live_loop_safety_reconcile_recovered",
            actor="system:safety_watchdog",
            evidence={
                **base_evidence,
                "live_loop_heartbeat_at": live_loop_probe.get(
                    "safety_heartbeat_at"
                ),
                "account_reconcile_id": str(
                    _live_state_get("account_reconcile_id", "") or ""
                ),
                "positions_reconcile_id": str(
                    _live_state_get("positions_reconcile_id", "") or ""
                ),
                "session_state_status": str(
                    _live_state_get("session_state_status", "unknown")
                    or "unknown"
                ),
                "safety_status": str(safety_payload.get("status") or ""),
                "reconciliation_blockers": reconciliation_blockers,
            },
        )

    supervisor_cause = ("safety_freshness", "supervisor_tighten")
    supervisor_record = active_causes.get(supervisor_cause) or {}
    supervisor_metadata = dict(supervisor_record.get("metadata") or {})
    supervisor_error = str(supervisor_metadata.get("error") or "")
    if (
        supervisor_record
        and supervisor_error
        == "amend_projection_unverified:position_missing_after_amend"
    ):
        open_position_ids = _fresh_cached_broker_open_position_ids()
        try:
            target_position_id = int(
                supervisor_metadata.get("position_id") or 0
            )
        except (TypeError, ValueError):
            target_position_id = 0
        target_confirmed_absent = (
            open_position_ids is not None
            and (
                target_position_id not in open_position_ids
                if target_position_id > 0
                else not open_position_ids
            )
        )
        if target_confirmed_absent:
            released = release_no_new_risk_latch_cause(
                cause=supervisor_cause[0],
                cause_id=supervisor_cause[1],
                reason="supervisor_tighten_position_absence_confirmed",
                actor="system:safety_watchdog",
                evidence={
                    **base_evidence,
                    "position_id": target_position_id or None,
                    "open_position_ids": sorted(open_position_ids),
                    "positions_reconcile_id": str(
                        _live_state_get("positions_reconcile_id", "") or ""
                    ),
                },
            )

    safety_failure = _live_state_get("safety_failure", {}, clone=True) or {}
    updates: dict[str, Any] = {"no_new_risk_latch": released}
    remaining_causes = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(
            released.get("remaining_causes") or released.get("causes") or []
        )
        if isinstance(item, dict)
    }
    failure_source = str(safety_failure.get("source") or "")
    if (
        failure_source == "safety_watchdog"
        and watchdog_cause not in remaining_causes
    ) or (
        failure_source == "supervisor_tighten"
        and supervisor_cause not in remaining_causes
    ) or (
        failure_source == "live_loop"
        and live_loop_cause not in remaining_causes
    ):
        updates["safety_failure"] = {}
    _live_state_update(**updates)

    # The watchdog owns the durable recovery edge, but the live loop owns a
    # separate in-memory admission projection.  Keep the two projections
    # linearized here: otherwise a cleared latch can leave the loop degraded
    # until a later tick happens to republish the positive edge.  Recovery is
    # still fail-closed: every current safety/reconcile fact must be known and
    # no independent latch cause may remain before the projection can reopen.
    recovery_blockers: list[str] = []
    recovery_ready = bool(
        result.enabled
        and result.running
        and result.ok
        and result.state == "current"
    )
    if recovery_ready:
        recovery_blockers.extend(str(item) for item in result.blockers if str(item))
        if bool(released.get("active")):
            recovery_blockers.append("no_new_risk_latched")

        projected_failure = (
            {}
            if updates.get("safety_failure") == {}
            else safety_failure
        )
        if projected_failure:
            recovery_blockers.extend(
                str(item)
                for item in (projected_failure.get("blockers") or [])
                if str(item)
            )
            if not projected_failure.get("blockers"):
                recovery_blockers.append(
                    f"safety_failure:{failure_source or 'unknown'}"
                )

        safety_payload = _live_state_get("safety_plane", {}, clone=True) or {}
        if not isinstance(safety_payload, dict) or not bool(
            safety_payload.get("accepting_new_risk")
        ):
            recovery_blockers.extend(
                str(item)
                for item in (
                    safety_payload.get("blockers", [])
                    if isinstance(safety_payload, dict)
                    else []
                )
                if str(item)
            )
            if not recovery_blockers or recovery_blockers[-1] != "no_new_risk_latched":
                recovery_blockers.append("safety_not_accepting_new_risk")

        recovery_blockers.extend(_new_risk_reconciliation_blockers())
        if (
            str(_live_state_get("session_state_status", "unknown") or "unknown")
            != "available"
        ):
            recovery_blockers.append("session_state_unavailable")
        if bool(_live_state_get("circuit_breaker", False)):
            recovery_blockers.append("session_circuit_breaker")

    normalized_recovery_blockers = sorted(set(recovery_blockers))
    current = _LIVE_LOOP_CONTROLLER.current()
    if current is not None:
        try:
            _LIVE_LOOP_CONTROLLER.update_runtime_health(
                current.generation_id,
                blockers=tuple(normalized_recovery_blockers)
                if recovery_ready
                else ("safety_recovery_not_ready",),
            )
            _live_state_update(
                accepting_new_risk=_LIVE_LOOP_CONTROLLER.accepting_new_risk(
                    current.generation_id
                )
            )
        except RuntimeError:
            pass


def _start_live_safety_watchdog() -> bool:
    global _live_safety_watchdog
    if _live_safety_watchdog is None:
        _live_safety_watchdog = LiveSafetyWatchdog(
            probe=_live_safety_watchdog_probe,
            on_violation=_on_live_safety_watchdog_violation,
            on_recovery=_on_live_safety_watchdog_recovery,
            recovery_checks=3,
            interval_sec=5.0,
            stale_after_sec=_LIVE_SAFETY_FRESHNESS_SEC,
        )
    return _live_safety_watchdog.start()


def _stop_live_safety_watchdog() -> None:
    global _live_safety_watchdog
    watchdog = _live_safety_watchdog
    if watchdog is not None:
        watchdog.stop(timeout_sec=2.0)
    _live_safety_watchdog = None


def _scheduled_awe_adapt():
    """每 30 分钟: AWE 权重自适应 (如果 factor pipeline 和 attribution 可用)。

    从 _factor_pipeline 读取 attribution engine, 触发权重调整。
    不阻塞, 异常只 log 不抛。
    """
    try:
        # The scheduler is process-owned so health/readiness/data maintenance
        # survives a stopped or recovering live generation.  AWE, however,
        # consumes generation-owned in-memory attribution and must never adapt
        # weights from a dead loop's stale pipeline.
        if not bool(loop_status().get("running")):
            logger.debug("[awe_adapt] skip: live loop not running")
            return

        # 原子快照 — 防止 live loop 重置 _factor_pipeline 时的 TOCTOU
        fp = _factor_pipeline
        if fp is None:
            logger.debug("[awe_adapt] skip: factor pipeline not active")
            return

        # 进一步检查每个子组件是否可用
        attr = fp.get("attribution")
        awe = fp.get("awe")
        engine_ref = fp.get("engine")
        if attr is None:
            logger.debug("[awe_adapt] skip: attribution engine not available")
            return
        if awe is None:
            logger.debug("[awe_adapt] skip: AWE not initialized")
            return

        from config.runtime_config import shared as _rc
        cfg = _rc()
        if autonomy_expansion_freeze_applies(cfg):
            logger.info("[awe_adapt] skipped: autonomy expansion frozen")
            return

        # 检查交易笔数门槛
        all_stats = attr.get_all_factor_stats()
        total_trades = sum(s.n_trades for s in all_stats.values())
        if total_trades < cfg.awe_min_trades:
            logger.debug("[awe_adapt] skip: only {} trades (min {})",
                         total_trades, cfg.awe_min_trades)
            return

        # Phase 1: 提取因子历史供 CausalCheck + blend_baseline 使用
        fv_dict: dict = {}
        fwd_ret: "np.ndarray | None" = None  # type: ignore[name-defined]
        engine = fp.get("engine")
        if engine is not None and hasattr(engine, "export_factor_history"):
            try:
                fv_dict, fwd_arr = engine.export_factor_history()
                fwd_ret = fwd_arr if len(fwd_arr) > 0 else None
                # Feed ICTracker for AWE IC gate
                ictracker = fp.get("ic_tracker")
                if ictracker is not None and fv_dict and fwd_ret is not None:
                    for fname, fvals in fv_dict.items():
                        try:
                            min_len = min(len(fvals), len(fwd_ret))
                            if min_len >= 2:
                                ictracker.update(fname, fvals[:min_len], fwd_ret[:min_len])
                        except Exception as _e:
                            logger.debug("[awe_adapt] ictracker.update failed for {}: {}: {}", fname, type(_e).__name__, _e)
            except Exception as _e2:
                logger.debug("[awe_adapt] export_factor_history failed: {}: {}", type(_e2).__name__, _e2)

        # 如果因子数据充足且 blend baseline 未计算, 触发计算
        use_blend = bool(awe._blend_baselines)
        if not use_blend and fv_dict and fwd_ret is not None and len(fwd_ret) > 50:
            try:
                from alpha.portfolio_compositor import resolve_factor_role

                alpha_names = {
                    n for n, sc in (cfg.factor_signal_config or {}).items()
                    if resolve_factor_role(n, sc if isinstance(sc, dict) else None) == "alpha"
                }
                f_names = [
                    n for n in fv_dict
                    if n in cfg.factor_portfolio_weights and (not alpha_names or n in alpha_names)
                ]
                if len(f_names) >= 3:
                    factor_mat = _np.column_stack([
                        fv_dict[n][:len(fwd_ret)] for n in f_names
                    ])
                    awe.compute_blend_baseline(factor_mat, fwd_ret[:len(fwd_ret)], f_names)
                    use_blend = True
            except Exception as _e2:
                logger.debug("[awe_adapt] blend_baseline compute failed: {}: {}", type(_e2).__name__, _e2)

        current_weights = dict(cfg.factor_portfolio_weights or {})
        factor_configs = _merge_portfolio_configs(
            cfg.factor_signal_config,
            current_weights,
            cfg.factor_tactical_alpha,
            cfg.factor_signal_threshold,
        )
        patches = awe.adapt(attr, factor_configs,
                           use_blend_baseline=use_blend,
                           factor_values=fv_dict if fv_dict else None,
                           forward_returns=fwd_ret)
        if patches:
            logger.info("[awe_adapt] adapted {} factors: {}",
                        len(patches),
                        {k: v["weight"] for k, v in patches.items()})
            # All producers share the same decision/admission/application boundary.
            try:
                from backend.services.factor_weight_change import FactorWeightChangeService

                run_id = f"awe_adapt_{int(time.time())}"

                def _awe_risk_check(plan: dict[str, Any]):
                    partial = dict(plan.get("proposed_weights") or {})
                    return RiskPolicyService.shared().evaluate(
                        "update_weight",
                        {
                            "source": "awe_adapt",
                            "required_mode": "autonomous_governance",
                            "changed_factors": sorted(partial),
                            "current_weights": current_weights,
                            "proposed_weights": partial,
                        },
                    )

                result = FactorWeightChangeService().execute(
                    source="awe_decision_policy_update_weight",
                    producer="awe_adapt",
                    run_id=run_id,
                    actor="system:awe_adapt",
                    reason="AWE weight patch merged by governed weight service",
                    awe_patches=patches,
                    weight_policy_weights=None,
                    factor_configs=factor_configs,
                    current_weights=current_weights,
                    fast=True,
                    risk_check=_awe_risk_check,
                )
                partial = dict(result.get("proposed_weights") or {})
                status = str(result.get("status") or "")
                if status == "blocked_by_risk":
                    logger.info(
                        "[awe_adapt] RiskPolicy blocked weight update run_id={} reason={}",
                        run_id,
                        (result.get("risk_verdict") or {}).get("reason"),
                    )
                    return
                if status == "governance_error":
                    logger.error(
                        "[awe_adapt] governed weight update failed run_id={} stage={} error={}: {}",
                        run_id,
                        result.get("error_stage") or "unknown",
                        result.get("error_type") or "Error",
                        result.get("error") or "unknown",
                    )
                    return
                if status != "applied" or not partial:
                    admission_summary = {}
                    for name, item in (result.get("admissions") or {}).items():
                        item = item if isinstance(item, dict) else {}
                        active = item.get("active_application") or {}
                        active = active if isinstance(active, dict) else {}
                        admission_summary[name] = {
                            "status": item.get("status") or "",
                            "reason": item.get("reason") or "",
                            "active_application_id": active.get("application_id") or "",
                            "active_application_status": active.get("application_status") or "",
                            "active_effect_status": active.get("effect_status") or "",
                        }
                    logger.info(
                        "[awe_adapt] weight update not applied run_id={} status={} admission_status={} admissions={}",
                        run_id,
                        status or "unknown",
                        result.get("admission_status") or "",
                        admission_summary,
                    )
                    return
                logger.info(
                    "[awe_adapt] weights pushed via governed service (%d changed, %d total)",
                    len(partial),
                    len(current_weights),
                )
            except Exception as _e2:
                logger.opt(exception=True).error(
                    "[awe_adapt] unexpected weight push exception run_id={} error={}: {}",
                    locals().get("run_id", "unassigned"),
                    type(_e2).__name__,
                    _e2,
                )
        else:
            logger.debug("[awe_adapt] no weight changes needed")
    except Exception as e:
        logger.warning("[awe_adapt] failed: {}", e)




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

    session = _live_state_get("market_session", {}, clone=True) or {}
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

    # AWE consumes the live process' in-memory attribution/pipeline state.  It
    # therefore belongs to the backend even when CPU-heavy research jobs are
    # delegated to the learning worker.  Offset it from governance/nursery
    # minutes to avoid decisions from the same evidence window racing.
    sched.add_job("awe_adapt", "8,38 * * * *", _scheduled_awe_adapt)

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
            get_ctrader=_get_ctrader,
            market_session_snapshot=_market_session_snapshot,
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
        # awe_adapt 始终由持有 live pipeline 的 backend 注册。
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


def _stop_live_scheduler():
    """停止 Scheduler. 幂等. wait=False 避免阻塞."""
    from backend.runtime.scheduler import InProcessScheduler
    sched = InProcessScheduler()
    try:
        sched.stop(wait=False)
        logger.info("[live] InProcessScheduler stopped")
    except Exception as e:
        logger.debug("[live] scheduler stop: {}", e)


def loop_status() -> dict:
    """Return the canonical generation status and read-only live projections."""
    with _loop_state_lock:
        generation = _LIVE_LOOP_CONTROLLER.status()
        identity = _loop_identity_snapshot(
            generation=generation,
        )
        freshness = evaluate_safety_freshness(
            _live_safety_watchdog_probe(),
            now=time.time(),
            stale_after_sec=_LIVE_SAFETY_FRESHNESS_SEC,
        )
        local_blockers: list[str] = []
        if freshness.enabled and freshness.running and not freshness.ok:
            local_blockers.extend(freshness.blockers)
        if no_new_risk_latched(fail_closed=True):
            local_blockers.append("no_new_risk_latched")
        safety_payload = _live_state_get("safety_plane", {}, clone=True) or {}
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
            reconcile_blockers = _new_risk_reconciliation_blockers()
            local_blockers.extend(reconcile_blockers)
            session_status = str(
                _live_state_get("session_state_status", "unknown") or "unknown"
            )
            if session_status != "available":
                local_blockers.append(f"session_state_{session_status}")
            if bool(_live_state_get("circuit_breaker", False)):
                local_blockers.append("session_circuit_breaker")
            market_session = _live_state_get("market_session", {}, clone=True) or {}
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
                _live_state_update(
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
            "safety": _live_state_get("safety_plane", {}, clone=True) or {},
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
        persist_desired_state=_persist_loop_desired_state,
        prime_live_loop_state=_prime_live_loop_state,
        start_safety_watchdog=_start_live_safety_watchdog,
        start_scheduler=_start_live_scheduler,
        stop_scheduler=_stop_live_scheduler,
        stop_safety_watchdog=_stop_live_safety_watchdog,
        thread_factory=threading.Thread,
        loop_target=_run_loop,
        live_state_update=_live_state_update,
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
                _live_state_update(
                    loop_shutdown=draining,
                    accepting_new_risk=False,
                )
                if stop_flag is not None:
                    stop_flag.set()
            result = None

    if result is not None:
        _live_state_update(loop_shutdown=result, accepting_new_risk=False)
        _runtime_kv_set(_RUNTIME_KV_LAST_SHUTDOWN, result)
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
        _live_state_update(loop_shutdown=result, accepting_new_risk=False)
        _runtime_kv_set(_RUNTIME_KV_LAST_SHUTDOWN, result)
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
        _live_state_update(loop_shutdown=result, accepting_new_risk=False)
        _runtime_kv_set(_RUNTIME_KV_LAST_SHUTDOWN, result)
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
    _live_state_update(loop_shutdown=result, accepting_new_risk=False)
    _runtime_kv_set(_RUNTIME_KV_LAST_SHUTDOWN, result)
    logger.info(
        f"[live] process shutdown completed; ownership_released={ownership_released}"
    )
    return result


def _live_loop_stop_runtime() -> LiveLoopStopRuntime:
    return LiveLoopStopRuntime(
        state_lock=_loop_state_lock,
        controller=_LIVE_LOOP_CONTROLLER,
        admission_lock=_OPEN_TRADE_ADMISSION_LOCK,
        live_state_update=_live_state_update,
        persist_desired_state=_persist_loop_desired_state,
        runtime_kv_set=_runtime_kv_set,
        last_shutdown_key=_RUNTIME_KV_LAST_SHUTDOWN,
        now=time.time,
        thread_factory=threading.Thread,
        persist_safety_fail_closed=_persist_safety_fail_closed,
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


def _warmup_from_local_db(symbol: str = "XAUUSD+", timeframe: str = "M15", n_bars: int = 200) -> "pd.DataFrame | None":
    """从本地 DuckDB 直接拉历史 bar 预热 strategy 指标。

    直接连接 DuckDB 执行 SELECT, 绕开 DataStore 单例/并发写入冲突。
    实时 tick 走 broker spot event, 这里只保证 strategy 暖机有数据。
    """
    import time as _time
    from backend.core.db import (
        bars_monthly_read_paths,
        duckdb_readonly_connection,
    )

    db_paths = bars_monthly_read_paths(newest_first=True)
    target_bars = int(n_bars)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            remaining = target_bars
            frames = []
            for db_path in db_paths:
                if remaining <= 0:
                    break
                with duckdb_readonly_connection(
                    str(db_path), snapshot_first=True
                ) as conn:
                    frame = conn.execute(
                        "SELECT time, open, high, low, close, volume "
                        "FROM bars WHERE symbol=? AND timeframe=? "
                        "ORDER BY time DESC LIMIT ?",
                        [symbol, timeframe, remaining],
                    ).df()
                if frame is not None and len(frame) > 0:
                    frames.append(frame)
                    remaining -= len(frame)

            if not frames:
                logger.warning(f"DuckDB has no bars for {symbol} {timeframe}")
                return None
            df = pd.concat(frames, ignore_index=True)
            df = (
                df.drop_duplicates(subset=["time"], keep="first")
                .sort_values("time")
                .tail(target_bars)
            )
            # time 是 epoch 秒, 转 datetime index
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.set_index("time").sort_index()
            return df[["open", "high", "low", "close", "volume"]]
        except Exception as e:
            if attempt < max_retries - 1:
                delay = 1.0 * (2 ** attempt)
                logger.warning(f"_warmup_from_local_db attempt {attempt+1}/{max_retries} failed: {e}, retrying in {delay}s")
                _time.sleep(delay)
            else:
                logger.warning(f"_warmup_from_local_db failed after {max_retries} attempts: {e}")
                return None


def _fetch_bars_with_retry(bridge, timeframe: str, n_bars: int, max_retries: int = 3) -> "pd.DataFrame | None":
    """fetch_bars 重试 wrapper. 失败 1 次不致命, 指数 backoff 2s/4s/8s.
    返 None 表示彻底失败 (调用方决定是否继续).

    Startup-only historical seed.  Runtime ticks consume the bridge's
    in-memory live trendbar feed and never call this wrapper.
    """
    for attempt in range(max_retries):
        try:
            df = bridge.fetch_bars(timeframe=timeframe, n_bars=n_bars)
            if df is not None and len(df) >= 30:
                return df
        except Exception as e:
            logger.warning(f"fetch_bars attempt {attempt+1}/{max_retries} failed: {e}")
        if attempt < max_retries - 1:
            time.sleep(2 ** (attempt + 1))  # 2s, 4s, 8s
    return None


def _get_live_bars(
    symbol: str = "XAUUSD+",
    timeframe: str = "M5",
    n_bars: int = 500,
) -> "pd.DataFrame | None":
    """Read the in-memory cTrader trendbar feed without touching DuckDB."""
    try:
        bridge, error, warming = _get_ctrader()
    except Exception as exc:
        logger.debug("online trendbar bridge lookup failed: %s", exc)
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
            "online trendbar read failed: symbol=%s timeframe=%s error=%s",
            symbol,
            timeframe,
            exc,
        )
        return None


def _df_latest_epoch(df: "pd.DataFrame | None") -> float:
    if df is None or len(df) == 0:
        return 0.0
    try:
        idx = df.index[-1]
        return float(idx.timestamp()) if hasattr(idx, "timestamp") else float(idx)
    except Exception:
        return 0.0


def _closed_decision_bar_frame(
    df: "pd.DataFrame | None",
    *,
    timeframe: str,
    now_ts: float,
) -> "pd.DataFrame | None":
    if df is None or len(df) == 0:
        return df
    freshness = _sync_classify_decision_bar_freshness(
        latest_ts=_df_latest_epoch(df),
        timeframe=timeframe,
        now=now_ts,
    )
    expected_ts = float(freshness.get("expected_closed_bar_ts", 0.0) or 0.0)
    if expected_ts <= 0:
        return df
    try:
        keep = []
        for idx in df.index:
            ts = float(idx.timestamp()) if hasattr(idx, "timestamp") else float(idx)
            keep.append(ts <= expected_ts)
        filtered = df.loc[keep]
        return filtered if filtered is not None and len(filtered) > 0 else df.iloc[0:0]
    except Exception:
        return df


def _clamp_last_closed_bar_close_to_spot(
    df: "pd.DataFrame | None",
    *,
    timeframe: str,
    now_ts: float,
    quote_provider: Any = None,
) -> tuple["pd.DataFrame | None", dict[str, Any]]:
    """Clamp the just-closed bar's stream close toward the live spot mid.

    Spot-stream trendbar frames report ``close`` as the frame-moment price,
    so when a bar's final stream update never arrives the cached close
    freezes wherever the last frame caught the tape (observed close==low on
    2026-08-24).  Within seconds of the bar close the continuous bid/ask
    mid is the best in-memory estimate of that bar's true close, so this
    clamps only the newest closed bar and only while the estimate is fresh
    and sane.
    """
    info: dict[str, Any] = {}
    if df is None or getattr(df, "empty", False) or len(df) == 0:
        return df, info
    if not callable(quote_provider):
        return df, info
    freshness = _sync_classify_decision_bar_freshness(
        latest_ts=_df_latest_epoch(df),
        timeframe=timeframe,
        now=now_ts,
    )
    expected_ts = float(freshness.get("expected_closed_bar_ts", 0.0) or 0.0)
    if expected_ts <= 0:
        return df, info
    try:
        last_idx = df.index[-1]
        last_ts = (
            float(last_idx.timestamp())
            if hasattr(last_idx, "timestamp")
            else float(last_idx)
        )
    except Exception:
        return df, info
    if abs(last_ts - expected_ts) > 1e-6:
        # Newest row is not the just-closed bar; nothing to clamp.
        return df, info
    try:
        last_low = float(df["low"].iloc[-1])
        last_high = float(df["high"].iloc[-1])
        last_close = float(df["close"].iloc[-1])
    except Exception:
        return df, info
    degenerate = abs(last_close - last_low) < 1e-9 and last_high > last_low
    if not degenerate:
        return df, info
    period_seconds = max(1, _timeframe_seconds(timeframe))
    try:
        quote = quote_provider() or {}
    except Exception:
        return df, info
    bid = float(quote.get("bid") or 0.0)
    ask = float(quote.get("ask") or 0.0)
    quote_ts = float(quote.get("ts") or 0.0)
    if bid <= 0.0 or ask <= 0.0 or ask < bid:
        info["close_clamp_skipped"] = "quote_unusable"
        return df, info
    bar_close_time = expected_ts + period_seconds
    if quote_ts > 0.0 and quote_ts < bar_close_time - 2.0:
        info["close_clamp_skipped"] = "quote_predates_close"
        return df, info
    mid = round((bid + ask) / 2.0, 5)
    span = max(1e-9, last_high - last_low)
    tolerance = max(0.5 * span, 10.0 * (last_high + last_low) * 1e-6)
    if not (last_low - tolerance <= mid <= last_high + tolerance):
        info["close_clamp_skipped"] = "mid_outside_bar_range"
        return df, info
    out = df.copy()
    out.loc[last_idx, "close"] = mid
    info.update(
        {
            "close_clamp_applied": True,
            "close_clamp_from": last_close,
            "close_clamp_price": mid,
            "close_clamp_bar_ts": last_ts,
        }
    )
    return out, info


def _decision_bar_freshness_snapshot(
    df: "pd.DataFrame | None",
    *,
    timeframe: str,
    now_ts: float,
) -> dict[str, Any]:
    snapshot = _sync_classify_decision_bar_freshness(
        latest_ts=_df_latest_epoch(df),
        timeframe=timeframe,
        now=now_ts,
    )
    snapshot.setdefault("source", "live_decision_bar")
    return snapshot


def _record_decision_bar_freshness(snapshot: dict[str, Any]) -> None:
    try:
        _live_state_update(decision_bar_freshness=dict(snapshot or {}))
    except Exception:
        logger.debug("[live] decision bar freshness snapshot update failed", exc_info=True)


def _ensure_live_decision_bars_fresh(
    *,
    bridge: Any,
    symbol: str,
    timeframe: str,
    df_new: "pd.DataFrame",
    tick: int,
    log,
    market_session: dict[str, Any] | None = None,
) -> "pd.DataFrame":
    # The serial live owner consumes only the bridge's in-memory live
    # trendbar frame.  Historical RPCs and durable DuckDB writes stay outside
    # this boundary so a slow broker history call cannot starve Safety.
    now_ts = time.time()
    closed_df = _closed_decision_bar_frame(df_new, timeframe=timeframe, now_ts=now_ts)
    clamp_info: dict[str, Any] = {}
    try:
        # Snapshot the spot quote before the bridge reference is dropped; the
        # clamp itself stays in-memory (no broker/history RPC in this path).
        quote_snapshot: Any = None
        if bridge is not None and hasattr(bridge, "get_spot_quote"):
            try:
                quote_snapshot = bridge.get_spot_quote()
            except Exception:
                quote_snapshot = None
        del bridge
        closed_df, clamp_info = _clamp_last_closed_bar_close_to_spot(
            closed_df,
            timeframe=timeframe,
            now_ts=now_ts,
            quote_provider=(lambda q=quote_snapshot: q) if quote_snapshot else None,
        )
    except Exception:
        logger.debug("[live] decision bar close clamp failed", exc_info=True)
        clamp_info = {}
    snapshot = _decision_bar_freshness_snapshot(closed_df, timeframe=timeframe, now_ts=now_ts)
    if bool(snapshot.get("fresh", False)):
        snapshot.update(
            {
                "repair_attempted": False,
                "repair_status": "fresh",
                "source": "ctrader_live_trendbar",
            }
        )
        snapshot.update(clamp_info)
        _record_decision_bar_freshness(snapshot)
        return closed_df if closed_df is not None and len(closed_df) > 0 else df_new

    repair_suppressed = ""
    try:
        from backend.services.market_session import maintenance_wait_evidence
        from config.runtime_config import shared as _runtime_cfg

        # Production passes the already computed session snapshot.  Do not
        # perform another broker/history read when the online bar is stale.
        session = dict(market_session or {})
        session_status = str(session.get("status") or "")
        if session_status in {
            "closed_confirmed",
            "closed_pending_confirmation",
            "closed_pending_positions",
        }:
            repair_suppressed = "market_closed"
        else:
            maintenance = maintenance_wait_evidence(
                session,
                latest_market_data_ts=float(snapshot.get("latest_bar_ts", 0.0) or 0.0),
                now_ts=now_ts,
                grace_seconds=float(_runtime_cfg().market_open_pending_quote_grace_seconds),
            )
            if maintenance["active"]:
                repair_suppressed = "maintenance_wait"
    except Exception:
        logger.debug("[live] decision bar repair market-session check failed", exc_info=True)

    snapshot.update(
        {
            "repair_attempted": False,
            "repair_status": repair_suppressed or "stale_waiting_for_live_trendbar",
            "repair_inserted_bars": 0,
            "repair_latest_bar_ts": 0.0,
            "repair_error": "",
            "source": (
                "live_trendbar_repair_suppressed"
                if repair_suppressed
                else "ctrader_live_trendbar_read_only"
            ),
        }
    )
    _record_decision_bar_freshness(snapshot)
    try:
        log(
            f"tick {tick}: online trendbars stale; alpha waits for cTrader live feed "
            f"{symbol} {timeframe} latest={snapshot.get('latest_bar_ts', 0):.0f} "
            f"expected={snapshot.get('expected_closed_bar_ts', 0):.0f} "
            f"status={snapshot.get('repair_status')}"
        )
    except Exception:
        pass
    for frame in (closed_df, df_new):
        if frame is not None:
            try:
                return frame.iloc[0:0]
            except Exception:
                break
    return None


# ★ v9-fix: 备份 bar 缓存 (防 DB 空/broker 无数据时死机)
def _save_bar_cache(df: "pd.DataFrame") -> None:
    """将 warmup 成功的 bar 缓存到 pickle 文件, 供下次启动 fallback."""
    try:
        _BAR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        df.to_pickle(str(_BAR_CACHE_PATH))
        logger.info(f"[bar_cache] saved {len(df)} bars to {_BAR_CACHE_PATH.name}")
    except Exception as e:
        logger.warning(f"[bar_cache] save failed: {e}")


def _load_bar_cache() -> "pd.DataFrame | None":
    """从 pickle 读取备份 bar 缓存."""
    try:
        if not _BAR_CACHE_PATH.exists():
            return None
        df = pd.read_pickle(str(_BAR_CACHE_PATH))
        if df is not None and len(df) >= 30:
            age_hours = (time.time() - _BAR_CACHE_PATH.stat().st_mtime) / 3600
            logger.info(f"[bar_cache] loaded {len(df)} bars (age={age_hours:.1f}h) "
                        f"last close={df['close'].iloc[-1]:.2f}")
            return df
    except Exception as e:
        logger.warning(f"[bar_cache] load failed: {e}")
    return None


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
        logger.warning("[live] position snapshot enrichment unavailable: %s", exc)
    safe_positions = _safe_container_snapshot(positions)
    if not isinstance(safe_positions, list):
        safe_positions = []
    positions = [
        item if isinstance(item, dict) else {}
        for item in safe_positions
    ]
    _live_state_update(
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
    if not bool(_live_state_get("loop_running", False)) or not persist:
        return positions
    # A successful Reconcile response is authoritative only when it agrees
    # with broker-confirmed opens that have not yet received a complete close
    # deal.  Do not turn that conflict into a safe empty account: keep normal
    # multi-position operation when every durable open ID is present, but
    # durably stop *new* risk while any such ID is absent.
    conflict_reason = ""
    try:
        recovery_ids = _lifecycle_recovery_active_position_ids(
            _list_active_recovery_positions(broker)
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
            purged_ids = _recovery_position_store().purge_unbrokered(
                set(missing_recovery_ids),
                broker=broker,
                broker_position_ids=broker_ids,
            )
            if purged_ids:
                logger.warning(
                    f"[live] purged orphaned recovery rows absent from fresh broker "
                    f"snapshot: {sorted(purged_ids)}"
                )
                _release_orphaned_recovery_session_latches(
                    purged_ids,
                    broker=broker,
                    broker_position_ids=broker_ids,
                    reconcile_id=reconcile_id,
                    observed_at=observed_at,
                )
                recovery_ids = _lifecycle_recovery_active_position_ids(
                    _list_active_recovery_positions(broker)
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
                    if _retire_broker_missing_position(
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
                        "for pos %s: %s",
                        position_id,
                        exc,
                    )
            if retired_ids:
                recovery_ids = _lifecycle_recovery_active_position_ids(
                    _list_active_recovery_positions(broker)
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
            "[live] cannot validate fresh broker positions against recovery state: %s",
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
                    "[live] failed to persist position reconcile conflict latch: %s",
                    exc,
                )
        _live_state_update(
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
                "[live] failed to release resolved position reconcile conflict latch: %s",
                exc,
            )
        _live_state_update(no_new_risk_latch=no_new_risk_latch_status(fail_closed=True))
    return positions


def _safety_reference_price(bridge: Any, positions: list[dict[str, Any]]) -> float:
    try:
        quote = bridge.get_spot_quote() if bridge is not None and hasattr(bridge, "get_spot_quote") else {}
        if quote:
            _live_state_update(spot_quote=quote)
        if _quote_is_fresh(quote):
            price = float(quote.get("mid") or 0.0)
            if price > 0:
                return price
    except Exception:
        pass
    for position in positions:
        for field in ("current_price", "price_current", "entry_price", "price_open", "open_price"):
            try:
                price = float(position.get(field) or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            if price > 0:
                return price
    return float(get_latest_price() or 0.0)


def _live_safety_planner_runtime(bridge: Any) -> SafetyPlannerRuntime:
    """Build read-only adapters shared by two independent planning algorithms."""

    broker_schedule = _broker_schedule_from_bridge(bridge)

    def build_timeout_context(position, effective_cfg, now_ts):
        pid = int(position.get("position_id") or position.get("ticket") or 0)
        timeframe = str(getattr(effective_cfg, "timeframe", "M5") or "M5")
        temporal = _temporal_context_for_trade(
            decision_ts=float(now_ts),
            timeframe=timeframe,
        )
        return _lifecycle_build_close_position_risk_context_payload(
            position_id=pid,
            close_reason="holding_timeout",
            mode="live",
            broker="ctrader",
            symbol=str(position.get("symbol") or "XAUUSD+"),
            entry_ts=float(_position_open_timestamp(position) or 0.0),
            entry_ts_source="broker_position",
            temporal_context=temporal,
            max_holding_bars=int(
                getattr(effective_cfg, "risk_max_holding_bars", 0) or 0
            ),
            broker_schedule=broker_schedule,
        )

    def load_entry_plan(position_id: int) -> dict[str, Any]:
        try:
            row = _load_recovery_position_row(int(position_id))
        except Exception:
            return {}
        meta = dict((row or {}).get("recovery_meta") or {})
        return dict(meta.get("entry_protection_plan") or {})

    def evaluate_supervisor_read_only(position, all_positions, effective_cfg, acct, now_ts):
        existing = position.get("supervisor")
        if isinstance(existing, dict) and existing.get("action"):
            return _lifecycle_compact_supervisor_mapping(
                existing,
                nested_keys=frozenset({"evidence", "recommended_controls", "execution"}),
            )
        timeout_context = build_timeout_context(position, effective_cfg, now_ts)
        planner_position = dict(position)
        planner_position["max_holding_seconds"] = float(
            timeout_context.get("max_holding_seconds", 0.0) or 0.0
        )
        planner_position["holding_timeout_ratio"] = float(
            timeout_context.get("holding_timeout_ratio", 0.0) or 0.0
        )
        metric_names = {
            "mfe",
            "mae",
            "giveback_ratio",
            "profit_capture_ratio",
            "time_in_profit",
            "time_in_profit_seconds",
            "holding_efficiency",
            "time_decay_score",
            "thesis_status",
            "regime_shift",
            "entry_regime",
            "current_regime",
        }
        metrics = {
            name: planner_position[name]
            for name in metric_names
            if name in planner_position
        }
        context_inputs = _lifecycle_build_position_supervisor_context_inputs(
            position=planner_position,
            cfg=effective_cfg,
            positions=list(all_positions),
            account=dict(acct or {}),
            entry_decision_id="",
            risk_snapshot=_live_state_get("risk", {}, clone=True) or {},
            total_api_volume=_tracked_total_api_volume(list(all_positions)),
            market_context=_live_state_get("last_composite", {}, clone=True) or {},
            supervisor_state=dict(
                (
                    _load_recovery_row_for_risk_reduction(
                        int(position.get("position_id") or position.get("ticket") or 0),
                        operation="position_supervisor_safety_planner_context",
                    )
                    or {}
                ).get("recovery_meta")
                or {}
            ),
            loop_running=bool(_live_state_get("loop_running", True)),
        )
        context = _lifecycle_build_position_supervisor_context_payload(
            **context_inputs,
            temporal_context=timeout_context,
            position_metrics=metrics,
        )
        return evaluate_position_supervisor(context)

    def normalize_supervisor_action(position, verdict):
        payload = dict(verdict or {})
        if str(payload.get("action") or "").strip().lower() != "reduce":
            return payload
        controls = dict(payload.get("recommended_controls") or {})
        execution_plan = _plan_supervisor_reduce_action(
            bridge=bridge,
            position=dict(position or {}),
            verdict=payload,
            controls=controls,
            floor_api_volume_to_step=_floor_api_volume_to_step,
            should_full_close_untradeable_reduce=(
                _should_full_close_untradeable_reduce
            ),
        )
        return _normalize_supervisor_reduce_verdict(payload, execution_plan)

    return SafetyPlannerRuntime(
        build_timeout_context=build_timeout_context,
        load_entry_protection_plan=load_entry_plan,
        evaluate_supervisor=evaluate_supervisor_read_only,
        normalize_supervisor_action=normalize_supervisor_action,
    )


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
        runtime=_live_safety_planner_runtime(bridge),
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
        get_live_state=_live_state_get,
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
            get_safety_plane=_get_live_safety_plane,
            explicit_position_reconcile=_explicit_position_reconcile,
            publish_fresh_positions=partial(
                _publish_fresh_position_reconcile,
                bridge=bridge,
            ),
            get_live_state=_live_state_get,
            update_live_state=_live_state_update,
            runtime_config=_runtime_config,
            safety_reference_price=_safety_reference_price,
            factor_pipeline=_factor_pipeline or {},
            plan_safety_candidates=_plan_live_safety_candidates,
            execute_safety_candidate=_execute_live_safety_candidate,
            run_position_protection_cycle=_run_position_protection_cycle,
            persist_safety_fail_closed=_persist_safety_fail_closed,
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
            get_cached_recovery=lambda: _live_state_get(
                "execution_recovery", {}, clone=True
            )
            or {},
            update_live_state=_live_state_update,
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
            update_live_state=_live_state_update,
            get_live_state=_live_state_get,
            explicit_position_reconcile=_explicit_position_reconcile,
            publish_fresh_positions=_publish_fresh_position_reconcile,
            run_safety_cycle=_run_live_safety_cycle,
            restore_session_state=_restore_session_state_for_day,
            bootstrap_position_recovery=_bootstrap_position_recovery,
            factor_pipeline=_factor_pipeline or {},
            strategy_name=_current_loop_strategy_name(),
        ),
    )


def _live_loop_tick_runtime() -> LiveLoopTickRuntime:
    return LiveLoopTickRuntime(
        get_ctrader=_get_ctrader,
        reconcile_positions=_explicit_position_reconcile,
        run_safety_cycle=_run_live_safety_cycle,
        persist_safety_fail_closed=_persist_safety_fail_closed,
        reconcile_account=_explicit_account_reconcile,
        reconcile_value=_reconcile_value,
        mark_account_reconcile_failed=_mark_account_reconcile_failed,
        live_state_update=_live_state_update,
        loop_controller=_LIVE_LOOP_CONTROLLER,
        set_loop_diagnostic=_set_loop_diagnostic,
        recover_execution_outcomes=(
            _recover_execution_outcomes_before_alpha
        ),
        attempt_startup_barrier=_attempt_generation_startup_barrier,
        live_state_get=_live_state_get,
        bootstrap_position_recovery=_bootstrap_position_recovery,
        loop_strategy_name=_current_loop_strategy_name(),
        restore_session_state=_restore_session_state_for_day,
        session_circuit_breaker_enforced=lambda: not bounded_demo_mode_active(),
        evaluate_daily_drawdown=_evaluate_daily_drawdown,
        market_session_snapshot=_market_session_snapshot,
        ensure_spot_subscription=_ensure_spot_subscription,
        get_live_bars=_get_live_bars,
        ensure_decision_bars_fresh=_ensure_live_decision_bars_fresh,
        get_safety_plane=_get_live_safety_plane,
        retry_pending_open=_retry_pending_open_trade,
        process_tick=_process_tick,
        update_risk_metrics=_update_live_loop_risk_metrics,
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


def _risk_metric_inputs(
    positions: list[dict[str, Any]] | None,
) -> tuple[
    list[float],
    list[dict[str, Any]] | None,
]:
    from backend.services.review_contract import review_has_system_contamination
    from config.runtime_config import shared as runtime_config

    cfg = runtime_config()
    if str(getattr(cfg, "var_method", "historical")) != "historical":
        raise ValueError("risk_metrics_snapshot.v2 requires historical VaR")
    conn = _get_state_pg_conn()
    try:
        rows = iter_review_rows(conn, limit=0)
        rows.sort(key=lambda row: float(row.get("created_at") or 0.0), reverse=True)
        rows = rows[:200]
        review_rows = []
        for row in rows:
            review = row.get("review_json")
            if isinstance(review, str):
                try:
                    review = json.loads(review)
                except (TypeError, ValueError):
                    review = {}
            review_rows.append((row, review if isinstance(review, dict) else {}))
        conn.commit()
    finally:
        conn.close()

    clean_pnls: list[float] = []
    seen_positions: set[str] = set()
    for row, review in review_rows:
        position_id = str(row["position_id"] or "")
        if not isinstance(review, dict):
            continue
        if position_id in seen_positions or review_has_system_contamination(review):
            continue
        seen_positions.add(position_id)
        clean_pnls.append(float(row["pnl"] or 0.0))

    normalized_positions = [] if positions is not None else None
    for index, position in enumerate(positions or []):
        symbol = str(position.get("symbol") or "XAUUSD+")
        instrument = dict((cfg.multi_symbol_config or {}).get(symbol) or {})
        if not instrument:
            symbol_key = symbol.upper().rstrip("+")
            instrument = next(
                (
                    dict(value or {})
                    for name, value in (cfg.multi_symbol_config or {}).items()
                    if str(name).upper().rstrip("+") == symbol_key
                ),
                {},
            )
        price = float(
            position.get("current_price")
            or position.get("price_current")
            or 0.0
        )
        api_volume = float(position.get("volume") or 0.0)
        normalized = {
            "position_id": position.get("position_id") or index,
            "symbol": symbol,
            "direction": position.get("direction", position.get("side")),
        }
        contract_size = float(instrument.get("contract_size") or 0.0)
        if price > 0 and api_volume >= 0 and contract_size > 0:
            normalized["notional_usd"] = (
                    price
                    * api_volume
                    / 10_000.0
                    * contract_size
            )
        normalized_positions.append(normalized)
    return clean_pnls, normalized_positions


def _closed_bar_forward_var_input(*, cfg, observed_at: float):
    from backend.risk.metrics_snapshot import freeze_closed_bar_returns

    symbol = str((getattr(cfg, "enabled_symbols", None) or ["XAUUSD+"])[0])
    timeframe = str(getattr(cfg, "timeframe", "M5") or "M5")
    lookback = max(2, int(getattr(cfg, "var_window", 500) or 500))
    try:
        frame = _get_live_bars(symbol, timeframe, lookback + 1)
        frame = _closed_decision_bar_frame(
            frame,
            timeframe=timeframe,
            now_ts=time.time(),
        )
        closes = (
            list(frame["close"].tolist())
            if frame is not None and len(frame) > 0
            else []
        )
        timestamps = (
            [
                index.isoformat()
                if hasattr(index, "isoformat")
                else str(index)
                for index in frame.index
            ]
            if frame is not None and len(frame) > 0
            else []
        )
        return freeze_closed_bar_returns(
            closes,
            timestamps=timestamps,
            symbol=symbol,
            timeframe=timeframe,
            as_of=observed_at,
            lookback=lookback,
        )
    except Exception as exc:
        return freeze_closed_bar_returns(
            [],
            symbol=symbol,
            timeframe=timeframe,
            as_of=observed_at,
            lookback=lookback,
            invalid_reason=(
                f"online_closed_bar_return_input_error:{type(exc).__name__}"
            ),
        )


def _update_live_loop_risk_metrics(*, tick: int, log) -> None:
    try:
        from backend.risk.metrics_snapshot import (
            SNAPSHOT_KEY,
            attach_internal_forward_var_input,
            build_risk_metrics_snapshot,
        )
        from config.runtime_config import shared as runtime_config

        account = _live_state_get("account_reconciled", {}, clone=True) or {}
        positions = _live_state_get("positions_reconciled", [], clone=True)
        account_id = str(_live_state_get("account_reconcile_id", "") or "")
        positions_id = str(
            _live_state_get("positions_reconcile_id", "") or ""
        )
        account_at = float(_live_state_get("account_updated_at", 0.0) or 0.0)
        positions_at = float(
            _live_state_get("positions_updated_at", 0.0) or 0.0
        )
        account_failed_at = float(
            _live_state_get("account_reconcile_failed_at", 0.0) or 0.0
        )
        positions_failed_at = float(
            _live_state_get("positions_reconcile_failed_at", 0.0) or 0.0
        )
        facts_fresh = (
            bool(account_id)
            and bool(positions_id)
            and _fresh_observation_timestamp(account_at)
            and _fresh_observation_timestamp(positions_at)
            and account_failed_at <= account_at
            and positions_failed_at <= positions_at
        )
        if not facts_fresh:
            previous = _runtime_kv_get(SNAPSHOT_KEY, {}) or {}
            snapshot = {
                **previous,
                "schema_version": SNAPSHOT_KEY,
                "status": "stale",
                "published_at": time.time(),
                "as_of": min(
                    value for value in (account_at, positions_at) if value > 0
                ) if account_at > 0 or positions_at > 0 else 0.0,
                "blockers": ["broker_risk_facts_stale"],
            }
            _live_state_update(
                risk={
                    **dict(previous.get("components") or {}),
                    "snapshot": snapshot,
                }
            )
            _runtime_kv_set(SNAPSHOT_KEY, snapshot)
            return
        observed_at = min(account_at, positions_at)
        clean_pnls, normalized_positions = _risk_metric_inputs(
            positions,
        )
        cfg = runtime_config()
        forward_var_input = _closed_bar_forward_var_input(
            cfg=cfg,
            observed_at=observed_at,
        )
        snapshot = build_risk_metrics_snapshot(
            forward_var_input=forward_var_input,
            clean_trade_pnls=clean_pnls,
            positions=normalized_positions,
            account=account,
            account_reconcile_id=account_id,
            positions_reconcile_id=positions_id,
            as_of=observed_at,
            kelly_min_closed_trades=int(
                getattr(cfg, "kelly_min_closed_trades", 20)
                or 20
            ),
            kelly_multiplier=float(
                getattr(cfg, "kelly_fraction", 0.5) or 0.5
            ),
            kelly_max_fraction=float(
                getattr(cfg, "kelly_max_pct", 0.25) or 0.25
            ),
            var_confidence=float(
                getattr(cfg, "var_alpha", 0.95) or 0.95
            ),
            var_lookback=max(
                2,
                int(getattr(cfg, "var_window", 500) or 500),
            ),
        ).to_dict()
        # ``as_of`` is the oldest broker input used by the calculation.  Keep
        # a separate publication clock so the API does not expire a valid
        # snapshot merely because serial reconciliation and risk math took
        # part of the 20-second input window.
        snapshot["published_at"] = time.time()
        _live_state_update(
            risk=attach_internal_forward_var_input(
                {**snapshot["components"], "snapshot": snapshot},
                forward_var_input,
            )
        )
        _runtime_kv_set(SNAPSHOT_KEY, snapshot)
    except Exception as risk_e:
        try:
            from backend.risk.metrics_snapshot import SNAPSHOT_KEY

            previous = _runtime_kv_get(SNAPSHOT_KEY, {}) or {}
            error_snapshot = {
                **previous,
                "schema_version": SNAPSHOT_KEY,
                "status": "error",
                "published_at": time.time(),
                "blockers": ["risk_metrics_calculation_error"],
            }
            _live_state_update(
                risk={
                    **dict(previous.get("components") or {}),
                    "snapshot": error_snapshot,
                }
            )
            _runtime_kv_set(SNAPSHOT_KEY, error_snapshot)
        except Exception:
            pass
        log(f"tick {tick}: risk calculation error (non-fatal): {risk_e}")


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
        logger.exception("[live] generation %s failed", generation_id or "unowned")
        raise
    finally:
        _live_state_update(accepting_new_risk=False)
        if generation_id:
            try:
                _LIVE_LOOP_CONTROLLER.acknowledge_exit(
                    generation_id,
                    failed_reason=failed_reason,
                )
            except RuntimeError as exc:
                logger.error("[live] loop exit ownership mismatch: %s", exc)
        # The scheduler is process-owned: readiness, health and data
        # maintenance must remain alive while a generation is stopped or being
        # recovered.  Generation-sensitive jobs guard on loop ownership.
        # Only BackendRuntimeLifecycle.stop() shuts the scheduler down.
        _stop_live_safety_watchdog()
        if not _process_shutdown_requested:
            try:
                if schedule_auto_resume_loop():
                    logger.warning(
                        "[live] loop exited while desired state remained enabled; "
                        "auto-resume scheduled"
                    )
            except Exception as exc:
                logger.error("[live] failed to schedule loop auto-resume: %s", exc)


def _startup_safety_runtime() -> StartupSafetyRuntime:
    return StartupSafetyRuntime(
        get_ctrader=_get_ctrader,
        reconcile_positions=_explicit_position_reconcile,
        run_safety_cycle=_run_live_safety_cycle,
        reconcile_account=_explicit_account_reconcile,
        reconcile_value=_reconcile_value,
        live_state_update=_live_state_update,
        persist_safety_fail_closed=_persist_safety_fail_closed,
    )


def _bar_warmup_runtime() -> BarWarmupRuntime:
    return BarWarmupRuntime(
        warmup_from_local_db=_warmup_from_local_db,
        get_ctrader=_get_ctrader,
        wait_ctrader_ready=_wait_ctrader_ready,
        fetch_bars_with_retry=_fetch_bars_with_retry,
        load_bar_cache=_load_bar_cache,
        publish_latest_price=_publish_latest_price,
        save_bar_cache=_save_bar_cache,
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


def _factor_generation_active(generation_id: str) -> bool:
    if not generation_id:
        return True
    current = _LIVE_LOOP_CONTROLLER.current()
    return bool(
        current is not None
        and current.generation_id == generation_id
        and not current.stop_event.is_set()
    )


def _factor_event_sizing_factory():
    from backend.core.db import DUCKDB_EVENTS
    from execution.event_sizing import EventSizing

    return EventSizing(db_path=str(DUCKDB_EVENTS), enabled=True)


def _factor_initialization_runtime() -> FactorInitializationRuntime:
    from alpha.adaptive_weight_engine import AdaptiveWeightEngine
    from alpha.attribution_engine import AttributionEngine
    from alpha.execution_gate import ExecutionGate
    from alpha.ic_tracker import ICTracker
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
        adaptive_weight_cls=AdaptiveWeightEngine,
        ic_tracker_cls=ICTracker,
        selection_factory=select_runtime_factors,
        projection_service_factory=RuntimeFactorSelectionProjectionService,
        event_sizing_factory=_factor_event_sizing_factory,
        subscribe_config=subscribe,
        generation_active=_factor_generation_active,
        merge_portfolio_configs=_merge_portfolio_configs,
        execution_gate_config=_loop_execution_gate_config,
        adaptive_weight_config=_loop_adaptive_weight_config,
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
        live_state_update=_live_state_update,
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

    # 订阅 cTrader 实时报价；warmup local_db 路径从 _get_ctrader() 拿真 bridge 并短等 ready.
    if broker == "ctrader":
        try:
            _loop_subscribe_spot_once(
                get_ctrader=_get_ctrader,
                wait_ctrader_ready=_wait_ctrader_ready,
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
        _live_state_update(
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
    _live_state_update(
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


# 模块级,供 _read_state_snapshot 读
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

    quote = _live_state_get("spot_quote", None, clone=True)
    if not _quote_is_fresh(quote):
        _live_state_update(
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
        _live_state_update(spot_price=value)
    return value


def _latest_bar_close_from_store() -> float | None:
    """Read the latest local XAU M5 bar close with a short TTL; no broker call."""
    global _latest_bar_price_cache
    now_ts = time.time()
    if _latest_bar_price_cache and now_ts - _latest_bar_price_cache[0] < 5.0:
        return _latest_bar_price_cache[1]
    try:
        from data.store import DataStore

        store = DataStore()
        df = store.load_bars("XAUUSD+", "M5", limit=1)
        if df is None or len(df) == 0:
            return None
        price = float(df.iloc[-1]["close"] or 0.0)
        if price > 0:
            _latest_bar_price_cache = (now_ts, price)
            return price
    except Exception as exc:
        logger.debug("[live] latest bar close fallback failed: %s", exc)
    return None


def get_latest_price() -> float | None:
    """返回最新价. 优先共享缓存 (live loop 写), 其次 bridge spot, 最后本地 bar close."""
    quote = _live_state_get("spot_quote", None, clone=True)
    if _quote_is_fresh(quote):
        spot = float((quote or {}).get("mid") or 0.0)
        if spot > 0:
            return spot
    cached_spot = _live_state_get("spot_price", None)
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
        bridge, err, warming = _get_ctrader()
        if bridge is None or err or warming or not bridge.is_connected:
            fallback = _latest_bar_close_from_store()
            return _publish_latest_price(fallback, source="bar_close") if fallback else _latest_price
        quote = bridge.get_spot_quote() if hasattr(bridge, "get_spot_quote") else {}
        spot = float((quote or {}).get("mid") or 0.0) if _quote_is_fresh(quote) else 0.0
        if spot > 0:
            _live_state_update(spot_quote=quote)
            return spot
    except Exception as _e2:
        logger.debug("[live] get_latest_price spot query failed: %s", _e2)
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
            update_live_state=_live_state_update,
            admission_lock=_OPEN_TRADE_ADMISSION_LOCK,
            get_ctrader=_get_ctrader,
            wait_ctrader_ready=_wait_ctrader_ready,
            reconcile_positions=_fresh_emergency_position_reconcile,
            position_volume=_position_api_volume,
            build_close_risk_context=_build_close_position_risk_context,
            risk_policy=_RISK_POLICY,
            remember_close_reason=_remember_close_reason,
            remember_close_verdict=_remember_close_verdict,
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
def _record_filled_open_attribution(
    *,
    attr_engine: Any,
    pid: int,
    current_price: float,
    actual_api_volume: float,
    composite: Any,
) -> dict[str, Any]:
    trade_attribution_payload: dict[str, Any] = {}
    try:
        from alpha.attribution_engine import TradeAttribution

        trade_attribution_payload = _trade_attribution_payload_from_composite(
            position_id=pid,
            open_ts=time.time(),
            open_price=current_price,
            direction=composite.direction,
            actual_api_volume=actual_api_volume,
            composite=composite,
        )
        trade_attr = TradeAttribution.from_jsonable(trade_attribution_payload)
        if attr_engine is not None and trade_attr is not None:
            attr_engine.record_open(pid, trade_attr)
            trade_attribution_payload = trade_attr.to_jsonable()
        _pos_open_prices[pid] = current_price
        _pos_open_api_volume[pid] = float(actual_api_volume)
    except Exception as attr_err:
        logger.debug("[live] attribution open persist failed for pos %s: %s", pid, attr_err)
    return trade_attribution_payload


def _log_filled_open_ledger(
    *,
    cfg: Any,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    acct: dict,
    pos: list,
    composite: Any,
    gate_result: Any,
    learning_context: dict[str, Any],
    risk_verdict: Any = None,
    sizing_trace: dict[str, Any] | None = None,
    parent_decision_id: str = "",
    execution_intent_id: str = "",
) -> str:
    if not _LEDGER:
        return ""
    try:
        ledger_payloads = _lifecycle_build_filled_open_ledger_payloads(
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            acct=acct,
            positions_before=pos,
            composite=composite,
            gate_result=gate_result,
            learning_context={"sizing_trace": sizing_trace or {}, **learning_context},
            risk_state=(
                _risk_state_with_verdict(risk_verdict)
                if risk_verdict is not None
                else (_live_state_get("risk", {}, clone=True) or {})
            ),
            session_pnl=float(_live_state_get("session_pnl", 0) or 0.0),
            risk_verdict=risk_verdict,
            decision_ts_fallback=time.time(),
            event_ts=time.time(),
        )
        lineage = {
            "parent_decision_id": str(parent_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
        }
        decision_payload = ledger_payloads["composite_decision_payload"]
        decision_payload["action_json"] = {
            **dict(decision_payload.get("action_json") or {}),
            **lineage,
            "event_stage": "fill",
        }
        for payload_key in ("submitted_order_payload", "filled_order_payload"):
            order_payload = ledger_payloads[payload_key]
            order_payload["details"] = {
                **dict(order_payload.get("details") or {}),
                **lineage,
            }
        ledger_payloads["position_event_payload"]["details"] = {
            **dict(ledger_payloads["position_event_payload"].get("details") or {}),
            **lineage,
        }
        entry_decision_id = _LEDGER.log_composite_decision(
            **ledger_payloads["composite_decision_payload"]
        )
        lineage_decision_id = str(parent_decision_id or entry_decision_id or "")
        for payload_key in ("submitted_order_payload", "filled_order_payload"):
            order_payload = ledger_payloads[payload_key]
            order_payload["details"] = {
                **dict(order_payload.get("details") or {}),
                "decision_id": lineage_decision_id,
                "child_decision_id": str(entry_decision_id or ""),
            }
        ledger_payloads["position_event_payload"]["details"] = {
            **dict(ledger_payloads["position_event_payload"].get("details") or {}),
            "decision_id": lineage_decision_id,
            "child_decision_id": str(entry_decision_id or ""),
        }
        _pos_entry_decisions[int(pid)] = lineage_decision_id
        _LEDGER.log_order_event(
            decision_id=lineage_decision_id,
            **ledger_payloads["submitted_order_payload"],
        )
        _LEDGER.log_order_event(
            decision_id=lineage_decision_id,
            **ledger_payloads["filled_order_payload"],
        )
        _LEDGER.log_position_event(
            decision_id=lineage_decision_id,
            **ledger_payloads["position_event_payload"],
        )
        return lineage_decision_id
    except Exception as ledger_err:
        logger.debug("[live] ledger open persist failed for pos %s: %s", pid, ledger_err)
        return ""


def _upsert_filled_open_recovery(
    *,
    broker: str,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    composite: Any,
    entry_decision_id: str,
    trade_attribution_payload: dict[str, Any],
    learning_context: dict[str, Any],
    execution_intent_id: str = "",
) -> None:
    try:
        entry_protection_plan = _entry_protection_plan_payload(
            position_id=pid,
            direction=composite.direction,
            entry_price=float(fill_price or current_price),
            target_stop_loss=sl_price,
            target_take_profit=tp_price,
            requested_volume=requested_volume,
            actual_api_volume=actual_api_volume,
            tick=tick,
            status="pending",
        )
        recovery_payloads = _lifecycle_build_filled_open_recovery_payloads(
            position_id=pid,
            broker=broker,
            strategy_name=_current_loop_strategy_name(),
            direction=composite.direction,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            requested_volume=requested_volume,
            actual_api_volume=actual_api_volume,
            tick=tick,
            entry_decision_id=entry_decision_id or _lookup_entry_decision_id(int(pid)),
            entry_protection_plan=entry_protection_plan,
            trade_attribution_payload=trade_attribution_payload,
            learning_context=learning_context,
            context_integrity=_RECOVERY_CONTEXT_FULL,
        )
        recovery_payloads["meta"] = {
            **dict(recovery_payloads.get("meta") or {}),
            "parent_decision_id": str(entry_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
        }
        _upsert_recovery_position_state(
            recovery_payloads["state_payload"],
            **recovery_payloads["state_kwargs"],
            meta=recovery_payloads["meta"],
        )
    except Exception as recovery_err:
        logger.debug("[live] recovery open persist failed for pos %s: %s", pid, recovery_err)


def _filled_open_processing_runtime() -> FilledOpenRuntime:
    return FilledOpenRuntime(
        ledger_available=bool(_LEDGER),
        record_attribution=_record_filled_open_attribution,
        build_learning_context=_open_learning_context_payload,
        log_ledger=_log_filled_open_ledger,
        upsert_recovery=_upsert_filled_open_recovery,
        debug=logger.debug,
    )


def _record_filled_position_open_context(
    *,
    attr_engine,
    broker: str,
    cfg,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    acct: dict,
    pos: list,
    composite,
    gate_result,
    risk_verdict=None,
    market_session: dict[str, Any] | None = None,
    base_requested_volume: float | None = None,
    event_sizing_context: dict[str, Any] | None = None,
    sizing_trace: dict[str, Any] | None = None,
    sl_dist: float = 0.0,
    tp_dist: float = 0.0,
    bridge: Any = None,
    parent_decision_id: str = "",
    execution_intent_id: str = "",
) -> str:
    return _runtime_record_filled_open(
        FilledOpenRequest(
            attr_engine=attr_engine,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            acct=acct,
            pos=pos,
            composite=composite,
            gate_result=gate_result,
            risk_verdict=risk_verdict,
            market_session=market_session,
            base_requested_volume=base_requested_volume,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            bridge=bridge,
            parent_decision_id=parent_decision_id,
            execution_intent_id=execution_intent_id,
        ),
        runtime=_filled_open_processing_runtime(),
    )


def _closed_position_processing_runtime() -> ClosedPositionProcessingRuntime:
    return ClosedPositionProcessingRuntime(
        consume_close_reason=_consume_close_reason,
        consume_close_verdict=_consume_close_verdict,
        classify_close_source=_classify_close_source,
        select_close_total_pnl=_tick_select_close_total_pnl,
        open_api_volumes=_pos_open_api_volume,
        ledger=_LEDGER,
        ensure_open_ledger=_ensure_open_ledger_for_recovered_close,
        lookup_context_integrity=_lookup_recovery_context_integrity,
        build_close_ledger_payloads=_tick_build_close_ledger_payloads,
        get_session_pnl=lambda: _live_state_get("session_pnl", 0),
        risk_state_with_verdict=_risk_state_with_verdict_dict,
        trade_reviewer=_TRADE_REVIEWER,
        experience_builder=_EXPERIENCE_BUILDER,
        policy_suggester=_POLICY_SUGGESTER,
        build_trade_review_payload=_tick_build_trade_review_payload,
        mark_recovery_closed=_mark_recovery_position_closed,
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
            defer_close=_defer_close_until_authoritative_deal,
            update_live_state=_live_state_update,
            collect_attribution=_collect_closed_position_attribution,
            lookup_context_integrity=_lookup_recovery_context_integrity,
            log_closed_position_ledger=_log_closed_position_ledger_after_tick,
            run_closed_position_learning=_run_closed_position_learning_after_tick,
            cleanup_closed_position=_cleanup_closed_position_after_tick,
            record_aux_failure=_record_risk_reduction_aux_failure,
            mark_recovery_closed=_mark_recovery_position_closed,
            reconcile_account=_explicit_account_reconcile,
            reconcile_value=_reconcile_value,
            restore_session_state=_restore_session_state_for_day,
            release_close_latch=_release_session_close_deal_latch,
            trade_date=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            now=time.time,
            full_context=_RECOVERY_CONTEXT_FULL,
        ),
    )


def _mark_amended_open_success_local_state(
    *,
    pid: int,
    sl_price: float,
    tp_price: float,
    tick: int,
    actual_api_volume: float,
    composite: Any,
    direction_name: str,
    log,
) -> None:
    _track_local_sl_tp(pid, sl=sl_price, tp=tp_price)
    try:
        _update_entry_protection_plan_status(
            int(pid),
            status="applied",
            attempted=True,
            applied_sl=sl_price,
            applied_tp=tp_price,
        )
    except Exception as _plan_update_err:
        logger.debug(
            "[live] entry protection applied update failed for pos %s: %s",
            pid,
            _plan_update_err,
        )
    _pos_entry_scores[pid] = composite.score
    log(f"tick {tick}: v4 {direction_name} ORDER+AMEND OK "
        f"api_volume={actual_api_volume:.0f} pos={pid} score={composite.score:.4f}")


def _record_amended_open_execution_quality(
    *,
    bar: dict,
    current_price: float,
    fill_price: float,
    composite: Any,
    actual_api_volume: float,
    pid: int,
    submit_started_at: float | None = None,
    fill_received_at: float | None = None,
) -> None:
    try:
        submitted_at = float(submit_started_at or time.time())
        filled_at = float(fill_received_at or time.time())
        _exec_quality.record(_ExecTrade(
            signal_time=submitted_at,
            submit_time=submitted_at,
            fill_time=filled_at,
            signal_price=current_price,
            fill_price=fill_price,
            symbol="XAUUSD+",
            direction=composite.direction,
            volume=actual_api_volume,
            order_id=pid,
        ))
    except Exception:
        pass


def _record_amended_open_attribution(
    *,
    attr_engine: Any,
    pid: int,
    current_price: float,
    actual_api_volume: float,
    composite: Any,
    tick: int,
    log,
) -> Any:
    from alpha.attribution_engine import TradeAttribution

    trade_attribution_payload = _trade_attribution_payload_from_composite(
        position_id=int(pid),
        open_ts=time.time(),
        open_price=float(current_price),
        direction=int(composite.direction),
        actual_api_volume=float(actual_api_volume),
        composite=composite,
    )
    trade_attr = TradeAttribution.from_jsonable(trade_attribution_payload)
    attr_engine.record_open(pid, trade_attr)
    _pos_open_prices[pid] = current_price
    _pos_open_api_volume[pid] = float(actual_api_volume)
    log(f"tick {tick}: attribution recorded open pos={pid}")
    return trade_attr


def _log_amended_open_ledger(
    *,
    cfg: Any,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    acct: dict,
    pos: list,
    composite: Any,
    gate_result: Any,
    risk_verdict: Any,
    event_sizing_context: dict[str, Any],
    sizing_trace: dict[str, Any],
    learning_context: dict[str, Any],
    parent_decision_id: str = "",
    execution_intent_id: str = "",
) -> str:
    if not _LEDGER:
        return ""
    try:
        open_ledger_payloads = _tick_build_open_ledger_payloads(
            composite=composite,
            gate_result=gate_result,
            cfg=cfg,
            bar=bar,
            account=acct,
            positions_before=pos,
            session_pnl=_live_state_get("session_pnl", 0),
            risk_state=_risk_state_with_verdict(risk_verdict),
            risk_verdict=risk_verdict,
            pid=int(pid),
            requested_volume=float(requested_volume),
            base_requested_volume=float(base_requested_volume),
            actual_api_volume=float(actual_api_volume),
            current_price=float(current_price),
            fill_price=float(fill_price),
            sl_price=float(sl_price),
            tp_price=float(tp_price),
            tick=tick,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            learning_context=learning_context,
            decision_ts_fallback=time.time(),
            event_ts=time.time(),
        )
        decision_payload = open_ledger_payloads["decision"]
        decision_payload["action_json"] = {
            **dict(decision_payload.get("action_json") or {}),
            "parent_decision_id": str(parent_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
            "event_stage": "protection_confirmed",
        }
        for payload_key in ("submitted_order", "filled_order"):
            order_payload = open_ledger_payloads[payload_key]
            order_payload["details"] = {
                **dict(order_payload.get("details") or {}),
                "parent_decision_id": str(parent_decision_id or ""),
                "execution_intent_id": str(execution_intent_id or ""),
            }
        open_ledger_payloads["position_event"]["details"] = {
            **dict(open_ledger_payloads["position_event"].get("details") or {}),
            "parent_decision_id": str(parent_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
        }
        entry_decision_id = _LEDGER.log_composite_decision(
            **open_ledger_payloads["decision"]
        )
        lineage_decision_id = str(parent_decision_id or entry_decision_id or "")
        for payload_key in ("submitted_order", "filled_order"):
            order_payload = open_ledger_payloads[payload_key]
            order_payload["details"] = {
                **dict(order_payload.get("details") or {}),
                "decision_id": lineage_decision_id,
                "child_decision_id": str(entry_decision_id or ""),
            }
        open_ledger_payloads["position_event"]["details"] = {
            **dict(open_ledger_payloads["position_event"].get("details") or {}),
            "decision_id": lineage_decision_id,
            "child_decision_id": str(entry_decision_id or ""),
        }
        _pos_entry_decisions[int(pid)] = lineage_decision_id
        _LEDGER.log_order_event(
            decision_id=lineage_decision_id,
            **open_ledger_payloads["submitted_order"],
        )
        _LEDGER.log_order_event(
            decision_id=lineage_decision_id,
            **open_ledger_payloads["filled_order"],
        )
        _LEDGER.log_position_event(
            decision_id=lineage_decision_id,
            **open_ledger_payloads["position_event"],
        )
        return lineage_decision_id
    except Exception as _ledger_err:
        logger.debug("[live] ledger open failed for pos %s: %s", pid, _ledger_err)
        return ""


def _upsert_amended_open_recovery(
    *,
    broker: str,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    composite: Any,
    entry_decision_id: str,
    entry_protection_plan: dict[str, Any],
    trade_attr: Any,
    event_sizing_context: dict[str, Any],
    sizing_trace: dict[str, Any],
    learning_context: dict[str, Any],
    execution_intent_id: str = "",
) -> None:
    try:
        recovery_payloads = _lifecycle_build_filled_open_recovery_payloads(
            position_id=int(pid),
            broker=broker,
            strategy_name=_current_loop_strategy_name(),
            direction=int(composite.direction),
            fill_price=float(fill_price),
            current_price=float(current_price),
            sl_price=float(sl_price),
            tp_price=float(tp_price),
            requested_volume=float(requested_volume),
            actual_api_volume=float(actual_api_volume),
            tick=tick,
            entry_decision_id=entry_decision_id or _lookup_entry_decision_id(int(pid)),
            entry_protection_plan=_lifecycle_build_applied_entry_protection_plan_payload(
                plan=entry_protection_plan,
                updated_at=time.time(),
                applied_sl=sl_price,
                applied_tp=tp_price,
            ),
            trade_attribution_payload=trade_attr.to_jsonable(),
            learning_context={
                "event_sizing": event_sizing_context,
                "sizing_trace": sizing_trace,
                **learning_context,
            },
            context_integrity=_RECOVERY_CONTEXT_FULL,
        )
        recovery_payloads["meta"] = {
            **dict(recovery_payloads.get("meta") or {}),
            "parent_decision_id": str(entry_decision_id or ""),
            "execution_intent_id": str(execution_intent_id or ""),
        }
        _upsert_recovery_position_state(
            recovery_payloads["state_payload"],
            **recovery_payloads["state_kwargs"],
            meta=recovery_payloads["meta"],
        )
    except Exception as _recovery_open_err:
        logger.debug("[live] recovery open persist failed for pos %s: %s", pid, _recovery_open_err)


def _amended_open_success_processing_runtime() -> AmendedOpenSuccessRuntime:
    return AmendedOpenSuccessRuntime(
        mark_local_state=_mark_amended_open_success_local_state,
        record_execution_quality=_record_amended_open_execution_quality,
        record_attribution=_record_amended_open_attribution,
        build_learning_context=_open_learning_context_payload,
        log_ledger=_log_amended_open_ledger,
        upsert_recovery=_upsert_amended_open_recovery,
    )


def _record_amended_open_success_context(
    *,
    attr_engine: Any,
    bridge: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    sl_dist: float,
    tp_dist: float,
    acct: dict,
    pos: list,
    composite: Any,
    gate_result: Any,
    risk_verdict: Any,
    market_session: dict[str, Any],
    event_sizing_context: dict[str, Any],
    sizing_trace: dict[str, Any],
    entry_protection_plan: dict[str, Any],
    direction_name: str,
    log,
    submit_started_at: float | None = None,
    fill_received_at: float | None = None,
    parent_decision_id: str = "",
    execution_intent_id: str = "",
) -> None:
    _runtime_record_amended_success(
        AmendedOpenSuccessRequest(
            attr_engine=attr_engine,
            bridge=bridge,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            base_requested_volume=base_requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            acct=acct,
            pos=pos,
            composite=composite,
            gate_result=gate_result,
            risk_verdict=risk_verdict,
            market_session=market_session,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            entry_protection_plan=entry_protection_plan,
            direction_name=direction_name,
            log=log,
            submit_started_at=submit_started_at,
            fill_received_at=fill_received_at,
            parent_decision_id=parent_decision_id,
            execution_intent_id=execution_intent_id,
        ),
        runtime=_amended_open_success_processing_runtime(),
    )


def _amend_failure_processing_runtime() -> AmendFailureRuntime:
    ledger = _LEDGER
    return AmendFailureRuntime(
        persist_fail_closed=_persist_safety_fail_closed,
        record_aux_failure=_record_risk_reduction_aux_failure,
        record_filled_context=lambda request: (
            _record_filled_position_open_context(**vars(request))
        ),
        update_plan_status=_update_entry_protection_plan_status,
        ledger_available=bool(ledger),
        build_failed_payloads=_tick_build_amend_failed_ledger_payloads,
        get_risk_state=lambda: (
            _live_state_get("risk", {}, clone=True) or {}
        ),
        log_composite_decision=(
            ledger.log_composite_decision if ledger else lambda **_kwargs: ""
        ),
        log_order_event=(
            ledger.log_order_event if ledger else lambda **_kwargs: None
        ),
        debug=logger.debug,
        now=time.time,
    )


def _record_amend_failure_after_fill(
    *,
    attr_engine: Any,
    bridge: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    sl_dist: float,
    tp_dist: float,
    acct: dict,
    pos: list,
    composite: Any,
    gate_result: Any,
    risk_verdict: Any,
    market_session: dict[str, Any] | None,
    event_sizing_context: dict[str, Any],
    sizing_trace: dict[str, Any],
    status_error: str,
    ledger_action_reason: str,
    ledger_comment: str = "",
    ledger_error: str = "",
    ledger_debug_message: str = "[live] ledger amend failed event failed for pos %s: %s",
    failure_log: str = "",
    log=None,
    parent_decision_id: str = "",
    execution_intent_id: str = "",
) -> None:
    _runtime_record_amend_failure(
        AmendFailureRequest(
            attr_engine=attr_engine,
            bridge=bridge,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            base_requested_volume=base_requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            acct=acct,
            pos=pos,
            composite=composite,
            gate_result=gate_result,
            risk_verdict=risk_verdict,
            market_session=market_session,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            status_error=status_error,
            ledger_action_reason=ledger_action_reason,
            ledger_comment=ledger_comment,
            ledger_error=ledger_error,
            ledger_debug_message=ledger_debug_message,
            failure_log=failure_log,
            log=log,
            parent_decision_id=parent_decision_id,
            execution_intent_id=execution_intent_id,
        ),
        runtime=_amend_failure_processing_runtime(),
    )


def _resolve_open_trade_bridge_meta(bridge: Any) -> dict[str, Any]:
    meta = getattr(bridge, "_symbol_meta", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    if not meta.get("api_min_volume") and bridge is not None and hasattr(bridge, "_resolve_symbol_id"):
        try:
            bridge._resolve_symbol_id()
            meta = getattr(bridge, "_symbol_meta", None) or {}
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            pass
    return dict(meta or {})


def _apply_context_position_sizing(
    *,
    volume: float,
    sizing_trace: dict[str, Any],
    composite: Any,
    bridge_meta: dict[str, Any],
) -> tuple[float, dict[str, Any]]:
    context_policy = dict(getattr(composite, "context_policy", {}) or {})
    input_volume = float(volume or 0.0)
    next_trace = dict(sizing_trace)
    try:
        context_mult = float(context_policy.get("position_multiplier", 1.0) or 1.0)
    except (TypeError, ValueError):
        context_mult = 1.0
    if not context_policy or abs(context_mult - 1.0) <= 1e-9:
        return input_volume, sizing_trace

    upstream_blocked_reason = str(next_trace.get("blocked_reason") or "")
    context_raw_volume = input_volume * context_mult
    min_volume = float((bridge_meta or {}).get("api_min_volume") or 1.0)
    blocked_reason = ""
    if input_volume <= 0:
        adjusted_volume = 0.0
        blocked_reason = upstream_blocked_reason or "non_positive_upstream_sizing"
    elif (
        context_mult < 1.0
        and context_raw_volume < min_volume
        and bool(
            next_trace.get("demo_exploration")
            or next_trace.get("demo_nursery_exploration")
        )
    ):
        adjusted_volume = input_volume
        next_trace["context_policy_demo_nursery_min_preserved"] = True
    elif context_mult < 1.0 and context_raw_volume < min_volume:
        adjusted_volume = 0.0
        blocked_reason = (
            f"context_sizing_below_min: {input_volume:.0f}*{context_mult:.2f}="
            f"{context_raw_volume:.0f}<{min_volume:.0f}"
        )
    else:
        if context_mult < 1.0:
            context_volume = _round_api_volume_to_step(context_raw_volume, bridge_meta)
        else:
            context_volume = _floor_api_volume_to_step(context_raw_volume, bridge_meta)
        adjusted_volume = context_volume if context_volume > 0 else input_volume
    next_trace["context_policy"] = {
        **context_policy,
        "raw_api_volume": context_raw_volume,
        "adjusted_api_volume": adjusted_volume,
        "blocked_reason": blocked_reason,
    }
    if blocked_reason:
        next_trace["blocked_reason"] = blocked_reason
        next_trace["context_policy_candidate_api_volume"] = input_volume
    return adjusted_volume, next_trace


def _prepare_open_trade_candidate(
    *,
    bridge: Any,
    pipeline: dict,
    cfg: Any,
    bar: dict[str, Any],
    factor_values: dict[str, Any],
    composite: Any,
    positions: list,
    current_price: float,
    atr_price: float,
    tick: int,
    log,
) -> _OpenTradeCandidate:
    bridge_meta = _resolve_open_trade_bridge_meta(bridge)
    preflight = _tick_build_open_order_preflight(
        direction=int(composite.direction or 0),
        current_price=float(current_price or 0.0),
        atr_price=float(atr_price or 0.0),
        strategy_sl_atr=float(getattr(cfg, "strategy_sl_atr", 0.0) or 0.0),
        strategy_tp_atr=float(getattr(cfg, "strategy_tp_atr", 0.0) or 0.0),
        bridge_meta=bridge_meta,
        protection_prices=_protection_prices_from_reference,
    )
    direction_name = str(preflight["direction_name"])
    sl_dist = float(preflight["sl_dist"])
    tp_dist = float(preflight["tp_dist"])
    digits = int(preflight["digits"])
    sl_price = float(preflight["sl_price"])
    tp_price = float(preflight["tp_price"])

    account_for_risk = _live_state_get("account", {}, clone=True) or {}
    sizing_result = _risk_kelly_sizing(
        cfg,
        composite.direction,
        current_price,
        sl_price,
        bridge_meta,
        account_for_risk,
    )
    base_volume = float(sizing_result.get("volume") or 0.0)
    sizing_trace = dict(sizing_result.get("trace") or {})
    event_sizing_context = _event_sizing_context(
        pipeline.get("event_sizing"),
        float(bar.get("time", time.time()) or time.time()),
    )
    try:
        event_multiplier = float(event_sizing_context.get("multiplier", 1.0))
    except (TypeError, ValueError):
        event_multiplier = 1.0
    event_sizing_result = _apply_entry_event_sizing(
        base_volume=base_volume,
        event_multiplier=event_multiplier,
        bridge_meta=bridge_meta,
        sizing_trace=sizing_trace,
    )
    adjusted_volume = float(event_sizing_result.get("volume") or 0.0)
    sizing_trace = dict(event_sizing_result.get("trace") or {})
    sizing_block_reason = str(event_sizing_result.get("blocked_reason") or "")
    effective_sizing = _tick_build_effective_event_sizing_payload(
        base_volume=base_volume,
        adjusted_volume=adjusted_volume,
        sizing_trace=sizing_trace,
        sizing_block_reason=sizing_block_reason,
        event_sizing_context=event_sizing_context,
    )
    volume = float(effective_sizing["volume"])
    sizing_trace = dict(effective_sizing["sizing_trace"])
    event_sizing_context = dict(effective_sizing["event_sizing_context"])
    volume, sizing_trace = _apply_context_position_sizing(
        volume=volume,
        sizing_trace=sizing_trace,
        composite=composite,
        bridge_meta=bridge_meta,
    )

    log(
        f"tick {tick}: v4 {direction_name} req_api_volume={volume:.0f} "
        f"(Kelly enabled={getattr(cfg, 'kelly_enabled', False)} "
        f"event_mult={event_multiplier:.2f} base_api_volume={base_volume:.0f})"
    )

    event_filter_context = _event_filter_context_for_risk_policy(
        cfg=cfg,
        direction=int(composite.direction or 0),
        bar=bar,
        factor_values=factor_values,
    )
    risk_context = _build_open_trade_risk_context(
        cfg=cfg,
        bridge=bridge,
        acct=account_for_risk,
        positions=positions,
        requested_api_volume=volume,
        signal_score=float(composite.score or 0.0),
        symbol="XAUUSD",
        direction=int(composite.direction or 0),
        current_price=float(current_price or 0.0),
        atr_price=float(atr_price or 0.0),
        event_sizing_context=event_sizing_context,
        event_filter_context=event_filter_context,
        decision_quality_context=_decision_quality_context(composite),
        decision_ts=float(bar.get("time", time.time()) or time.time()),
    )
    risk_verdict = _RISK_POLICY.evaluate("open_trade", risk_context)
    market_session = _live_state_get("market_session", {}, clone=True) or {}
    order_block = _tick_build_market_order_block(
        market_session=market_session,
        risk_verdict=risk_verdict,
    )
    quote = _live_state_get("spot_quote", {}, clone=True) or {}
    bid = float(quote.get("bid") or 0.0)
    ask = float(quote.get("ask") or 0.0)
    spread_points = max(0.0, ask - bid) if bid > 0 and ask > 0 else 0.0
    try:
        from config import load_config

        settings = load_config()
    except Exception:
        settings = {}
    commission_per_lot = float(((settings.get("commission") or {}).get("value") or 0.0))
    slippage_points = float(((settings.get("execution") or {}).get("slippage_value") or 0.0))
    lots = max(0.0, volume) / 10000.0
    ounces = lots * float(bridge_meta.get("lot_size") or 0.0)
    uncertainty_points = max(0.0, atr_price) * float(
        getattr(cfg, "entry_edge_uncertainty_atr_ratio", 0.10) or 0.10
    )
    gross_edge = max(0.0, tp_dist) * ounces
    estimated_cost = (
        (spread_points + slippage_points + uncertainty_points) * ounces
        + commission_per_lot * lots
    )
    edge_evidence = {
        "schema_version": "entry_cost_edge.v1",
        "gross_edge": gross_edge,
        "estimated_cost": estimated_cost,
        "net_edge": gross_edge - estimated_cost,
        "spread_points": spread_points,
        "expected_slippage_points": slippage_points,
        "uncertainty_points": uncertainty_points,
        "commission_per_lot": commission_per_lot,
        "lots": lots,
        "lot_size": float(bridge_meta.get("lot_size") or 0.0),
    }
    audit_payload = dict(getattr(risk_verdict, "audit_payload", {}) or {})
    audit_payload["entry_cost_edge"] = edge_evidence
    risk_verdict.audit_payload = audit_payload
    if not bool(order_block.get("order_blocked")) and (ounces <= 0 or gross_edge <= estimated_cost):
        order_block = {
            **order_block,
            "order_blocked": True,
            "block_reason": "no_positive_edge_after_costs",
            "skip_stage": "entry_cost_edge",
        }
    if not bool(order_block.get("order_blocked")):
        try:
            model_veto = _evaluate_open_quality_model_veto(
                cfg=cfg,
                bridge=bridge,
                bar=bar,
                composite=composite,
                positions=positions,
                current_price=current_price,
                event_context=event_sizing_context,
                rule_decision={
                    "passed": True,
                    "risk_reason": str(getattr(risk_verdict, "reason", "") or ""),
                    "entry_cost_edge": edge_evidence,
                },
            )
        except Exception as exc:
            model_veto = {
                "passed": True,
                "reason": f"model_open_veto_unavailable:{type(exc).__name__}",
            }
        audit_payload = dict(getattr(risk_verdict, "audit_payload", {}) or {})
        audit_payload["model_open_quality"] = model_veto
        risk_verdict.audit_payload = audit_payload
        if not bool(model_veto.get("passed", True)):
            order_block = {
                **order_block,
                "order_blocked": True,
                "block_reason": "model_open_quality_veto",
                "skip_stage": "model_influence",
            }
    if not bool(order_block.get("order_blocked")):
        # Build the same canonical context that the filled-open ledger will
        # persist.  The broker fill is intentionally allowed to be unknown at
        # this point, but every other training input must already be present;
        # otherwise the order is rejected before broker mutation instead of
        # creating another permanently untrainable open sample.
        try:
            pre_open_context = _open_learning_context_payload(
                bridge=bridge,
                bar=bar,
                positions_before=positions,
                composite=composite,
                symbol="XAUUSD+",
                pid=0,
                actual_api_volume=volume,
                requested_volume=volume,
                base_requested_volume=base_volume,
                current_price=current_price,
                fill_price=0.0,
                sl_price=sl_price,
                tp_price=tp_price,
                sl_dist=sl_dist,
                tp_dist=tp_dist,
                event_sizing_context=event_sizing_context,
                sizing_trace=sizing_trace,
                risk_verdict=risk_verdict,
                market_session=market_session,
            )
            context_quality = _lifecycle_validate_open_learning_context(
                pre_open_context,
                require_fill=False,
            )
        except Exception as exc:
            context_quality = {
                "schema_version": "open_learning_context.v2",
                "ready": False,
                "missing_fields": [],
                "invalid_fields": [f"capture_exception:{type(exc).__name__}"],
                "require_fill": False,
            }
        audit_payload = dict(getattr(risk_verdict, "audit_payload", {}) or {})
        audit_payload["open_learning_context_quality"] = context_quality
        risk_verdict.audit_payload = audit_payload
        if not bool(context_quality.get("ready")):
            order_block = {
                **order_block,
                "order_blocked": True,
                "block_reason": "open_learning_context_incomplete",
                "skip_stage": "learning_context",
                "learning_context_quality": context_quality,
            }
    nursery_reservation_id = ""
    audit_payload = dict(getattr(risk_verdict, "audit_payload", {}) or {})
    observations = list(audit_payload.get("demo_nursery_observations") or [])
    if observations and not bool(order_block.get("order_blocked")):
        try:
            from backend.services.nursery_exploration_budget import (
                NurseryExplorationBudgetService,
                build_setup_fingerprint,
            )

            decision_quality = _decision_quality_context(composite)
            context_state = dict(decision_quality.get("context_state") or {})
            top_contributors = list(decision_quality.get("top_contributors") or [])
            setup_fingerprint = build_setup_fingerprint(
                symbol="XAUUSD+",
                direction=int(composite.direction or 0),
                regime=str(decision_quality.get("regime_id") or ""),
                session=str(context_state.get("session_state") or ""),
                event_state=str(context_state.get("event_window_state") or ""),
                signal_score=float(composite.score or 0.0),
                alpha_family=[str(item.get("factor") or "") for item in top_contributors],
            )
            reservation = NurseryExplorationBudgetService().reserve(
                reasons=[str(item.get("reason") or "") for item in observations],
                setup_fingerprint=setup_fingerprint,
                per_reason_limit=int(getattr(cfg, "nursery_exploration_per_reason_daily_limit", 5) or 5),
                global_limit=int(getattr(cfg, "nursery_exploration_global_daily_limit", 15) or 15),
                setup_limit=int(getattr(cfg, "nursery_exploration_setup_daily_limit", 1) or 1),
                ttl_seconds=int(getattr(cfg, "nursery_exploration_reservation_ttl_seconds", 300) or 300),
            )
            audit_payload["nursery_exploration_budget"] = reservation
            risk_verdict.audit_payload = audit_payload
            if reservation.get("allowed"):
                nursery_reservation_id = str(reservation.get("reservation_id") or "")
            else:
                order_block = {
                    **order_block,
                    "order_blocked": True,
                    "block_reason": str(reservation.get("status") or "nursery_exploration_budget_exhausted"),
                    "skip_stage": "nursery_exploration_budget",
                }
        except Exception as exc:
            order_block = {
                **order_block,
                "order_blocked": True,
                "block_reason": "nursery_exploration_budget_unavailable",
                "skip_stage": "nursery_exploration_budget",
            }
            logger.warning("[live] nursery exploration reservation failed closed: {}", exc)

    return _OpenTradeCandidate(
        direction_name=direction_name,
        bridge_meta=bridge_meta,
        digits=digits,
        sl_dist=sl_dist,
        tp_dist=tp_dist,
        sl_price=sl_price,
        tp_price=tp_price,
        base_volume=base_volume,
        volume=volume,
        event_multiplier=event_multiplier,
        event_sizing_context=event_sizing_context,
        sizing_trace=sizing_trace,
        risk_verdict=risk_verdict,
        market_session=market_session,
        order_block=order_block,
        nursery_reservation_id=nursery_reservation_id,
    )


def _blocked_open_trade_gate_result(block_reason: str):
    return type("GateResult", (), {
        "passed": False,
        "reason": block_reason,
    })()


def _maybe_tighten_incident_for_live_autonomy_budget_breach(risk_verdict: Any, *, tick: int, log) -> dict[str, Any]:
    verdict = risk_verdict.to_dict() if hasattr(risk_verdict, "to_dict") else dict(risk_verdict or {})
    payload = dict(verdict.get("audit_payload") or {})
    reason = str(verdict.get("reason") or "")
    if reason != "live_autonomy_budget_breach" and payload.get("source") != "live_autonomy_budget":
        return {"ok": True, "status": "not_budget_breach"}

    target_mode = str(payload.get("recommended_incident_mode") or "no_new_risk").strip().lower()
    if target_mode != "no_new_risk":
        target_mode = "no_new_risk"

    try:
        service = RuntimeIncidentControlService()
        current_mode = str((service.status() or {}).get("mode") or "normal").strip().lower()
        if INCIDENT_MODE_RANK.get(current_mode, 0) >= INCIDENT_MODE_RANK[target_mode]:
            return {"ok": True, "status": "already_strict", "current_mode": current_mode, "target_mode": target_mode}
        result = service.set_mode(
            target_mode,
            reason="live_autonomy_budget_breach",
            actor="system:live_autonomy_budget",
            confirm_thaw=False,
        )
        log(f"tick {tick}: live autonomy budget breach incident tighten -> {target_mode} ({result.get('status')})")
        return dict(result or {})
    except Exception as exc:
        logger.warning("[live] live autonomy budget incident tighten failed: %s", exc)
        return {"ok": False, "status": "incident_tighten_failed", "error": str(exc)[:300]}


def _record_open_trade_blocked_by_policy(
    *,
    bridge: Any,
    cfg: Any,
    bar: dict,
    account: dict,
    positions: list,
    composite: Any,
    candidate: _OpenTradeCandidate,
    current_price: float,
    tick: int,
    log,
):
    block_reason = str(candidate.order_block["block_reason"])
    log(f"tick {tick}: v4 {candidate.direction_name} SKIP ({block_reason})")
    _maybe_tighten_incident_for_live_autonomy_budget_breach(candidate.risk_verdict, tick=tick, log=log)
    gate_result = _blocked_open_trade_gate_result(block_reason)
    if not _LEDGER:
        return gate_result
    try:
        learning_context = _open_learning_context_payload(
            bridge=bridge,
            bar=bar,
            positions_before=positions,
            composite=composite,
            symbol="XAUUSD+",
            pid=0,
            actual_api_volume=0.0,
            requested_volume=float(candidate.volume or 0.0),
            base_requested_volume=float(candidate.base_volume or 0.0),
            current_price=float(current_price or 0.0),
            fill_price=0.0,
            sl_price=float(candidate.sl_price or 0.0),
            tp_price=float(candidate.tp_price or 0.0),
            sl_dist=float(candidate.sl_dist or 0.0),
            tp_dist=float(candidate.tp_dist or 0.0),
            event_sizing_context=candidate.event_sizing_context,
            sizing_trace=candidate.sizing_trace,
            risk_verdict=candidate.risk_verdict,
            market_session=candidate.market_session,
        )
        _LEDGER.log_composite_decision(
            **_tick_build_skip_ledger_payload(
                composite=composite,
                gate_result=gate_result,
                cfg=cfg,
                bar=bar,
                account=account,
                positions_before=positions,
                risk_state=_risk_state_with_verdict(candidate.risk_verdict),
                risk_verdict=candidate.risk_verdict,
                block_reason=block_reason,
                skip_stage=str(candidate.order_block["skip_stage"]),
                tick=tick,
                sizing_trace=candidate.sizing_trace,
                market_session=candidate.market_session,
                event_sizing_context=candidate.event_sizing_context,
                learning_context=learning_context,
                decision_ts_fallback=time.time(),
            )
        )
    except Exception as _ledger_err:
        logger.debug("[live] ledger risk policy skip failed: %s", _ledger_err)
    return gate_result


def _record_open_trade_admission_blocked(
    *,
    cfg: Any,
    bar: dict[str, Any],
    account: dict[str, Any],
    positions: list,
    composite: Any,
    gate_result: Any,
    blockers: tuple[str, ...],
    block_reason: str,
    skip_stage: str,
    tick: int,
) -> None:
    """Persist a signal-pass admission stop before RiskPolicy is reached.

    This is deliberately a ledger-only observation.  It does not invoke
    RiskPolicy, create an execution intent, or turn the admission blocker into
    a risk verdict.  The same decision ledger remains the audit authority used
    by later risk/order/position records.
    """
    if not _LEDGER:
        return
    action_json = {
        "bar_ts": (bar or {}).get("time"),
        "gate_passed": bool(getattr(gate_result, "passed", False)),
        "admission_gate_passed": False,
        "blockers": list(blockers),
        "action_reason": str(block_reason or "open_admission_blocked"),
        "execution_intent_created": False,
    }
    try:
        payload = _tick_build_skip_ledger_payload(
            composite=composite,
            gate_result=gate_result,
            cfg=cfg,
            bar=bar,
            account=account,
            positions_before=positions,
            risk_state=_live_state_get("risk", {}, clone=True) or {},
            risk_verdict=None,
            block_reason=block_reason,
            skip_stage=skip_stage,
            tick=tick,
            sizing_trace={},
            market_session=_live_state_get("market_session", {}, clone=True) or {},
            event_sizing_context={},
            learning_context={},
            decision_ts_fallback=time.time(),
        )
        payload["action_json"].update(action_json)
        decision_id = _LEDGER.log_composite_decision(**payload)
        logger.debug(
            "[live] open admission blocker audited decision_id=%s stage=%s blockers=%s",
            decision_id,
            skip_stage,
            list(blockers),
        )
    except Exception as ledger_error:
        # Admission remains fail-closed when the audit sink is unavailable;
        # the ledger is observability, never permission to submit an order.
        logger.debug(
            "[live] open admission blocker ledger persist failed: %s",
            ledger_error,
        )


def _serialize_open_risk_verdict(verdict: Any) -> dict[str, Any]:
    if verdict is None:
        return {}
    if hasattr(verdict, "to_dict"):
        try:
            payload = verdict.to_dict()
            return dict(payload) if isinstance(payload, dict) else {}
        except Exception:
            return {}
    if isinstance(verdict, dict):
        return dict(verdict)
    payload = getattr(verdict, "__dict__", {})
    return dict(payload) if isinstance(payload, dict) else {}


def _open_runtime_factor_lineage() -> dict[str, Any]:
    try:
        from backend.services.runtime_factor_selection_projection import (
            RuntimeFactorSelectionProjectionService,
        )

        projection = RuntimeFactorSelectionProjectionService().latest(
            max_age_seconds=900.0
        )
    except Exception as exc:
        return {
            "schema_version": "runtime_factor_selection.v1",
            "status": "unavailable",
            "error": f"{type(exc).__name__}:{exc}",
            "live_generation_id": _current_generation_id(),
        }
    status = str(projection.get("status") or "unknown")
    return {
        "schema_version": "runtime_factor_selection.v1",
        "status": status,
        "source": str(projection.get("source") or ""),
        "selection_fingerprint": str(
            projection.get("selection_fingerprint") or ""
        ),
        "config_version": int(projection.get("config_version") or 0),
        "config_hash": str(projection.get("config_hash") or ""),
        "live_generation_id": str(
            projection.get("live_generation_id") or _current_generation_id()
        ),
        "published_at": float(projection.get("published_at") or 0.0),
        "age_seconds": float(projection.get("age_seconds") or 0.0),
    }


def _open_causal_timing(*, decision_ts: float, intent_prepared_at: float) -> dict[str, Any]:
    """Describe the known hot-path timestamps without inferring later success."""
    def known(ts: float) -> dict[str, Any]:
        return {"ts": float(ts or 0.0) or None, "status": "known", "reason": ""}

    def unknown(reason: str) -> dict[str, Any]:
        return {"ts": None, "status": "unknown", "reason": str(reason or "unknown")}

    return {
        "schema_version": "causal_timing.v1",
        "stages": {
            "decision": known(decision_ts),
            "intent_prepared": known(intent_prepared_at),
            "intent_submitted": unknown("broker_submission_pending"),
            "broker_ack": unknown("broker_ack_pending"),
            "order_position": unknown("broker_identity_pending"),
            "supervisor": unknown("position_supervision_pending"),
            "review": unknown("trade_review_pending"),
            "learning": unknown("learning_application_pending"),
            "effect": unknown("learning_effect_not_observed"),
        },
        "path": {
            "hot": [
                "factor_selection",
                "open_intent",
                "risk_policy",
                "broker_execution_intent",
                "protection",
                "supervision",
            ],
            "warm": ["trade_review", "feature_provider", "posterior", "candidate_review"],
            "cold": ["v16_command", "governance_coordinator", "mutation_effect"],
        },
    }


def _prepare_open_trade_intent(
    *,
    bridge: Any,
    broker: str,
    cfg: Any,
    bar: dict[str, Any],
    tick: int,
    account: dict[str, Any],
    positions: list[Any],
    composite: Any,
    gate_result: Any,
    candidate: _OpenTradeCandidate,
    current_price: float,
    signal_decision_id: str = "",
) -> str:
    """Persist the root decision immediately before broker submission."""
    if not _LEDGER:
        raise RuntimeError("canonical_risk_decision_unavailable")
    decision_ts = float((bar or {}).get("time") or time.time())
    intent_prepared_at = time.time()
    risk_payload = _serialize_open_risk_verdict(candidate.risk_verdict)
    audit_payload = dict(risk_payload.get("audit_payload") or {})
    runtime_binding = _open_runtime_factor_lineage()
    factor_set_version = str(
        runtime_binding.get("selection_fingerprint") or ""
    )
    config_version = int(runtime_binding.get("config_version") or 0)
    action_json = {
        "schema_version": "open_intent.v1",
        "tick": int(tick or 0),
        "decision_ts": decision_ts,
        "signal_decision_id": str(signal_decision_id or ""),
        "runtime_binding": runtime_binding,
        "factor_set_version": factor_set_version,
        "config_version": config_version,
        "config_hash": str(runtime_binding.get("config_hash") or ""),
        "policy_version": str(getattr(cfg, "policy_version", "") or ""),
        "evidence_refs": [
            ref
            for ref in (
                f"signal_decision:{signal_decision_id}" if signal_decision_id else "",
                f"factor_selection:{factor_set_version}" if factor_set_version else "",
            )
            if ref
        ],
        "causal_timing": _open_causal_timing(
            decision_ts=decision_ts,
            intent_prepared_at=intent_prepared_at,
        ),
        "requested_direction": int(getattr(composite, "direction", 0) or 0),
        "current_price": float(current_price or 0.0),
        "risk_verdict": risk_payload,
        "sizing": {
            "base_volume": float(candidate.base_volume or 0.0),
            "requested_volume": float(candidate.volume or 0.0),
            "event_multiplier": float(candidate.event_multiplier or 0.0),
            "sizing_trace": dict(candidate.sizing_trace or {}),
        },
        "learning_context_quality": dict(
            audit_payload.get("open_learning_context_quality") or {}
        ),
        "execution_intent_created": False,
    }
    return _LEDGER.log_composite_decision(
        event_type="open_intent",
        composite=composite,
        gate_result=gate_result,
        symbol="XAUUSD+",
        timeframe=str(getattr(cfg, "timeframe", "") or ""),
        decision_ts=decision_ts,
        portfolio_state={
            "balance": (account or {}).get("balance", 0),
            "equity": (account or {}).get("equity", 0),
            "n_positions": len(positions or []),
            "session_pnl": _live_state_get("session_pnl", 0),
        },
        risk_state=_risk_state_with_verdict(candidate.risk_verdict),
        policy_version=str(getattr(cfg, "policy_version", "") or ""),
        factor_set_version=factor_set_version,
        action_reason="open_intent",
        action_json={
            **action_json,
            "config_version": config_version,
            "config_hash": str(runtime_binding.get("config_hash") or ""),
        },
    )


def _submit_open_trade_order(
    bridge: Any,
    composite: Any,
    volume: float,
    *,
    decision_id: str,
    trade_id: str,
    risk_verdict: Any,
):
    risk_payload = _serialize_open_risk_verdict(risk_verdict)
    if composite.direction == 1:
        return bridge.market_buy(
            volume=volume,
            sl=0.0,
            tp=0.0,
            comment="quant-v4",
            decision_id=str(decision_id or ""),
            trade_id=str(trade_id or ""),
            risk_verdict=risk_payload,
        )
    if composite.direction == -1:
        return bridge.market_sell(
            volume=volume,
            sl=0.0,
            tp=0.0,
            comment="quant-v4",
            decision_id=str(decision_id or ""),
            trade_id=str(trade_id or ""),
            risk_verdict=risk_payload,
        )
    return None


def _persist_pending_entry_protection_plan(
    *,
    broker: str,
    position_id: int,
    composite: Any,
    fill_price: float,
    current_price: float,
    actual_api_volume: float,
    sl_price: float,
    tp_price: float,
    entry_protection_plan: dict[str, Any],
    tick: int,
    entry_decision_id: str = "",
    execution_intent_id: str = "",
) -> None:
    try:
        _upsert_recovery_position_state(
            {
                "position_id": position_id,
                "symbol": "XAUUSD+",
                "direction": composite.direction,
                "open_price": float(fill_price or current_price),
                "volume": float(actual_api_volume),
                "entry_decision_id": str(
                    entry_decision_id or _lookup_entry_decision_id(int(position_id))
                ),
            },
            broker=broker,
            strategy_name=_current_loop_strategy_name(),
            status="open",
            meta={
                "tick": tick,
                "sl": round(sl_price, 2),
                "tp": round(tp_price, 2),
                "entry_protection_plan": entry_protection_plan,
                "entry_decision_id": str(entry_decision_id or ""),
                "execution_intent_id": str(execution_intent_id or ""),
            },
        )
    except Exception as _protection_plan_err:
        logger.debug(
            "[live] entry protection plan persist failed for pos %s: %s",
            position_id,
            _protection_plan_err,
        )


def _attach_open_trade_protection(
    *,
    bridge: Any,
    attr_engine: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    position_id: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    fill_price: float,
    current_price: float,
    sl_price: float,
    tp_price: float,
    sl_dist: float,
    tp_dist: float,
    account: dict,
    positions: list,
    composite: Any,
    gate_result: Any,
    candidate: _OpenTradeCandidate,
    entry_protection_plan: dict[str, Any],
    log,
    submit_started_at: float | None = None,
    fill_received_at: float | None = None,
) -> None:
    _runtime_attach_open_trade_protection(
        OpenProtectionRequest(
            bridge=bridge,
            attr_engine=attr_engine,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            position_id=position_id,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            base_requested_volume=base_requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            account=account,
            positions=positions,
            composite=composite,
            gate_result=gate_result,
            candidate=candidate,
            entry_protection_plan=entry_protection_plan,
            log=log,
            submit_started_at=submit_started_at,
            fill_received_at=fill_received_at,
        ),
        runtime=OpenProtectionRuntime(
            amend_position=lambda *, bridge, position_id, sl, tp: (
                bridge.amend_position_sltp(
                    position_id=position_id,
                    sl=sl,
                    tp=tp,
                )
            ),
            reconcile_positions=_explicit_position_reconcile,
            verify_projection=_verify_position_protection_projection,
            publish_projection=_publish_fresh_position_reconcile,
            release_pending_latch=_release_entry_protection_pending_latch,
            record_success=_record_amended_open_success_context,
            record_failure=_record_amend_failure_after_fill,
            record_aux_failure=_record_risk_reduction_aux_failure,
        ),
    )


def _record_open_trade_order_failure(
    *,
    result: Any,
    cfg: Any,
    bar: dict,
    account: dict,
    positions: list,
    composite: Any,
    gate_result: Any,
    candidate: _OpenTradeCandidate,
    current_price: float,
    tick: int,
    log,
    decision_id: str = "",
) -> None:
    log(
        f"tick {tick}: v4 {candidate.direction_name} ORDER FAILED: "
        f"{getattr(result, 'error_code', '?')} {getattr(result, 'comment', '')}"
    )
    if not _LEDGER:
        return
    try:
        order_failed_payloads = _tick_build_order_failed_ledger_payloads(
            composite=composite,
            gate_result=gate_result,
            cfg=cfg,
            bar=bar,
            account=account,
            positions_before=positions,
            risk_state=_live_state_get("risk", {}, clone=True) or {},
            requested_volume=float(candidate.volume),
            current_price=float(current_price),
            sl_price=float(candidate.sl_price),
            tp_price=float(candidate.tp_price),
            tick=tick,
            error_code=str(getattr(result, "error_code", "") or ""),
            comment=str(getattr(result, "comment", "") or ""),
            decision_ts_fallback=time.time(),
        )
        failed_decision_id = str(decision_id or "")
        if not failed_decision_id:
            failed_decision_id = _LEDGER.log_composite_decision(
                **order_failed_payloads["decision"]
            )
        order_event = dict(order_failed_payloads["order_event"])
        order_event["details"] = {
            **dict(order_event.get("details") or {}),
            "decision_id": failed_decision_id,
            "execution_intent_id": str(
                getattr(result, "intent_id", "") or ""
            ),
            "parent_decision_id": str(decision_id or ""),
        }
        _LEDGER.log_order_event(
            decision_id=failed_decision_id,
            **order_event,
        )
    except Exception as _ledger_err:
        logger.debug("[live] ledger order failed event failed: %s", _ledger_err)


def _handle_open_trade_order_success(
    *,
    result: Any,
    bridge: Any,
    attr_engine: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    account: dict,
    positions: list,
    composite: Any,
    gate_result: Any,
    candidate: _OpenTradeCandidate,
    current_price: float,
    log,
    submit_started_at: float | None = None,
    fill_received_at: float | None = None,
    decision_id: str = "",
) -> None:
    fill_price = _tick_resolve_order_fill_price(result, current_price=current_price)
    position_id = _tick_resolve_order_position_id(result, positions_before=positions)
    try:
        candidate.open_decision_id = str(
            decision_id or getattr(candidate, "open_decision_id", "") or ""
        )
        candidate.execution_intent_id = str(
            getattr(result, "intent_id", "")
            or getattr(candidate, "execution_intent_id", "")
            or ""
        )
    except Exception:
        pass
    if position_id <= 0:
        failure = _persist_safety_fail_closed(
            blockers=("confirmed_open_position_identity_missing",),
            source="entry_protection_initialization",
            error=(
                "broker reported a successful market-open without a uniquely "
                "matched position_id"
            ),
        )
        reconcile = _explicit_position_reconcile(bridge)
        if bool(reconcile.get("success")):
            _publish_fresh_position_reconcile(reconcile, broker=broker)
        try:
            append_safety_outbox(
                event_type="confirmed_open_position_identity_missing",
                payload={
                    "broker": str(broker or ""),
                    "tick": int(tick),
                    "outcome": str(getattr(result, "outcome", "") or ""),
                    "intent_id": str(getattr(result, "intent_id", "") or ""),
                    "reconcile_id": str(reconcile.get("reconcile_id") or ""),
                    "reconcile_success": bool(reconcile.get("success")),
                    "safety_failure": failure,
                },
                error="confirmed_open_position_identity_missing",
            )
        except Exception:
            pass
        log(
            f"tick {tick}: v4 {candidate.direction_name} ORDER OUTCOME UNKNOWN "
            f"(confirmed response without position_id) vol={candidate.volume}"
        )
        return

    # Establish both the in-memory attach marker and durable no-new-risk cause
    # before any fallible broker refresh, price calculation, PG write, or audit.
    _remember_pending_open_attach(int(position_id))
    _activate_entry_protection_pending_latch(
        int(position_id),
        broker=broker,
        tick=tick,
    )
    refreshed_reconcile = bridge.reconcile_positions(
        getattr(bridge, "symbol", "") or "",
        force=True,
        allow_cache_fallback=False,
    )
    refreshed_positions = list(
        refreshed_reconcile.positions
        if str(getattr(refreshed_reconcile, "status", "")) == "fresh"
        else []
    )
    actual_api_volume = _resolve_position_api_volume(
        position_id,
        refreshed_positions,
        candidate.volume,
    )
    protection_prices = _tick_resolve_open_protection_prices(
        direction=int(composite.direction or 0),
        fill_price=float(fill_price or 0.0),
        current_price=float(current_price or 0.0),
        sl_dist=float(candidate.sl_dist or 0.0),
        tp_dist=float(candidate.tp_dist or 0.0),
        digits=int(candidate.digits or 2),
        position_id=int(position_id),
        refreshed_positions=refreshed_positions,
        position_open_price=_position_open_price,
        protection_prices=_protection_prices_from_reference,
    )
    sl_price = float(protection_prices["sl_price"])
    tp_price = float(protection_prices["tp_price"])
    entry_protection_plan = _entry_protection_plan_payload(
        position_id=int(position_id),
        direction=composite.direction,
        entry_price=float(fill_price or current_price),
        target_stop_loss=sl_price,
        target_take_profit=tp_price,
        requested_volume=candidate.volume,
        actual_api_volume=actual_api_volume,
        tick=tick,
        status="pending",
    )
    _persist_pending_entry_protection_plan(
        broker=broker,
        position_id=int(position_id),
        composite=composite,
        fill_price=fill_price,
        current_price=current_price,
        actual_api_volume=actual_api_volume,
        sl_price=sl_price,
        tp_price=tp_price,
        entry_protection_plan=entry_protection_plan,
        entry_decision_id=str(decision_id or ""),
        execution_intent_id=str(
            getattr(result, "intent_id", "") or ""
        ),
        tick=tick,
    )
    _attach_open_trade_protection(
        bridge=bridge,
        attr_engine=attr_engine,
        broker=broker,
        cfg=cfg,
        bar=bar,
        tick=tick,
        position_id=int(position_id),
        actual_api_volume=actual_api_volume,
        requested_volume=candidate.volume,
        base_requested_volume=candidate.base_volume,
        fill_price=fill_price,
        current_price=current_price,
        sl_price=sl_price,
        tp_price=tp_price,
        sl_dist=candidate.sl_dist,
        tp_dist=candidate.tp_dist,
        account=account,
        positions=positions,
        composite=composite,
        gate_result=gate_result,
        candidate=candidate,
        entry_protection_plan=entry_protection_plan,
        log=log,
        submit_started_at=submit_started_at,
        fill_received_at=fill_received_at,
    )


def _open_submission_runtime(bridge: Any) -> OpenSubmissionRuntime:
    """Assemble live callbacks for the canonical open-submission service."""
    return OpenSubmissionRuntime(
        probe_final_admission=_probe_final_open_admission,
        admission_lock=_OPEN_TRADE_ADMISSION_LOCK,
        open_trade_draining=_open_trade_draining,
        persist_safety_fail_closed=_persist_safety_fail_closed,
        submit_order=_submit_open_trade_order,
        prepare_open_intent=_prepare_open_trade_intent,
        handle_order_success=_handle_open_trade_order_success,
        record_order_failure=_record_open_trade_order_failure,
        reconcile_positions=_explicit_position_reconcile,
        publish_positions=_publish_fresh_position_reconcile,
        append_safety_outbox=append_safety_outbox,
        finalize_nursery_reservation=lambda reservation_id, consumed: (
            _runtime_finalize_nursery_reservation(
                reservation_id,
                consumed,
                warning=logger.warning,
            )
        ),
        now=time.time,
    )


def _submit_open_trade_candidate(
    *,
    bridge: Any,
    attr_engine: Any,
    broker: str,
    cfg: Any,
    bar: dict,
    tick: int,
    account: dict,
    positions: list,
    composite: Any,
    gate_result: Any,
    candidate: _OpenTradeCandidate,
    current_price: float,
    log,
    signal_decision_id: str = "",
    stop_requested=None,
) -> bool:
    return _runtime_submit_open_trade_candidate(
        bridge=bridge,
        attr_engine=attr_engine,
        broker=broker,
        cfg=cfg,
        bar=bar,
        tick=tick,
        account=account,
        positions=positions,
        composite=composite,
        gate_result=gate_result,
        candidate=candidate,
        current_price=current_price,
        log=log,
        signal_decision_id=signal_decision_id,
        stop_requested=stop_requested,
        runtime=_open_submission_runtime(bridge),
    )


def _probe_final_open_admission(
    *,
    bridge: Any,
    candidate: _OpenTradeCandidate,
) -> dict[str, Any]:
    """Collect fresh open-only facts before broker-mutation ownership.

    PostgreSQL probing intentionally happens outside ``_OPEN_TRADE_ADMISSION_LOCK``
    so a database outage cannot delay emergency close/reduce/tighten ownership.
    The lock rechecks draining and the durable latch before using this result.
    """

    postgres = _probe_postgres_authority(_get_final_open_probe_conn)
    try:
        quote = bridge.get_spot_quote() if hasattr(bridge, "get_spot_quote") else {}
        quote = dict(quote or {})
    except Exception as exc:
        quote = {
            "error": f"{type(exc).__name__}:{exc}"[:500],
        }
    result = _evaluate_final_open_admission(
        postgres=postgres,
        market_session=getattr(candidate, "market_session", None),
        spot_quote=quote,
    ).to_dict()
    # Keep the exact pre-submit quote available to post-fill context capture;
    # a transient bridge read failure after the broker accepts the order must
    # not erase the execution inputs already observed at the open boundary.
    if quote:
        _live_state_update(spot_quote=quote)
    _live_state_update(final_open_admission=result)
    return result


def _new_risk_reconciliation_blockers(*, now_ts: float | None = None) -> list[str]:
    """Validate broker facts at the final open-order admission boundary."""

    checked_at = float(time.time() if now_ts is None else now_ts)
    account = _live_state_get("account_reconciled", {}, clone=True) or {}
    positions = _live_state_get("positions_reconciled", None, clone=True)
    result = _evaluate_reconciliation_snapshot(
        account=account,
        account_updated_at=_live_state_get("account_updated_at", 0.0),
        account_reconcile_id=_live_state_get("account_reconcile_id", ""),
        account_reconcile_failed_at=_live_state_get(
            "account_reconcile_failed_at", 0.0
        ),
        positions=positions,
        positions_updated_at=_live_state_get("positions_updated_at", 0.0),
        positions_reconcile_id=_live_state_get("positions_reconcile_id", ""),
        positions_reconcile_failed_at=_live_state_get(
            "positions_reconcile_failed_at", 0.0
        ),
        checked_at=checked_at,
    )
    return list(result["blockers"])


def _bar_open_already_recorded(bar_ts: float) -> bool:
    """True if the durable ledger already has a successful open for this bar.

    Same-bar dedup guard: stale decision bar repair replays and pending open
    retry re-entry must not open a second position for a bar that already
    filled.  Fail-open by design (this is a duplicate guard, not a risk
    gate): an unreadable ledger logs and allows the open rather than
    silently blocking a legitimate signal.
    """

    if not bar_ts or bar_ts <= 0:
        return False
    conn = _get_state_read_conn()
    try:
        # Bounded window around the bar time (reverse keyset); canonical
        # open decisions carry decision_ts == bar_ts for the same bar.
        window = 5.0
        for candidate in iter_decision_rows(
            conn,
            min_observed_epoch=float(bar_ts) - window,
            max_observed_epoch=float(bar_ts) + window,
            reverse=True,
        ):
            if str(candidate.get("event_type") or "") == "open":
                return True
        return False
    except Exception as exc:
        logger.warning("[live] bar open dedup check failed: %s", exc)
        return False
    finally:
        conn.close()


def _open_trade_admission_blockers(stop_requested=None) -> tuple[str, ...]:
    blockers: list[str] = []
    if _process_shutdown_requested:
        blockers.append("process_shutdown_requested")
    # The durable latch is checked again while the admission lock is held by
    # _submit_open_trade_candidate.  This linearizes emergency activation with
    # the broker open RPC and fails closed if the latch ledger is unreadable.
    if no_new_risk_latched(fail_closed=True):
        blockers.append("no_new_risk_latched")
    if not _LIVE_LOOP_CONTROLLER.accepting_new_risk(_current_generation_id()):
        blockers.append("generation_not_accepting_new_risk")
    if bool(_live_state_get("loop_running", False)):
        # The controller is the only lifecycle/admission authority.  The
        # shared value is a projection for APIs and WebSocket consumers, never
        # an independent gate that can diverge from the generation.
        if str(
            _live_state_get("session_state_status", "unknown") or "unknown"
        ) != "available":
            blockers.append("session_state_unavailable")
        if bool(_live_state_get("circuit_breaker", False)):
            blockers.append("session_circuit_breaker")
        reconcile_blockers = _new_risk_reconciliation_blockers()
        _live_state_update(new_risk_reconcile_blockers=reconcile_blockers)
        blockers.extend(reconcile_blockers)
    if bool(stop_requested is not None and stop_requested()):
        blockers.append("loop_stop_requested")
    return tuple(dict.fromkeys(blockers))


def _open_trade_draining(stop_requested=None) -> bool:
    return bool(_open_trade_admission_blockers(stop_requested))


def _open_admission_gate_reason(blockers: tuple[str, ...]) -> str:
    draining = {
        "process_shutdown_requested",
        "generation_not_accepting_new_risk",
        "loop_stop_requested",
    }
    if any(blocker in draining for blocker in blockers):
        return "loop_draining"
    for blocker in blockers:
        if blocker != "accepting_new_risk_false":
            return blocker
    return blockers[0] if blockers else "open_admission_blocked"


def _watchdog_freshness_retry_eligible(
    blockers: tuple[str, ...],
    *,
    latch_status: dict[str, Any] | None = None,
) -> bool:
    """Allow one same-bar retry only for the watchdog's stale fact snapshots."""

    if set(blockers) - {"no_new_risk_latched", "accepting_new_risk_false"}:
        return False
    unknown_raw = _live_safety_watchdog_probe().get("unknown_execution_count")
    try:
        if unknown_raw is None or int(unknown_raw) != 0:
            return False
    except (TypeError, ValueError):
        return False
    latch = (
        dict(latch_status)
        if latch_status is not None
        else no_new_risk_latch_status(fail_closed=True)
    )
    causes = list(latch.get("causes") or [])
    if not bool(latch.get("active")) or len(causes) != 1:
        return False
    cause = causes[0] if isinstance(causes[0], dict) else {}
    if (
        str(cause.get("cause") or "") != "safety_freshness"
        or str(cause.get("cause_id") or "") != "safety_watchdog"
    ):
        return False
    freshness_blockers = set(
        (cause.get("metadata") or {}).get("blockers") or []
    )
    allowed = {
        "safety_freshness_stale",
        "safety_freshness_unknown",
        "account_freshness_stale",
        "account_freshness_unknown",
        "positions_freshness_stale",
        "positions_freshness_unknown",
    }
    return bool(freshness_blockers) and freshness_blockers <= allowed


def _open_admission_gate_result(
    *,
    tick: int,
    stage: str,
    blockers: tuple[str, ...],
    log,
):
    reason = _open_admission_gate_reason(blockers)
    log(
        f"tick {tick}: v4 open SKIP "
        f"({reason} stage={stage} blockers={list(blockers)})"
    )
    result = _blocked_open_trade_gate_result(reason)
    result.retryable_watchdog_freshness = (
        reason == "no_new_risk_latched"
        and _watchdog_freshness_retry_eligible(blockers)
    )
    return result


def _run_open_trade_pipeline(
    *,
    bridge: Any,
    pipeline: dict,
    broker: str,
    cfg: Any,
    bar: dict[str, Any],
    factor_values: dict[str, Any],
    composite: Any,
    gate_result: Any,
    account: dict,
    positions: list,
    attr_engine: Any,
    current_price: float,
    atr_price: float,
    pending_open_attach_ids: list[int],
    send: bool,
    tick: int,
    log,
    signal_decision_id: str = "",
    stop_requested=None,
):
    if not (composite.direction != 0 and gate_result.passed and send):
        return gate_result
    admission_blockers = _open_trade_admission_blockers(stop_requested)
    # ★ same-bar dedup: a bar that already filled must not open a second
    # position (stale decision bar repair replay / pending open retry re-entry).
    # Distinct bars may still open concurrently; this only blocks the same bar.
    if _bar_open_already_recorded(float(bar.get("time") or 0.0)):
        admission_blockers = list(admission_blockers) + ["bar_already_opened"]
    if admission_blockers:
        admission_result = _open_admission_gate_result(
            tick=tick,
            stage="before_candidate",
            blockers=admission_blockers,
            log=log,
        )
        _record_open_trade_admission_blocked(
            cfg=cfg,
            bar=bar,
            account=account,
            positions=positions,
            composite=composite,
            gate_result=gate_result,
            blockers=admission_blockers,
            block_reason=_open_admission_gate_reason(admission_blockers),
            skip_stage="before_candidate",
            tick=tick,
        )
        return admission_result
    if pending_open_attach_ids:
        log(
            f"tick {tick}: v4 open SKIP (pending_open_attach "
            f"positions={pending_open_attach_ids})"
        )
        return gate_result

    candidate = _prepare_open_trade_candidate(
        bridge=bridge,
        pipeline=pipeline,
        cfg=cfg,
        bar=bar,
        factor_values=factor_values,
        composite=composite,
        positions=positions,
        current_price=current_price,
        atr_price=atr_price,
        tick=tick,
        log=log,
    )
    if bool(candidate.order_block["order_blocked"]):
        return _record_open_trade_blocked_by_policy(
            bridge=bridge,
            cfg=cfg,
            bar=bar,
            account=account,
            positions=positions,
            composite=composite,
            candidate=candidate,
            current_price=current_price,
            tick=tick,
            log=log,
        )

    admitted = _submit_open_trade_candidate(
        bridge=bridge,
        attr_engine=attr_engine,
        broker=broker,
        cfg=cfg,
        bar=bar,
        tick=tick,
        account=account,
        positions=positions,
        composite=composite,
        gate_result=gate_result,
        candidate=candidate,
        current_price=current_price,
        log=log,
        signal_decision_id=signal_decision_id,
        stop_requested=stop_requested,
    )
    if not admitted:
        return _blocked_open_trade_gate_result("loop_draining")
    return gate_result


def _remember_or_clear_pending_open_retry(
    *,
    pipeline: dict,
    bar: dict[str, Any],
    factor_values: dict[str, Any],
    composite: Any,
    signal_gate_result: Any,
    open_result: Any,
) -> None:
    if bool(getattr(open_result, "retryable_watchdog_freshness", False)):
        pipeline["pending_open_retry"] = {
            "bar": dict(bar),
            "factor_values": dict(factor_values),
            "composite": composite,
            "gate_result": signal_gate_result,
            "signal_decision_id": str(pipeline.get("last_signal_decision_id") or ""),
        }
        return
    pipeline.pop("pending_open_retry", None)


def _retry_pending_open_trade(
    *,
    bridge: Any,
    frame: Any,
    last_bar: Any,
    broker: str,
    tick: int,
    log,
    stop_requested=None,
) -> None:
    """Retry a same-bar signal after the canonical watchdog latch recovers."""

    pipeline = _factor_pipeline
    if pipeline is None:
        return
    pending = pipeline.get("pending_open_retry")
    if not isinstance(pending, dict):
        return

    bar = dict(pending.get("bar") or {})
    current_bar = _tick_build_factor_bar(
        last_bar,
        frame,
        str(bar.get("timeframe") or "M5"),
    )
    if float(current_bar.get("time") or 0.0) != float(bar.get("time") or 0.0):
        pipeline.pop("pending_open_retry", None)
        log(f"tick {tick}: discarded stale pending open retry")
        return

    acct = _live_state_get("account", {}, clone=True) or {}
    positions_payload = _live_state_get("positions", [], clone=True) or []
    positions_probe = (
        (positions_payload.get("positions", []) or [])
        if isinstance(positions_payload, dict)
        else positions_payload
    )
    if positions_probe and not isinstance(positions_probe[0], dict):
        from backend.ws.endpoints import _position_to_dict
    else:
        _position_to_dict = None
    positions = _tick_normalize_live_positions_payload(
        positions_payload,
        position_to_dict=_position_to_dict,
    )
    current_price = float(last_bar["close"])
    if bridge is not None and hasattr(bridge, "get_spot_quote"):
        price_guard = _tick_guard_current_price_with_spot_quote(
            current_price=current_price,
            get_spot_quote=bridge.get_spot_quote,
            quote_is_fresh=_quote_is_fresh,
        )
        current_price = float(price_guard["current_price"])

    factor_values = dict(pending.get("factor_values") or {})
    atr_ratio = factor_values.get("atr_ratio", 0)
    atr_price = (
        float(atr_ratio) * current_price
        if atr_ratio and float(atr_ratio) > 0
        else 0.0
    )
    current_pids = _tick_collect_position_ids(positions)
    try:
        from config.runtime_config import shared as _runtime_config

        cfg = _runtime_config()
    except Exception:
        cfg = None
    result = _run_open_trade_pipeline(
        bridge=bridge,
        pipeline=pipeline,
        broker=broker,
        cfg=cfg,
        bar=bar,
        factor_values=factor_values,
        composite=pending["composite"],
        gate_result=pending["gate_result"],
        account=acct,
        positions=positions,
        attr_engine=pipeline.get("attribution"),
        current_price=current_price,
        atr_price=atr_price,
        pending_open_attach_ids=_active_pending_open_attach_ids(current_pids),
        send=_should_send_orders(broker),
        tick=tick,
        log=log,
        signal_decision_id=str(pending.get("signal_decision_id") or ""),
        stop_requested=stop_requested,
    )
    if bool(getattr(result, "retryable_watchdog_freshness", False)):
        return
    pipeline.pop("pending_open_retry", None)
    log(f"tick {tick}: completed pending open retry for bar={bar.get('time')}")


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

    acct = _live_state_get("account", {}, clone=True) or {}
    positions_payload = _live_state_get("positions", [], clone=True) or []
    _positions_probe = (
        (positions_payload.get("positions", []) or [])
        if isinstance(positions_payload, dict)
        else positions_payload
    )
    if _positions_probe and not isinstance(_positions_probe[0], dict):
        from backend.ws.endpoints import _position_to_dict
    else:
        _position_to_dict = None
    pos = _tick_normalize_live_positions_payload(
        positions_payload,
        position_to_dict=_position_to_dict,
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
            logger.debug("[live] spot price guard failed for tick %s: %s", tick, price_guard["error"])

    current_pids = _tick_collect_position_ids(pos)
    attr_engine = pipeline.get("attribution")
    positions_snapshot_ready = bool(_live_state_get("positions_updated_at", 0.0))
    closed_pids, current_pids, close_detection_deferred = _tick_resolve_closed_position_ids(
        previous_position_ids=_prev_position_ids,
        current_position_ids=current_pids,
        positions_snapshot_ready=positions_snapshot_ready,
        tracked_position_ids=(
            _active_recovery_position_ids_for_close_detection(broker)
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
            from execution.deal_sync import sync_close_deals_batch

            _sconn = _get_state_pg_conn()
            try:
                # tick 路径检测到的 closed_pids 语义 = broker 仓位已消失
                # (final close)。必须传空 baseline:若缺省, sync_close_deals_batch
                # 会把"当前库里已有的 close deal"当作 baseline(observed_baseline),
                # 导致 observed_ids - baseline_ids 恒为空集、delta_proven 永远
                # False —— 平仓成交已入库但永远无法确认的死锁。
                real_pnls = sync_close_deals_batch(
                    bridge,
                    _sconn,
                    closed_pids,
                    min_exec_timestamp_by_position=(
                        _recovery_last_seen_by_position(closed_pids)
                    ),
                    required_closed_volume_delta_by_position=(
                        _recovery_remaining_volume_by_position(closed_pids)
                    ),
                    baseline_close_cursor_by_position={
                        int(pid): {
                            "baseline_cursor_available": True,
                            "baseline_deal_ids": [],
                            "baseline_closed_volume": 0.0,
                        }
                        for pid in closed_pids
                    },
                    observed_close_cursor_out=close_deal_cursors,
                )
            finally:
                _sconn.close()
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
        f"pnl_session={_live_state_get('session_pnl', 0):.2f}")
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
        _live_state_get("last_processed_decision_bar_ts", 0.0),
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
        update_live_state=_live_state_update,
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
    acct = _live_state_get("account", {}, clone=True) or {}
    positions_payload = _live_state_get("positions", [], clone=True) or []
    # ★ P0 fix: 统一转 dict — 支持 dataclass / protobuf / 任意非 dict
    _positions_probe = (
        (positions_payload.get("positions", []) or [])
        if isinstance(positions_payload, dict)
        else positions_payload
    )
    if _positions_probe and not isinstance(_positions_probe[0], dict):
        from backend.ws.endpoints import _position_to_dict
    else:
        _position_to_dict = None
    pos = _tick_normalize_live_positions_payload(
        positions_payload,
        position_to_dict=_position_to_dict,
    )
    current_price = float(last_bar["close"])
    signal_decision_id = ""
    if _LEDGER:
        try:
            # Signal events are the root of the durable factor lineage. Bind
            # them to the same runtime selection projection later used by the
            # open-intent record; otherwise the ledger has a contribution
            # snapshot but cannot prove which factor set produced it.
            runtime_binding = _open_runtime_factor_lineage()
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
                    "session_pnl": _live_state_get("session_pnl", 0),
                },
                risk_state=_live_state_get("risk", {}, clone=True) or {},
                policy_version=policy_version,
                factor_set_version=factor_set_version,
                action_reason="signal_detected",
                action_json={
                    "tick": tick,
                    "runtime_binding": runtime_binding,
                    "factor_set_version": factor_set_version,
                    "policy_version": policy_version,
                },
            )
            pipeline["last_signal_decision_id"] = str(signal_decision_id or "")
        except Exception as _ledger_err:
            logger.warning("[live] ledger signal failed: %s", _ledger_err)
            pipeline["last_signal_decision_id"] = ""
    else:
        pipeline["last_signal_decision_id"] = ""

    # ── 平仓检测: 对比 _prev_position_ids 找出被 broker 关闭的仓位 ──
    current_pids = _tick_collect_position_ids(pos)
    pending_open_attach_ids = _active_pending_open_attach_ids(current_pids)
    attr_engine = pipeline.get("attribution")
    positions_snapshot_ready = bool(_live_state_get("positions_updated_at", 0.0))
    closed_pids, current_pids, close_detection_deferred = _tick_resolve_closed_position_ids(
        previous_position_ids=_prev_position_ids,
        current_position_ids=current_pids,
        positions_snapshot_ready=positions_snapshot_ready,
        tracked_position_ids=(
            _active_recovery_position_ids_for_close_detection(broker)
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
            from execution.deal_sync import sync_close_deals_batch
            _sconn = _get_state_pg_conn()
            try:
                _real_pnls = sync_close_deals_batch(
                    bridge,
                    _sconn,
                    closed_pids,
                    min_exec_timestamp_by_position=(
                        _recovery_last_seen_by_position(closed_pids)
                    ),
                    required_closed_volume_delta_by_position=(
                        _recovery_remaining_volume_by_position(closed_pids)
                    ),
                    observed_close_cursor_out=_close_deal_cursors,
                )
            finally:
                _sconn.close()
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
            logger.debug("[live] spot price guard failed for tick %s: %s", tick, price_guard["error"])

    # ── 开仓执行流水线: candidate -> risk verdict -> broker order -> post-fill audit.
    atr_val = factor_values.get("atr_ratio", 0)
    atr_price = atr_val * current_price if atr_val and atr_val > 0 else 0
    signal_gate_result = gate_result
    gate_result = _run_open_trade_pipeline(
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
    _remember_or_clear_pending_open_retry(
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
        f"pnl_session={_live_state_get('session_pnl', 0):.2f}"
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
        row = _load_recovery_position_row(pid)
        meta = dict((row or {}).get("recovery_meta") or {})
        plan = dict(meta.get("entry_protection_plan") or {})
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
                    logger.debug("[live] entry protection applied-state update failed pos=%s: %s", pid, exc)
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
        logger.debug("[live] entry protection %s update failed pos=%s: %s", status, pid, exc)


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
        loop_running=bool(_live_state_get("loop_running", True)),
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
        _record_risk_reduction_aux_failure(
            "protection_amend_projection_unverified",
            position_id=pid,
            action=candidate.action,
            error=failure_reason,
            payload={
                "source": candidate.source,
                "verification": verification,
            },
        )
        _persist_safety_fail_closed(
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
        _merge_recovery_position_meta(
            pid,
            {
                MARKET_CLOSED_DEFER_REASON_KEY: "market_closed_pending",
                MARKET_CLOSED_DEFER_TS_KEY: now_ts,
                "market_closed_defer_wall_holding_seconds": round(holding_seconds, 3),
                "market_closed_defer_open_holding_seconds": round(market_open_holding, 3),
            },
        )
    except Exception as exc:
        _record_risk_reduction_aux_failure(
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
            logger.warning("[live] holding timeout close blocked pos=%s reason=%s", pid, close_verdict.reason)
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
            logger.warning("[live] holding timeout close exception pos=%s: %s", pid, exc)
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
            _remember_close_reason(pid, "holding_timeout")
            _remember_close_verdict(pid, close_verdict)
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
                    _merge_recovery_position_meta(
                        pid,
                        {
                            MARKET_CLOSED_DEFER_REASON_KEY: "market_closed_pending",
                            MARKET_CLOSED_DEFER_TS_KEY: now_ts,
                            "market_closed_close_rejection": rejection_text[:300],
                        },
                    )
                except Exception as exc:
                    _record_risk_reduction_aux_failure(
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
        record_aux_failure=_record_risk_reduction_aux_failure,
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

def _check_business_alerts(tick: int, acct: dict, pos: list, log) -> None:
    """每 tick 检查业务告警规则, 通过 Alerter 发送。

    规则:
      1. 连亏 ≥ 3 笔 → WARNING
      2. 当日回撤 ≥ 3% → WARNING, ≥ 5% → ERROR
      3. 熔断触发 → CRITICAL (已在 circuit 逻辑中触发, 此处仅补发)
    """
    try:
        from monitor.alerter import Alerter
        _alerter = Alerter({"log_file": "logs/alerts.log", "min_level": "WARNING"})

        # 规则 1: 连亏
        consec = int(_live_state_get("session_consecutive_loss", 0))
        if consec >= 3 and tick % 10 == 0:  # 每 10 tick 发一次, 避免刷屏
            _alerter.send("WARNING", f"⚠️ 连续亏损 {consec} 笔",
                          f"Tick: {tick}\nConsecutive Loss: {consec}\n"
                          f"Session PnL: ${_live_state_get('session_pnl', 0):.2f}")

        # 规则 2: 当日回撤
        dd_pct = float(_live_state_get("session_max_drawdown_pct", 0))
        balance = float(acct.get("balance", 0))
        if dd_pct >= 5.0 and tick % 10 == 0:
            _alerter.send("ERROR", f"🔴 当日回撤 {dd_pct:.1f}%",
                          f"Tick: {tick}\nDrawdown: {dd_pct:.1f}%\n"
                          f"Balance: ${balance:.2f}\n"
                          f"Session PnL: ${_live_state_get('session_pnl', 0):.2f}")
        elif dd_pct >= 3.0 and tick % 10 == 0:
            _alerter.send("WARNING", f"⚠️ 当日回撤 {dd_pct:.1f}%",
                          f"Tick: {tick}\nDrawdown: {dd_pct:.1f}%\n"
                          f"Balance: ${balance:.2f}")

        # 规则 3: 熔断确认
        if _live_state_get("circuit_breaker") and tick % 10 == 0:
            reason = _live_state_get("circuit_reason", "unknown")
            _alerter.send("CRITICAL", "🔴 熔断触发",
                          f"Tick: {tick}\nReason: {reason}\n"
                          f"Session PnL: ${_live_state_get('session_pnl', 0):.2f}")

        # 每 50 tick 输出执行质量摘要
        if tick > 0 and tick % 50 == 0:
            summary = _exec_quality.summary()
            if _exec_quality.report().get("n_filled", 0) > 0:
                log(f"tick {tick}: {summary}")

    except Exception as _e:
        logger.debug("[live] _check_business_alerts failed: %s", _e)
