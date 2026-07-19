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
import json
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

# ── DecisionLog (v4 决策审计) ─────────────────────────────────
from db.store import DecisionLogStore
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
from backend.services.live_legacy_safety_preview import (
    preview_legacy_safety_candidates,
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
from backend.services.live_reconciliation import (
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
    LoopOwnershipSnapshot,
    stop_live_loop as _runtime_stop_live_loop,
)
from backend.services.live_loop_start import (
    LiveLoopStartRuntime,
    start_live_loop as _runtime_start_live_loop,
)
from backend.services.live_loop_tick_runtime import (
    LegacyLiveLoopTickRuntime,
    LiveLoopTickRuntime,
    run_legacy_live_loop_tick_body as _runtime_run_legacy_live_loop_tick_body,
    run_live_loop_tick_body as _runtime_run_live_loop_tick_body,
)
from backend.services.live_execution_recovery import (
    ExecutionRecoveryRuntime,
    PositionRecoveryRuntime,
    bootstrap_position_recovery as _runtime_bootstrap_position_recovery,
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
    state_get as _runtime_state_get,
    state_set as _runtime_state_set,
    state_update as _runtime_state_update,
)
from backend.services.session_restore import (
    PartialCloseSessionFactRuntime,
    authoritative_close_pnl as _authoritative_close_pnl,
    derive_session_start_balance as _derive_session_start_balance,
    load_authoritative_session_deal_facts as _session_load_authoritative_deal_facts,
    parse_degraded_session_cache as _parse_degraded_session_cache,
    rebuild_session_risk_projection as _rebuild_session_risk_projection,
    session_trade_window as _session_restore_trade_window,
    sync_partial_close_session_fact as _session_sync_partial_close_fact,
)
from backend.services.live_ctrader_runtime import CTraderRuntime
from backend.services.live_data_sync_job import make_data_sync_job as _make_data_sync_job
from backend.services.live_data_sync_helpers import (
    DATA_SYNC_CRON as _DATA_SYNC_CRON,
    classify_decision_bar_freshness as _sync_classify_decision_bar_freshness,
    dataframe_to_store_bars as _sync_dataframe_to_store_bars,
)
from backend.services.live_decision_pipeline import (
    build_signal_decision_log_payload as _decision_build_signal_decision_log_payload,
    run_live_decision_pipeline as _decision_run_live_decision_pipeline,
)
from backend.services.live_factor_state import (
    commit_ready_factor_decision as _factor_state_commit_ready_decision,
    resolve_decision_bar_progress as _factor_state_resolve_bar_progress,
)
from config.runtime_config import autonomy_expansion_freeze_applies
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
    loop_status_snapshot as _loop_status_snapshot,
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
    build_close_decision_audit_meta as _tick_build_close_decision_audit_meta,
    build_close_ledger_payloads as _tick_build_close_ledger_payloads,
    build_effective_event_sizing_payload as _tick_build_effective_event_sizing_payload,
    build_amend_failed_ledger_payloads as _tick_build_amend_failed_ledger_payloads,
    build_trade_review_payload as _tick_build_trade_review_payload,
    build_market_order_block as _tick_build_market_order_block,
    build_open_order_preflight as _tick_build_open_order_preflight,
    build_open_decision_log_payload as _tick_build_open_decision_log_payload,
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
    account_unrealized_pnl as _lifecycle_account_unrealized_pnl,
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
    build_holding_timeout_result_trace_fields as _lifecycle_build_holding_timeout_result_trace_fields,
    build_holding_timeout_verdict_payload as _lifecycle_build_holding_timeout_verdict_payload,
    build_market_micro_context_payload as _lifecycle_build_market_micro_context_payload,
    build_open_learning_context_payload as _lifecycle_build_open_learning_context_payload,
    build_open_trade_risk_context_payload as _lifecycle_build_open_trade_risk_context_payload,
    build_position_path_metrics_update as _lifecycle_build_position_path_metrics_update,
    build_position_path_metrics_inputs as _lifecycle_build_position_path_metrics_inputs,
    build_replayed_close_payloads as _lifecycle_build_replayed_close_payloads,
    build_recovered_open_ledger_payloads as _lifecycle_build_recovered_open_ledger_payloads,
    build_protection_execution_plan as _lifecycle_build_protection_execution_plan,
    build_protection_execution_result_payloads as _lifecycle_build_protection_execution_result_payloads,
    build_position_supervisor_context_inputs as _lifecycle_build_position_supervisor_context_inputs,
    build_position_supervisor_context_payload as _lifecycle_build_position_supervisor_context_payload,
    build_position_protection_cycle_result as _lifecycle_build_position_protection_cycle_result,
    build_legacy_awe_trailing_update as _lifecycle_build_legacy_awe_trailing_update,
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
    protection_candidate_supersede_reason as _lifecycle_protection_candidate_supersede_reason,
    normalize_position_snapshot as _lifecycle_normalize_position_snapshot,
    position_api_volume as _lifecycle_position_api_volume,
    position_direction_from_payload as _lifecycle_position_direction_from_payload,
    position_direction_sign as _lifecycle_position_direction_sign,
    position_id_value as _lifecycle_position_id_value,
    position_open_price as _lifecycle_position_open_price,
    position_open_timestamp as _lifecycle_position_open_timestamp,
    position_price_pnl_estimate as _lifecycle_position_price_pnl_estimate,
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
    make_initial_ctrader_data_pull as _make_initial_ctrader_data_pull,
    register_external_sync_jobs as _register_external_sync_jobs,
    start_initial_ctrader_data_pull as _start_initial_ctrader_data_pull,
    start_scheduler_catch_up as _start_scheduler_catch_up,
)
from backend.services.position_metrics import normalize_path_state, update_position_path_metrics
from backend.services.position_supervisor import evaluate_position_supervisor
from backend.services.stability import record_timed
_DECISION_LOG: DecisionLogStore | None = None
_DECISION_LOG_RUN_ID: int = 0
_LEDGER: DecisionLedger | None = None
_TRADE_REVIEWER: TradeReviewer | None = None
_EXPERIENCE_BUILDER: ExperienceBuilder | None = None
_POLICY_SUGGESTER: PolicySuggester | None = None
_POSITION_QUALITY_ADVISOR: Any = None
_OPEN_QUALITY_ADVISOR: Any = None
_RISK_POLICY = RiskPolicyService.shared()
_DECISION_LOG_PENDING_PATH = Path("data/charts/decision_log.pending.jsonl")
_DECISION_LOG_PENDING_LOCK = threading.Lock()
_DECISION_LOG_LAST_DRAIN = 0.0
_RUNTIME_KV_PENDING_PATH = Path("data/charts/runtime_kv.pending.jsonl")
_RUNTIME_KV_PENDING_LOCK = threading.Lock()
_ENTRY_CLUSTER_POLICY_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": {}}
_ENTRY_CLUSTER_POLICY_CACHE_LOCK = threading.Lock()
_EVENT_WINDOW_POLICY_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": {}}
_EVENT_WINDOW_POLICY_CACHE_LOCK = threading.Lock()
_ENTRY_QUALITY_POLICY_CACHE: dict[str, Any] = {"expires_at": 0.0, "value": {}}
_ENTRY_QUALITY_POLICY_CACHE_LOCK = threading.Lock()


def _append_decision_log_pending(payload: dict[str, Any], error: str = "") -> None:
    _DECISION_LOG_PENDING_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "queued_at": time.time(),
        "error": str(error or ""),
        "payload": payload,
    }
    line = json.dumps(record, ensure_ascii=False, default=str)
    with _DECISION_LOG_PENDING_LOCK:
        with _DECISION_LOG_PENDING_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _rewrite_decision_log_pending_unlocked(lines: list[str]) -> None:
    if lines:
        tmp_path = _DECISION_LOG_PENDING_PATH.with_suffix(".pending.tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp_path.replace(_DECISION_LOG_PENDING_PATH)
    else:
        _DECISION_LOG_PENDING_PATH.unlink(missing_ok=True)


def _drain_decision_log_pending(log_store: DecisionLogStore, limit: int = 100) -> int:
    if not _DECISION_LOG_PENDING_PATH.exists():
        return 0
    with _DECISION_LOG_PENDING_LOCK:
        lines = _DECISION_LOG_PENDING_PATH.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
        if not lines:
            _DECISION_LOG_PENDING_PATH.unlink(missing_ok=True)
            return 0

        drained = 0
        remaining: list[str] = []
        for idx, raw in enumerate(lines):
            if drained >= limit:
                remaining.extend(lines[idx:])
                break
            try:
                record = json.loads(raw)
                payload = record.get("payload") if isinstance(record, dict) else None
                if not isinstance(payload, dict):
                    remaining.append(raw)
                    continue
                log_store.log(**payload)
                drained += 1
            except Exception:
                remaining.append(raw)
                remaining.extend(lines[idx + 1:])
                break
        _rewrite_decision_log_pending_unlocked(remaining)
        return drained


def _safe_decision_log(log_store: DecisionLogStore | None, **kwargs) -> None:
    global _DECISION_LOG_LAST_DRAIN
    if log_store is None:
        return
    now = time.time()
    if _DECISION_LOG_PENDING_PATH.exists() and now - _DECISION_LOG_LAST_DRAIN >= 5.0:
        _DECISION_LOG_LAST_DRAIN = now
        try:
            drained = _drain_decision_log_pending(log_store)
            if drained:
                logger.info("[live] legacy decision_log pending drained: {}", drained)
        except Exception as exc:
            logger.warning("[live] legacy decision_log pending drain deferred: {}", exc)
    try:
        log_store.log(**kwargs)
    except Exception as exc:
        try:
            _append_decision_log_pending(dict(kwargs), str(exc))
            logger.warning("[live] legacy decision_log write queued: {}", exc)
        except Exception as queue_exc:
            logger.error(
                "[live] legacy decision_log write failed and queue failed: write={} queue={}",
                exc,
                queue_exc,
            )


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
# ── 追踪止损状态 ──
# position_id → {best_price, activated, entry_price, direction}
_trailing_state: dict[int, dict] = {}
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
_position_price_pnl_estimate = _lifecycle_position_price_pnl_estimate
_account_unrealized_pnl = _lifecycle_account_unrealized_pnl
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
        collect_runtime_health=_loop_collect_open_risk_runtime_health,
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
        decision_quality_context=decision_quality_context,
        decision_ts=decision_ts,
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


def _holding_summary_for_position(position: Any, *, cfg=None, now_ts: float | None = None) -> dict:
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
    )
    return _lifecycle_build_holding_summary_from_close_context(close_context)


def _position_unrealized_pnl(position: Any) -> float:
    return _lifecycle_position_unrealized_pnl(position)


def _load_recovery_position_row(position_id: int) -> dict[str, Any]:
    if position_id <= 0:
        return {}
    conn = _get_state_read_conn()
    try:
        row = _state_execute(
            conn,
            """
            SELECT *
            FROM recovery_position_state
            WHERE position_id=?
            LIMIT 1
            """,
            (int(position_id),),
        ).fetchone()
        return _lifecycle_normalize_recovery_position_row(row)
    finally:
        conn.close()


def _merge_recovery_position_meta(position_id: int, meta: dict[str, Any] | None) -> None:
    if position_id <= 0 or not meta:
        return
    conn = _get_state_pg_conn()
    try:
        row = _state_execute(
            conn,
            "SELECT recovery_meta_json FROM recovery_position_state WHERE position_id=?",
            (int(position_id),),
        ).fetchone()
        if row is None:
            return
        payload = _lifecycle_build_recovery_meta_update_payload(
            position_id=position_id,
            existing_meta_json=row["recovery_meta_json"],
            meta=meta,
            now_ts=time.time(),
        )
        _state_execute(
            conn,
            """
            UPDATE recovery_position_state
            SET recovery_meta_json=?, last_seen_at=?
            WHERE position_id=?
            """,
            (
                json.dumps(payload["recovery_meta"], ensure_ascii=False, default=str),
                payload["last_seen_at"],
                payload["position_id"],
            ),
        )
        final_row = _state_execute(
            conn,
            "SELECT * FROM recovery_position_state WHERE position_id=?",
            (position_id,),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()


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
        loop_strategy_name=_loop_strategy_name,
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
    )
    position_metrics = _position_path_metrics_for_position(position, cfg=cfg, now_ts=now_ts, persist=False)
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
) -> dict[str, Any]:
    from backend.services.model_influence import shared_model_influence_service
    from backend.services.position_supervisor import build_model_tighten_controls

    runtime = PositionSupervisorEvaluationRuntime(
        build_context=_build_position_supervisor_context,
        evaluate_rule=evaluate_position_supervisor,
        get_quality_advisor=_get_position_quality_advisor,
        set_quality_advisor=_set_position_quality_advisor,
        quality_advisor_factory=_create_position_quality_advisor,
        model_influence_service=shared_model_influence_service,
        build_model_tighten_controls=build_model_tighten_controls,
        load_recovery_row=_load_recovery_row_for_risk_reduction,
        upsert_recovery_position=_upsert_recovery_position_state,
        build_state_upsert_payload=_lifecycle_build_supervisor_state_upsert_payload,
        loop_strategy_name=str(_loop_strategy_name or ""),
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
    account: dict | None = None,
) -> list[dict]:
    now_ts = float(now_ts or time.time())
    return _lifecycle_enrich_positions_with_lifecycle_metrics(
        pos_list,
        account=account or (_live_state_get("account", {}, clone=True) or {}),
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
                loop_strategy_name=_loop_strategy_name,
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
                loop_strategy_name=_loop_strategy_name,
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


def _remember_supervisor_noop(position: dict[str, Any], verdict: dict[str, Any], *, fingerprint: str, reason: str) -> None:
    pid = int(position.get("position_id") or position.get("ticket") or 0)
    _remember_supervisor_state(
        position,
        verdict,
        broker="ctrader",
        strategy_name=str(_loop_strategy_name or "factor_v4"),
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
        logger.debug("[live] supervisor ledger failed for pos %s: %s", position.get("position_id"), exc)
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
        logger.debug("[live] supervisor trace failed for pos %s: %s", position.get("position_id"), exc)
        return ""


def _delegate_timeout_supervisor_close(
    *,
    position: dict[str, Any],
    verdict: dict[str, Any],
    cfg: Any,
    tick: int,
    acct: dict[str, Any],
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
    return bool(timeout_limit_seconds > 0 and timeout_holding_seconds >= timeout_limit_seconds)


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
    record_partial_close_execution=None,
    decision_ts: float | None = None,
    candidate_recorder=None,
    planned_verdicts: dict[int, dict[str, Any]] | None = None,
) -> set[int]:
    runtime = LiveSupervisionRuntime(
        logger=logger,
        strategy_name=str(_loop_strategy_name or "factor_v4"),
        ledger=_LEDGER,
        evaluate_position=_evaluate_position_supervisor_for_position,
        record_aux_failure=_record_risk_reduction_aux_failure,
        log_trace=_log_supervisor_trace,
        make_candidate=safety_candidate,
        recently_applied=_supervisor_recently_applied,
        delegate_timeout_close=_delegate_timeout_supervisor_close,
        build_tighten_execution_plan=_lifecycle_build_supervisor_tighten_execution_plan,
        build_action_fingerprint=_lifecycle_build_supervisor_action_fingerprint,
        noop_fingerprint_seen=_supervisor_noop_fingerprint_seen,
        remember_noop=_remember_supervisor_noop,
        risk_action_for_action=_lifecycle_supervisor_risk_action_for_action,
        build_risk_evaluation_inputs=(
            _lifecycle_build_supervisor_runtime_risk_evaluation_inputs
        ),
        supervisor_risk_context=_supervisor_risk_context,
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
        floor_api_volume_to_step=_floor_api_volume_to_step,
        should_full_close_untradeable_reduce=_should_full_close_untradeable_reduce,
        build_close_position_risk_context=_build_close_position_risk_context,
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
    )
    return _runtime_run_position_supervision(
        bridge,
        pos,
        cfg=cfg,
        account=acct,
        tick=tick,
        log=log,
        runtime=runtime,
        skip_position_ids=skip_position_ids,
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
_ACCOUNT_REFRESH_LOCK = threading.Lock()
# Legacy/off-mode keeps a compatibility refresh worker.  Its cadence must
# still satisfy the public 15-second account/position fact contract: a 5s
# worker poll with a 10s minimum RPC interval gives room for scheduler jitter
# without pretending event-cache updates are reconciliations.
# The worker wakes every five seconds.  A ten-second minimum lets scheduling
# phase plus broker RPC latency push the recorded account age beyond the
# 15-second live safety contract on alternating full cycles.  Refreshing once
# per worker wake leaves explicit latency headroom while positions remain
# authoritative through the serial safety reconcile.
_ACCOUNT_REFRESH_MIN_INTERVAL = 5.0
_POSITION_RECONCILE_MIN_INTERVAL = 10.0
_DATA_SYNC_LOCK = threading.Lock()


def _live_state_get(key: str, default=None, *, clone: bool = False):
    return _runtime_state_get(_live_state, _LIVE_STATE_LOCK, key, default, clone=clone)


def _live_state_set(key: str, value) -> None:
    _runtime_state_set(_live_state, _LIVE_STATE_LOCK, key, value)


def _live_state_update(**kwargs) -> None:
    _runtime_state_update(_live_state, _LIVE_STATE_LOCK, **kwargs)


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
    setup and ``SELECT 1`` must fail before the 15-second safety SLO.  Other
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


def _ensure_runtime_kv_schema(conn) -> None:
    from backend.core.db import STATE_DB_DDL

    if _state_conn_is_pg(conn):
        return
    conn.executescript(STATE_DB_DDL)


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
        _ensure_runtime_kv_schema(conn)
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


def _lookup_entry_decision_id(position_id: int) -> str:
    conn = _get_state_read_conn()
    try:
        row = _state_execute(
            conn,
            """
            SELECT decision_id FROM decision_ledger
            WHERE position_id=? AND event_type='open'
            ORDER BY decision_ts DESC LIMIT 1
            """,
            (str(position_id),),
        ).fetchone()
        return str(row["decision_id"]) if row and row["decision_id"] else ""
    finally:
        conn.close()


def _lookup_open_decision_context(position_id: int) -> dict:
    conn = _get_state_read_conn()
    try:
        row = _state_execute(
            conn,
            """
            SELECT decision_ts, timeframe FROM decision_ledger
            WHERE position_id=? AND event_type='open'
            ORDER BY decision_ts DESC LIMIT 1
            """,
            (str(position_id),),
        ).fetchone()
        if row:
            return {
                "entry_ts": float(row["decision_ts"] or 0.0),
                "timeframe": str(row["timeframe"] or ""),
                "source": "decision_ledger",
            }
        recovery = _state_execute(
            conn,
            """
            SELECT first_seen_at FROM recovery_position_state
            WHERE position_id=?
            ORDER BY first_seen_at DESC LIMIT 1
            """,
            (int(position_id),),
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
    """Create minimal open evidence for recovered legacy positions before close review."""
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
            (position_id,),
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
        fallback_strategy_name=_loop_strategy_name or "factor_v4",
        context_integrity_default=_RECOVERY_CONTEXT_PARTIAL,
        fallback_now_ts=time.time(),
    )

    try:
        decision_id = _LEDGER.log_decision(**payloads["decision_payload"])
        _LEDGER.log_position_event(**payloads["position_event_payload"])
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
    conn = _get_state_read_conn()
    try:
        row = _state_execute(
            conn,
            "SELECT context_integrity FROM recovery_position_state WHERE position_id=?",
            (position_id,),
        ).fetchone()
        return str(row["context_integrity"] or default) if row else default
    finally:
        conn.close()


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
    persisted_state: dict | None = None,
) -> dict:
    """Project fresh broker account/deal facts into the live risk session.

    ``persisted_state`` remains in the compatibility signature for one release
    but is intentionally ignored: cache-derived peak/equity history must not
    contaminate an authoritative reconstruction.
    """
    del persisted_state
    account = _live_state_get("account", {}, clone=True) or {}
    current_balance = float(account.get("balance", 0.0) or 0.0)
    start_balance = _derive_session_start_balance(
        current_balance=current_balance,
        completed_position_trades=trades,
        realized_close_legs=realized_close_legs,
    )
    limits = RiskLimitSnapshot.from_runtime_config()
    projection = _rebuild_session_risk_projection(
        trade_date=trade_date,
        completed_position_trades=trades,
        session_start_balance=start_balance,
        max_consecutive_losses=int(limits.max_consecutive_losses),
        max_daily_loss_pct=float(limits.max_daily_loss_pct),
        realized_close_legs=realized_close_legs,
    )
    return {
        **projection,
        "session_state_source": "ctrader_deals.final_close_rebuild.v1",
        "session_recorded_position_ids": sorted(
            {
                int(item.get("position_id") or 0)
                for item in trades
                if int(item.get("position_id") or 0) > 0
            }
        ),
    }


def _restore_session_state_for_day(
    trade_date: str | None = None,
    *,
    broker_open_position_ids: set[int] | None = None,
    confirmed_closed_position_ids: set[int] | None = None,
) -> bool:
    if not trade_date:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_state = _runtime_kv_get(_session_state_key(trade_date), {}) or {}
    state = _parse_degraded_session_cache(
        raw_state,
        trade_date=trade_date,
    )
    authoritative_facts = _load_authoritative_session_deal_facts(
        trade_date,
        broker_open_position_ids=broker_open_position_ids,
        confirmed_closed_position_ids=confirmed_closed_position_ids,
    )
    if authoritative_facts is not None:
        authoritative_trades = list(
            authoritative_facts.get("completed_position_trades") or []
        )
        realized_close_legs = list(
            authoritative_facts.get("realized_close_legs") or []
        )
        try:
            restored = _build_session_state_from_authoritative_trades(
                trade_date=trade_date,
                trades=authoritative_trades,
                realized_close_legs=realized_close_legs,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            # Completed deals without a usable fresh account balance are only
            # partial broker facts.  Never borrow a baseline from runtime_kv
            # and call the resulting risk state authoritative.
            logger.warning(
                "[live] authoritative session projection unavailable for %s: %s",
                trade_date,
                exc,
            )
        else:
            restored["session_state_status"] = "available"
            restored["session_observed_at"] = time.time()
            restored["session_risk_blockers"] = []
            _live_state_update(**restored)
            # Heal stale runtime_kv snapshots so the next restart sees the same
            # broker-derived session state without another stale carry-over.
            _persist_session_state(trade_date)
            _evaluate_daily_drawdown()
            return True

    if not state:
        # Do not manufacture a zero-risk day when neither PostgreSQL facts nor
        # a same-day cache can be established.  The generation startup/runtime
        # gate observes this explicit unavailable state and blocks new risk.
        _live_state_update(
            session_state_status="unavailable",
            session_state_source="unavailable",
            accepting_new_risk=False,
        )
        return False

    # Compatibility cache is protection-only.  It may keep a prior circuit
    # and statistics visible, but it is never sufficient to authorize alpha.
    logger.warning(
        "[live] restoring legacy session snapshot for %s because broker close facts are unavailable",
        trade_date,
    )
    _live_state_update(
        **state,
        session_state_source="runtime_legacy_snapshot",
        session_state_status="degraded_cache",
        accepting_new_risk=False,
    )
    return True


def _retry_legacy_session_restore(
    *,
    broker: str,
    strategy_name: str,
    trade_date: str,
    log,
) -> bool:
    """Retry delayed close deals before legacy-loop session admission.

    Phase 2 already runs recovery after its broker/account/safety snapshot.
    The compatibility loop needs the same fail-closed retry or a delayed
    final/partial deal would leave its durable cause latched forever.  A fresh
    account reconcile is required because realized legs change the balance
    used to derive the UTC-day opening balance.
    """

    if str(broker or "") != "ctrader":
        _live_state_update(accepting_new_risk=False)
        return False
    bridge, error, warming = _get_ctrader()
    if (
        error
        or bridge is None
        or warming
        or not bool(getattr(bridge, "is_connected", False))
    ):
        _live_state_update(accepting_new_risk=False)
        log(
            "legacy session recovery waiting for broker: "
            f"{error or 'ctrader_not_ready'}"
        )
        return False
    try:
        if not _bootstrap_position_recovery(
            bridge,
            broker="ctrader",
            strategy_name=str(strategy_name or "factor_v4"),
            log=log,
        ):
            _live_state_update(accepting_new_risk=False)
            return False
    except Exception as exc:
        _live_state_update(accepting_new_risk=False)
        log(
            "legacy session close-deal recovery failed closed: "
            f"{type(exc).__name__}: {exc}"
        )
        return False

    account_reconcile = _explicit_account_reconcile(bridge)
    account_value = (
        _reconcile_value(account_reconcile, "account", None)
        if account_reconcile is not None
        else None
    )
    if account_value is None:
        _mark_account_reconcile_failed("legacy_session_account_reconcile_failed")
        _live_state_update(accepting_new_risk=False)
        return False
    try:
        account_payload = (
            asdict(account_value)
            if is_dataclass(account_value)
            else dict(account_value)
        )
    except (TypeError, ValueError):
        _mark_account_reconcile_failed("legacy_session_account_payload_invalid")
        _live_state_update(accepting_new_risk=False)
        return False
    account_payload.update({"ok": True, "broker": "ctrader"})
    _live_state_update(
        account=account_payload,
        account_reconciled=copy.deepcopy(account_payload),
        account_updated_at=float(
            _reconcile_value(account_reconcile, "observed_at", 0.0) or 0.0
        ),
        account_reconcile_id=str(
            _reconcile_value(account_reconcile, "reconcile_id", "") or ""
        ),
        account_reconcile_failed_at=None,
        account_reconcile_error=None,
    )
    open_position_ids = _fresh_cached_broker_open_position_ids()
    if open_position_ids is None:
        _live_state_update(accepting_new_risk=False)
        return False
    restored = _restore_session_state_for_day(
        trade_date,
        broker_open_position_ids=open_position_ids,
    )
    if not restored or str(
        _live_state_get("session_state_status", "unknown") or "unknown"
    ) != "available":
        _live_state_update(accepting_new_risk=False)
        return False
    return True


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
        if (
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
    stale_after_sec: float = 15.0,
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
        row = _state_execute(
            conn,
            """
            SELECT decision_id, event_type, action_reason, action_json, risk_state_json, decision_ts
            FROM decision_ledger
            WHERE position_id=?
              AND (
                  event_type LIKE 'supervisor_%'
                  OR event_type IN ('legacy_awe_trailing', 'holding_timeout')
              )
              AND decision_ts <= ?
              AND decision_ts >= ?
            ORDER BY decision_ts DESC
            LIMIT 1
            """,
            (str(position_id), float(close_ts or time.time()), float(close_ts or time.time()) - max(1.0, lookback_sec)),
        ).fetchone()
        return _lifecycle_normalize_supervisor_event_row(row, close_ts=close_ts)
    finally:
        conn.close()


def _latest_protection_trace_before_close(position_id: int, close_ts: float, lookback_sec: float = 3600.0) -> dict[str, Any]:
    conn = _get_state_read_conn()
    try:
        try:
            row = _state_execute(
                conn,
                """
                SELECT trace_id, decision_id, action, summary_reason, event_ts,
                       verdict_json, risk_verdict_json, execution_json, stage, outcome
                FROM position_supervisor_trace
                WHERE position_id=?
                  AND event_ts <= ?
                  AND event_ts >= ?
                  AND action IN ('tighten', 'reduce', 'close')
                ORDER BY event_ts DESC
                LIMIT 1
                """,
                (str(position_id), float(close_ts or time.time()), float(close_ts or time.time()) - max(1.0, lookback_sec)),
            ).fetchone()
        except Exception:
            row = None
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
    snapshot = _normalize_position_snapshot(raw_position)
    position_id = snapshot["position_id"]
    if position_id <= 0:
        return
    now = time.time()
    entry_decision_id = _lookup_entry_decision_id(position_id)
    desired_integrity = context_integrity or (_RECOVERY_CONTEXT_FULL if entry_decision_id else _RECOVERY_CONTEXT_PARTIAL)
    conn = _get_state_pg_conn()
    try:
        prev = _state_execute(
            conn,
            "SELECT * FROM recovery_position_state WHERE position_id=?",
            (position_id,),
        ).fetchone()
        first_seen_at = float(prev["first_seen_at"]) if prev else now
        stored_meta = {}
        if prev and prev["recovery_meta_json"]:
            try:
                stored_meta = json.loads(prev["recovery_meta_json"])
            except Exception:
                stored_meta = {}
        next_meta = dict(stored_meta)
        if meta:
            next_meta.update(meta)
        prev_integrity = str(prev["context_integrity"]) if prev and prev["context_integrity"] else ""
        if prev_integrity == _RECOVERY_CONTEXT_FULL:
            desired_integrity = _RECOVERY_CONTEXT_FULL
        _state_execute(
            conn,
            """
            INSERT INTO recovery_position_state
            (position_id, broker, symbol, direction, open_price, volume,
             first_seen_at, last_seen_at, status, strategy_name,
             entry_decision_id, context_integrity, recovery_meta_json,
             closed_at, close_reason, close_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, '', 0.0)
            ON CONFLICT(position_id) DO UPDATE SET
                broker=excluded.broker,
                symbol=excluded.symbol,
                direction=excluded.direction,
                open_price=excluded.open_price,
                volume=CASE
                    WHEN excluded.volume > 0 THEN excluded.volume
                    WHEN recovery_position_state.volume > 0 THEN recovery_position_state.volume
                    ELSE excluded.volume
                END,
                last_seen_at=excluded.last_seen_at,
                status=excluded.status,
                strategy_name=excluded.strategy_name,
                entry_decision_id=CASE
                    WHEN recovery_position_state.entry_decision_id='' THEN excluded.entry_decision_id
                    ELSE recovery_position_state.entry_decision_id
                END,
                context_integrity=CASE
                    WHEN recovery_position_state.context_integrity='full' THEN 'full'
                    ELSE excluded.context_integrity
                END,
                recovery_meta_json=excluded.recovery_meta_json,
                closed_at=CASE
                    WHEN excluded.status IN ('open', 'recovered') THEN 0.0
                    ELSE recovery_position_state.closed_at
                END,
                close_reason=CASE
                    WHEN excluded.status IN ('open', 'recovered') THEN ''
                    ELSE recovery_position_state.close_reason
                END,
                close_pnl=CASE
                    WHEN excluded.status IN ('open', 'recovered') THEN 0.0
                    ELSE recovery_position_state.close_pnl
                END
            """,
            (
                position_id,
                broker,
                snapshot["symbol"],
                snapshot["direction"],
                snapshot["open_price"],
                snapshot["volume"],
                first_seen_at,
                now,
                status,
                strategy_name,
                entry_decision_id,
                desired_integrity,
                json.dumps(next_meta, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _list_active_recovery_positions(broker: str) -> list[dict]:
    conn = _get_state_read_conn()
    try:
        rows = _state_execute(
            conn,
            """
            SELECT * FROM recovery_position_state
            WHERE broker=? AND status IN ('open', 'recovered')
            ORDER BY last_seen_at ASC
            """,
            (broker,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _recovery_last_seen_by_position(position_ids: set[int]) -> dict[int, float]:
    """Return the last broker-open observation used to reject stale partial deals."""

    normalized = sorted({int(pid) for pid in position_ids if int(pid or 0) > 0})
    if not normalized:
        return {}
    conn = _get_state_read_conn()
    try:
        result: dict[int, float] = {}
        for pid in normalized:
            row = _state_execute(
                conn,
                "SELECT last_seen_at FROM recovery_position_state WHERE position_id=?",
                (pid,),
            ).fetchone()
            if row and float(row["last_seen_at"] or 0.0) > 0.0:
                # Allow a small broker/local clock and serialization tolerance;
                # an older partial close is normally far earlier than this.
                result[pid] = max(0.0, float(row["last_seen_at"]) - 5.0)
        return result
    finally:
        conn.close()


def _recovery_remaining_volume_by_position(
    position_ids: set[int],
) -> dict[int, float]:
    """Return the last fresh broker-open volume for close completeness proof."""

    normalized = sorted({int(pid) for pid in position_ids if int(pid or 0) > 0})
    if not normalized:
        return {}
    conn = _get_state_read_conn()
    try:
        result: dict[int, float] = {}
        for pid in normalized:
            row = _state_execute(
                conn,
                "SELECT volume FROM recovery_position_state WHERE position_id=?",
                (pid,),
            ).fetchone()
            if row and float(row["volume"] or 0.0) > 0.0:
                result[pid] = float(row["volume"])
            elif float(_pos_open_api_volume.get(pid, 0.0) or 0.0) > 0.0:
                # Missing PG rows are still allowed to fail closed with the
                # process-local original volume.  A too-large fallback delays
                # release; it cannot create a false confirmed close.
                result[pid] = float(_pos_open_api_volume[pid])
        return result
    finally:
        conn.close()


def _mark_recovery_position_closed(
    position_id: int,
    *,
    close_reason: str,
    close_pnl: float,
    closed_at: float,
    meta: dict | None = None,
) -> None:
    conn = _get_state_pg_conn()
    try:
        row = _state_execute(
            conn,
            "SELECT recovery_meta_json FROM recovery_position_state WHERE position_id=?",
            (position_id,),
        ).fetchone()
        payload = _lifecycle_build_recovery_closed_update_payload(
            position_id=position_id,
            existing_meta_json=row["recovery_meta_json"] if row else "",
            close_reason=close_reason,
            close_pnl=close_pnl,
            closed_at=closed_at,
            meta=meta,
        )
        _state_execute(
            conn,
            """
            UPDATE recovery_position_state
            SET status='closed_replayed',
                closed_at=?,
                close_reason=?,
                close_pnl=?,
                recovery_meta_json=?
            WHERE position_id=?
            """,
            (
                payload["closed_at"],
                payload["close_reason"],
                payload["close_pnl"],
                json.dumps(payload["recovery_meta"], ensure_ascii=False, default=str),
                payload["position_id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _replay_recovered_close(
    *,
    broker: str,
    position_id: int,
    position_state: dict,
    real_pnl: dict | None,
    strategy_name: str,
) -> bool:
    if not _authoritative_close_pnl(real_pnl):
        _defer_close_until_authoritative_deal(
            int(position_id),
            broker=broker,
            tick=0,
            reason="restart_replay_close_deal_unavailable",
        )
        return False
    payloads = _lifecycle_build_replayed_close_payloads(
        position_id=position_id,
        position_state=position_state,
        real_pnl=real_pnl,
        strategy_name=strategy_name,
        now_ts=time.time(),
        context_integrity_default=_RECOVERY_CONTEXT_PARTIAL,
    )
    total_pnl = float(payloads["total_pnl"])
    close_price = float(payloads["close_price"])
    close_ts = float(payloads["close_ts"])
    context_integrity = str(payloads["context_integrity"])

    # Session risk is rebuilt exclusively from ctrader_deals.  Replaying
    # recovery/audit state must not increment the already deals-derived session
    # projection a second time.
    _mark_recovery_position_closed(
        position_id,
        close_reason="restart_replay",
        close_pnl=total_pnl,
        closed_at=close_ts,
        meta=payloads["recovery_meta"],
    )
    # Do not release the durable cursor before the recovery projection commits.
    # Otherwise a PG failure leaves an active row but loses the original
    # pre-fetch baseline needed to resolve the already-stored delayed deal.
    _release_session_close_deal_latch(int(position_id), real_pnl)

    exit_decision_id = ""
    if _LEDGER:
        try:
            decision_payload = dict(payloads["decision"])
            exit_decision_id = _LEDGER.log_decision(
                event_type=decision_payload["event_type"],
                symbol=decision_payload["symbol"],
                timeframe=decision_payload["timeframe"],
                trade_id=decision_payload["trade_id"],
                position_id=decision_payload["position_id"],
                decision_ts=decision_payload["decision_ts"],
                portfolio_state=decision_payload["portfolio_state"],
                risk_state=_live_state_get("risk", {}, clone=True) or {},
                action_score=decision_payload["action_score"],
                action_reason=decision_payload["action_reason"],
                action_json=decision_payload["action_json"],
            )
            _LEDGER.log_position_event(**payloads["position_event"])
        except Exception as exc:
            logger.debug("[live] replay close ledger failed for pos %s: %s", position_id, exc)

    if _TRADE_REVIEWER and _EXPERIENCE_BUILDER and _POLICY_SUGGESTER:
        try:
            review = _TRADE_REVIEWER.review_closed_trade(
                position_id=payloads["review"]["position_id"],
                pnl=payloads["review"]["pnl"],
                close_price=payloads["review"]["close_price"],
                close_ts=payloads["review"]["close_ts"],
                contributions=payloads["review"]["contributions"],
                exit_decision_id=exit_decision_id,
                real_pnl=payloads["review"]["real_pnl"],
                close_reason=payloads["review"]["close_reason"],
                context_integrity=payloads["review"]["context_integrity"],
            )
            if review.get("accepted", True):
                experience = _EXPERIENCE_BUILDER.build_from_review(review)
                _POLICY_SUGGESTER.suggest_from_experience(experience)
        except Exception as exc:
            logger.debug("[live] replay close learning failed for pos %s: %s", position_id, exc)
    return True


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
    log=None,
) -> bool:
    pid = int(position_id)
    try:
        live_positions = _read_positions_for_recovery(bridge)
    except Exception as exc:
        logger.debug("[live] missing-position confirm failed for pos %s: %s", pid, exc)
        return False
    live_ids = {
        int(item["position_id"])
        for item in (_normalize_position_snapshot(pos) for pos in live_positions)
        if int(item["position_id"]) > 0
    }
    if pid in live_ids:
        return False

    conn = _get_state_read_conn()
    try:
        row = _state_execute(
            conn,
            "SELECT * FROM recovery_position_state WHERE position_id=?",
            (pid,),
        ).fetchone()
        position_state = dict(row) if row else {
            "position_id": pid,
            "broker": broker,
            "symbol": "XAUUSD+",
            "open_price": float(_pos_open_prices.get(pid, 0.0) or 0.0),
            "close_pnl": 0.0,
            "context_integrity": _RECOVERY_CONTEXT_PARTIAL,
        }
    finally:
        conn.close()

    real_pnl = None
    try:
        from execution.deal_sync import sync_close_deals_batch

        write_conn = _get_state_pg_conn()
        try:
            from_ts = int(max(0.0, float(position_state.get("last_seen_at") or time.time()) - _RECOVERY_REPLAY_LOOKBACK_SEC))
            real_pnl = sync_close_deals_batch(
                bridge,
                write_conn,
                {pid},
                from_ts=from_ts,
                max_rows=200,
                min_exec_timestamp_by_position={
                    pid: max(
                        0.0,
                        float(position_state.get("last_seen_at") or 0.0) - 5.0,
                    )
                },
                required_closed_volume_delta_by_position={
                    pid: float(position_state.get("volume") or 0.0)
                },
            ).get(pid)
        finally:
            write_conn.close()
    except Exception as exc:
        logger.debug("[live] missing-position deal sync failed for pos %s: %s", pid, exc)

    if not _authoritative_close_pnl(real_pnl):
        _defer_close_until_authoritative_deal(
            pid,
            broker=broker,
            tick=0,
            reason="broker_position_missing_close_deal_unavailable",
        )
        if log:
            log(
                f"broker missing position pending authoritative close deal "
                f"pos={pid}: {reason}"
            )
        return False

    replayed = _replay_recovered_close(
        broker=broker,
        position_id=pid,
        position_state=position_state,
        real_pnl=real_pnl,
        strategy_name=strategy_name,
    )
    if not replayed:
        return False
    _mark_recovery_position_closed(
        pid,
        close_reason="broker_position_not_found",
        close_pnl=float((real_pnl or {}).get("net", position_state.get("close_pnl", 0.0)) or 0.0),
        closed_at=float((real_pnl or {}).get("exec_timestamp", time.time()) or time.time()),
        meta={"broker_position_not_found": True, "failure_reason": reason, "retired_at": time.time()},
    )
    _remove_live_position_state(pid)
    if log:
        log(f"broker missing position retired pos={pid}: {reason}")
    return True


def _read_positions_for_recovery(bridge) -> list[Any]:
    result = _explicit_position_reconcile(bridge)
    if str(_reconcile_value(result, "status", "failed") or "failed") != "fresh":
        raise RuntimeError(
            str(_reconcile_value(result, "error_code", "") or "fresh broker reconcile unavailable")
        )
    return list(_publish_fresh_position_reconcile(result, broker="ctrader"))


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
    tripped = session_pnl < 0 and dd_pct >= limits.max_daily_loss_pct
    reason = f"daily drawdown {dd_pct:.1f}%" if tripped else ""
    if tripped:
        updates["circuit_breaker"] = True
        updates["circuit_reason"] = reason
    _live_state_update(**updates)
    if updates:
        _persist_session_state()
    return {
        "tripped": tripped,
        "dd_pct": dd_pct,
        "reason": reason,
        "session_pnl": session_pnl,
        "start_balance": start_balance,
        "risk_limits": limits.to_dict(),
    }


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


def _append_trade_equity(equity: float) -> list[float]:
    with _LIVE_STATE_LOCK:
        history = list(_live_state.get("trade_equity_history", []))
        history.append(float(equity))
        if len(history) > 1000:
            history = history[-500:]
        _live_state["trade_equity_history"] = history
    _persist_session_state()
    return list(history)


def _set_risk_metric(name: str, value: dict) -> None:
    with _LIVE_STATE_LOCK:
        risk_state = dict(_live_state.get("risk", {}))
        risk_state[name] = value
        _live_state["risk"] = risk_state


def _get_risk_state() -> dict:
    return _live_state_get("risk", {}, clone=True) or {}


def _set_factor_snapshot(votes: dict, composite: dict) -> None:
    _live_state_update(last_factor_votes=votes, last_composite=composite)


def _set_loop_diagnostic(tick: int, bridge_status: str, *, bridge_ready: bool | None = None) -> None:
    previous = _live_state_get("_diag", {}, clone=True) or {}
    snapshot = {
        "tick": tick,
        "ts": time.time(),
        "bridge": bridge_status,
        "last_error": previous.get("last_error", ""),
    }
    if bridge_ready is not None:
        snapshot["bridge_ready"] = bridge_ready
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
    execution_v2_enabled = bool(
        _phase2_feature_flags().ctrader_execution_outcome_v2_enabled
    )
    if not account_observed:
        account_payload.update(ok=False, warming_up=True)
    _live_state_update(
        broker=broker,
        loop_running=True,
        loop_strategy=strategy_name,
        loop_started_at=started_at,
        loop_shutdown=None,
        # Every generation starts fail-closed.  Legacy and v2 loops may reopen
        # only after their explicit broker/session gates complete.
        accepting_new_risk=False,
        account_event=account_payload,
        account_event_updated_at=(time.time() if account_observed else None),
        account_event_reason=(
            "startup_projection" if account_observed else "startup_warming"
        ),
        execution_recovery={
            "schema": "broker_execution_intent_recovery.v1",
            "enabled": execution_v2_enabled,
            "ready": not execution_v2_enabled,
            "unresolved_count": None if execution_v2_enabled else 0,
            "status": (
                "pending" if execution_v2_enabled else "disabled"
            ),
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
                        "source": "spot",
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
                    strategy_name=str(_loop_strategy_name or "factor_v4"),
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
_ctrader_bridge = None  # type: "CTraderBridge | None"
_ctrader_lock = _CTRADER_RUNTIME.lock
_ctrader_connect_thread: threading.Thread | None = None
_ctrader_last_error: str | None = None
_ctrader_next_retry_at: float = 0.0
_ctrader_guard_handle = None


def _sync_ctrader_runtime_from_legacy() -> None:
    """Keep old module globals as a facade over CTraderRuntime state."""
    global _ctrader_bridge, _ctrader_connect_thread, _ctrader_last_error, _ctrader_next_retry_at, _ctrader_guard_handle
    if _ctrader_bridge is not None and _ctrader_bridge is not _CTRADER_RUNTIME.bridge:
        _CTRADER_RUNTIME.bridge = _ctrader_bridge
    if _ctrader_connect_thread is not None and _ctrader_connect_thread is not _CTRADER_RUNTIME.connect_thread:
        _CTRADER_RUNTIME.connect_thread = _ctrader_connect_thread
    if _ctrader_guard_handle is not None and _ctrader_guard_handle is not _CTRADER_RUNTIME.guard_handle:
        _CTRADER_RUNTIME.guard_handle = _ctrader_guard_handle
    _ctrader_bridge = _CTRADER_RUNTIME.bridge
    _ctrader_connect_thread = _CTRADER_RUNTIME.connect_thread
    _ctrader_last_error = _CTRADER_RUNTIME.last_error
    _ctrader_next_retry_at = _CTRADER_RUNTIME.next_retry_at
    _ctrader_guard_handle = _CTRADER_RUNTIME.guard_handle


def _ensure_ctrader_process_guard() -> str | None:
    _sync_ctrader_runtime_from_legacy()
    err = _CTRADER_RUNTIME.ensure_process_guard()
    _sync_ctrader_runtime_from_legacy()
    return err


def _ctrader_retry_remaining() -> float:
    _sync_ctrader_runtime_from_legacy()
    return _CTRADER_RUNTIME.retry_remaining()


def _kickoff_ctrader_connect():
    """在后台线程跑 _ctrader_bridge.connect(). 不会阻塞调用方.
    必须已持有 _ctrader_lock 锁. 假定 _ctrader_bridge 已实例化."""
    _sync_ctrader_runtime_from_legacy()
    thread = _CTRADER_RUNTIME.kickoff_connect(logger=logger)
    _sync_ctrader_runtime_from_legacy()
    return thread


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

    _sync_ctrader_runtime_from_legacy()
    result = _CTRADER_RUNTIME.get_or_start(
        make_bridge=_make_ctrader_bridge,
        should_send_orders=_should_send_orders,
        apply_runtime_config=_apply_ctrader_runtime_config,
        logger=logger,
    )
    _sync_ctrader_runtime_from_legacy()
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
        from data.live_sync.health import SyncHealth

        bar_ts_by_tf = dict((SyncHealth.shared().record.last_bar_ts_by_tf or {}))
        latest_market_data_ts = float(bar_ts_by_tf.get("M1") or bar_ts_by_tf.get("M5") or 0.0)
    except Exception:
        latest_market_data_ts = 0.0
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
    market_session: dict[str, Any] | None = None,
) -> None:
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
    if not spot_needed:
        return
    try:
        from backend.services.market_session import maintenance_wait_evidence
        from config.runtime_config import shared as _runtime_cfg

        session = dict(market_session or _market_session_snapshot(bridge) or {})
        maintenance = maintenance_wait_evidence(
            session,
            latest_market_data_ts=float((quote or {}).get("ts") or 0.0),
            now_ts=now_ts,
            grace_seconds=float(_runtime_cfg().market_open_pending_quote_grace_seconds),
        )
        if maintenance["active"]:
            return
    except Exception:
        logger.debug("[market_session] spot subscription maintenance check failed", exc_info=True)
    if now_ts - _last_spot_subscription_attempt_ts < 60:
        return
    _last_spot_subscription_attempt_ts = now_ts
    try:
        if spot_needed and hasattr(bridge, "subscribe_spots"):
            bridge.subscribe_spots()
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
    return list(pos_list or [])


def get_live_readiness(broker: str = "ctrader") -> dict:
    state = {
        "diag": _live_state_get("_diag", {}, clone=True) or {},
        "account_reconciled": (
            _live_state_get("account_reconciled", {}, clone=True) or {}
        ),
        "account_updated_at": _live_state_get("account_updated_at", 0.0),
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
        v2_active=_phase2_v2_active(),
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
            account=_live_state_get("account_reconciled", {}, clone=True) or {},
        )

    def _visible_positions(pos_list: list[Any]) -> list[dict]:
        visible = _enrich_positions(pos_list)
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

# Module-level state for the loop (singleton, persists across requests)
# ── 模块级状态 ──────────────────────────────────────────
_loop_thread: threading.Thread | None = None
_loop_stop_flag: threading.Event = None  # type: ignore[assignment]
_loop_broker: str | None = None
_loop_started_at: float | None = None
_loop_strategy_name: str | None = "factor_pipeline_v4"
_loop_state_lock = threading.Lock()
_OPEN_TRADE_ADMISSION_LOCK = threading.Lock()
_process_shutdown_requested = False
_LIVE_LOOP_CONTROLLER = LiveLoopController()
_live_safety_plane: LiveSafetyPlane | None = None
_live_safety_plane_owner: str = ""
_live_safety_watchdog: LiveSafetyWatchdog | None = None
# ★ v9-fix: 重启退避 + 价格僵死检测 + 备份 bar 缓存
_last_loop_end: float = 0.0
_MIN_RESTART_INTERVAL = 60  # 最小重启间隔 60s
_BAR_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / ".bar_cache.pkl"
_PRICE_STUCK_WARNED: dict[str, float] = {}  # {(broker,tf): last_price}


def _phase2_feature_flags():
    return shared_static_feature_flags()


def _generation_controller_enabled() -> bool:
    return bool(_phase2_feature_flags().live_generation_controller_v2_enabled)


def _phase2_v2_active() -> bool:
    flags = _phase2_feature_flags()
    return bool(
        _generation_controller_enabled()
        or str(flags.live_safety_plane_v2_mode) != "off"
        # Execution-outcome recovery is itself a startup/new-risk barrier.  It
        # must not silently fall back to the legacy alpha-first loop merely
        # because generation ownership and Safety v2 are still off.
        or bool(flags.ctrader_execution_outcome_v2_enabled)
    )


def _current_generation_id() -> str:
    current = _LIVE_LOOP_CONTROLLER.current()
    return str(current.generation_id) if current is not None else ""


def _get_live_safety_plane(generation_id: str = "") -> LiveSafetyPlane:
    global _live_safety_plane, _live_safety_plane_owner
    owner = str(generation_id or "legacy")
    mode = str(_phase2_feature_flags().live_safety_plane_v2_mode)
    if (
        _live_safety_plane is None
        or _live_safety_plane_owner != owner
        or _live_safety_plane.mode != mode
    ):
        _live_safety_plane = LiveSafetyPlane(mode=mode)
        _live_safety_plane_owner = owner
    if mode == "enforce" and not _live_safety_plane.forced_shadow:
        # Re-read the additive authority cause even for an existing plane so
        # another process/generation cannot leave this loop enforcing V2 after
        # a shared comparison failure has already forced the deployment back
        # to legacy authority.
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

    thread_alive = bool(_loop_thread is not None and _loop_thread.is_alive())
    safety = _live_state_get("safety_plane", {}, clone=True) or {}
    heartbeat_at = float(safety.get("heartbeat_at", 0.0) or 0.0)
    if _generation_controller_enabled():
        controller = _LIVE_LOOP_CONTROLLER.status()
        heartbeat_at = float(controller.get("safety_heartbeat_at", 0.0) or 0.0)
    unknown_raw = safety.get("unknown_execution_count")
    return {
        # The feature flag selects the authoritative planner; it must never
        # disable the heartbeat invariant for a running legacy/off loop.
        "enabled": bool(thread_alive or _live_state_get("loop_running", False)),
        "running": thread_alive,
        "started_at": float(_loop_started_at or 0.0),
        "safety_heartbeat_at": heartbeat_at,
        "account_updated_at": float(_live_state_get("account_updated_at", 0.0) or 0.0),
        "positions_updated_at": float(_live_state_get("positions_updated_at", 0.0) or 0.0),
        "unknown_execution_count": unknown_raw,
    }


def _persist_safety_fail_closed(
    *,
    blockers: list[str] | tuple[str, ...],
    source: str,
    error: str = "",
) -> dict[str, Any]:
    """Durably block new risk without changing any broker action result."""

    normalized = sorted({str(item) for item in blockers if str(item)}) or [
        "safety_state_unavailable"
    ]
    latch = no_new_risk_latch_status(fail_closed=True)
    forced_shadow = "safety_v2_forced_shadow" in normalized
    persisted_forced_shadow = safety_v2_forced_shadow_status()
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
    if _generation_controller_enabled():
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
    """Release only the watchdog-owned cause after sustained fresh facts."""

    cause = ("safety_freshness", "safety_watchdog")
    latch = no_new_risk_latch_status(fail_closed=True)
    active_causes = {
        (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
        for item in list(latch.get("causes") or [])
        if isinstance(item, dict)
    }
    if cause not in active_causes:
        return
    released = release_no_new_risk_latch_cause(
        cause=cause[0],
        cause_id=cause[1],
        reason="safety_freshness_sustained_recovery",
        actor="system:safety_watchdog",
        evidence={
            "state": result.state,
            "ages": dict(result.ages),
            "blockers": list(result.blockers),
            "recovery_checks": 3,
        },
    )
    safety_failure = _live_state_get("safety_failure", {}, clone=True) or {}
    updates: dict[str, Any] = {"no_new_risk_latch": released}
    if str(safety_failure.get("source") or "") == "safety_watchdog":
        updates["safety_failure"] = {}
    _live_state_update(**updates)


def _start_live_safety_watchdog() -> bool:
    global _live_safety_watchdog
    if _live_safety_watchdog is None:
        _live_safety_watchdog = LiveSafetyWatchdog(
            probe=_live_safety_watchdog_probe,
            on_violation=_on_live_safety_watchdog_violation,
            on_recovery=_on_live_safety_watchdog_recovery,
            recovery_checks=3,
            interval_sec=5.0,
            stale_after_sec=15.0,
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
                    logger.info(
                        "[awe_adapt] weight update not applied run_id={} status={} admission_status={} admissions={}",
                        run_id,
                        status or "unknown",
                        result.get("admission_status") or "",
                        {name: item.get("status") for name, item in (result.get("admissions") or {}).items()},
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
    if not session:
        session = _market_session_snapshot(None)
    return run_offmarket_position_quality_job(session=session, db_path=db_path)



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

    if run_heavy_jobs:
        # ★ 初始化 EvolutionKernel (注册中枢 + quality gate + governor)
        from backend.runtime.evolution_kernel import EvolutionKernel

        kernel = EvolutionKernel.shared()
        kernel.set_pipeline(_factor_pipeline)
        kernel.start()  # registers evolution_hourly + factor governance + system_health
    else:
        try:
            from monitor.system_health import shared as _sh_shared
            from monitor.alerter import Alerter

            _sys_health = _sh_shared()
            _sys_health.set_alerter(Alerter({
                "log_file": "logs/alerts.log",
                "min_level": "WARNING",
            }).send)
            sched.add_job("system_health", "* * * * *", _sys_health.run)
            logger.info("[live] heavy scheduler jobs disabled; system_health remains in backend")
        except Exception as e:
            logger.warning("[live] system_health registration failed while heavy jobs disabled: {}", e)

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
    _register_external_sync_jobs(
        sched,
        repo_root=Path(__file__).resolve().parent.parent.parent,
        logger=logger,
    )
    if run_heavy_jobs:
        # evolution_hourly / factor governance / system_health 由 EvolutionKernel 注册;
        # awe_adapt 始终由持有 live pipeline 的 backend 注册。
        # Phase 3: 特征工程 (每天凌晨 3:00)
        sched.add_job("feature_eng", "0 3 * * *", _scheduled_feature_engineering)
        # Phase F1.1: 停盘确认窗口 LightGBM 旁路训练 (每小时检查, 非窗口只写 skip 审计)
        sched.add_job("offmarket_position_quality_lightgbm", "20 * * * *", _scheduled_offmarket_position_quality_lightgbm)
    else:
        logger.info("[live] heavy jobs delegated; set QUANT_BACKEND_HEAVY_JOBS=1 to run them in backend")
    # ★ system_health 已由 EvolutionKernel 注册
    sched.start()
    logger.info("[live] InProcessScheduler started; heavy_jobs={}", run_heavy_jobs)

    _initial_ctrader_data_pull = _make_initial_ctrader_data_pull(
        get_ctrader=_get_ctrader,
        logger=logger,
        default_timeframes=_BAR_TIMEFRAMES,
    )
    _start_initial_ctrader_data_pull(_initial_ctrader_data_pull)
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
    """Whether the live trading loop thread is running. 优先 _live_state 缓存."""
    with _loop_state_lock:
        legacy = _loop_status_snapshot(
            state_get=_live_state_get,
            thread=_loop_thread,
            broker=_loop_broker,
            started_at=_loop_started_at,
            strategy_name=_loop_strategy_name,
        )
        if _generation_controller_enabled():
            generation = _LIVE_LOOP_CONTROLLER.status()
        else:
            thread_alive = bool(_loop_thread is not None and _loop_thread.is_alive())
            draining = bool(
                thread_alive
                and _loop_stop_flag is not None
                and _loop_stop_flag.is_set()
            )
            safety = _live_state_get("safety_plane", {}, clone=True) or {}
            heartbeat_at = float(safety.get("heartbeat_at", 0.0) or 0.0)
            heartbeat_age = max(0.0, time.time() - heartbeat_at) if heartbeat_at > 0 else None
            heartbeat_healthy = bool(
                heartbeat_age is not None and heartbeat_age <= 15.0
            )
            generation = {
                "phase": (
                    "draining"
                    if draining
                    else
                    "degraded"
                    if thread_alive and not heartbeat_healthy
                    else "running"
                    if thread_alive
                    else "stopped"
                ),
                "generation": "",
                "thread_alive": thread_alive,
                "ready": bool(thread_alive and heartbeat_healthy and not draining),
                "accepting_new_risk": bool(
                    thread_alive
                    and heartbeat_healthy
                    and not draining
                    and _live_state_get("accepting_new_risk", True)
                    and not no_new_risk_latched(fail_closed=True)
                ),
                "safety_heartbeat_at": heartbeat_at or None,
                "alpha_heartbeat_at": None,
                "safety_heartbeat_age_sec": heartbeat_age,
                "alpha_heartbeat_age_sec": None,
                "startup_barrier": {},
                "blockers": sorted(
                    (
                        ["live_loop_draining"]
                        if draining
                        else []
                    )
                    + (
                        []
                        if heartbeat_healthy
                        else [
                            "safety_heartbeat_unknown"
                            if heartbeat_age is None
                            else "safety_heartbeat_stale"
                        ]
                    )
                ),
                "components": {},
            }
        freshness = evaluate_safety_freshness(
            _live_safety_watchdog_probe(),
            now=time.time(),
            stale_after_sec=15.0,
        )
        local_blockers: list[str] = []
        if freshness.enabled and freshness.running and not freshness.ok:
            local_blockers.extend(freshness.blockers)
        if no_new_risk_latched(fail_closed=True):
            local_blockers.append("no_new_risk_latched")
        if bool(generation.get("thread_alive")):
            reconcile_blockers = _new_risk_reconciliation_blockers()
            _live_state_update(new_risk_reconcile_blockers=reconcile_blockers)
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
        if local_blockers:
            generation = {
                **generation,
                "phase": (
                    "degraded"
                    if generation.get("phase") == "running"
                    else generation.get("phase")
                ),
                "accepting_new_risk": False,
                "blockers": sorted(
                    set(generation.get("blockers") or ()) | set(local_blockers)
                ),
            }
        return {
            **legacy,
            **generation,
            # Compatibility field remains available during the v2 rollout.
            "running": bool(generation["thread_alive"] and generation["phase"] != "stopped"),
            "safety": _live_state_get("safety_plane", {}, clone=True) or {},
            "safety_authority": (
                "phase2_serial_safety_plane"
                if _phase2_v2_active()
                else "legacy_authoritative"
            ),
            "safety_heartbeat_state": freshness.state,
            "safety_freshness": freshness.to_dict(),
            "safety_shadow_gate": safety_shadow_gate_status(),
        }


def _prepare_loop_ownership(
    *,
    stop_flag,
    broker: str,
    started_at: float,
    strategy_name: str,
) -> None:
    global _loop_stop_flag, _loop_broker, _loop_started_at, _loop_strategy_name

    _loop_stop_flag = stop_flag
    _loop_broker = broker
    _loop_started_at = started_at
    _loop_strategy_name = strategy_name


def _reset_start_ownership() -> None:
    global _loop_thread, _loop_stop_flag, _loop_broker, _loop_started_at
    global _loop_strategy_name

    _loop_thread = None
    _loop_stop_flag = None
    _loop_broker = None
    _loop_started_at = None
    _loop_strategy_name = None


def _install_loop_thread(thread) -> None:
    global _loop_thread

    _loop_thread = thread


def _live_loop_start_runtime() -> LiveLoopStartRuntime:
    return LiveLoopStartRuntime(
        generation_controller_enabled=_generation_controller_enabled,
        state_lock=_loop_state_lock,
        snapshot_ownership=_loop_ownership_snapshot,
        process_shutdown_requested=lambda: _process_shutdown_requested,
        controller=_LIVE_LOOP_CONTROLLER,
        last_loop_end=lambda: _last_loop_end,
        now=time.time,
        sleep=time.sleep,
        logger_warning=logger.warning,
        logger_info=logger.info,
        event_factory=threading.Event,
        prepare_ownership=_prepare_loop_ownership,
        reset_start_ownership=_reset_start_ownership,
        persist_desired_state=_persist_loop_desired_state,
        prime_live_loop_state=_prime_live_loop_state,
        phase2_active=_phase2_v2_active,
        start_safety_watchdog=_start_live_safety_watchdog,
        start_scheduler=_start_live_scheduler,
        stop_scheduler=_stop_live_scheduler,
        stop_safety_watchdog=_stop_live_safety_watchdog,
        thread_factory=threading.Thread,
        loop_target=_run_loop,
        install_loop_thread=_install_loop_thread,
        live_state_update=_live_state_update,
        live_state_get=_live_state_get,
        no_new_risk_latched=no_new_risk_latched,
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
    global _loop_thread, _loop_stop_flag, _loop_broker, _loop_started_at
    global _loop_strategy_name, _last_loop_end
    global _process_shutdown_requested

    requested_at = time.time()
    timeout = max(0.0, float(timeout_sec))
    trigger_reason = "backend_shutdown"

    with _loop_state_lock:
        thread = _loop_thread
        broker = _loop_broker
        thread_id = getattr(thread, "ident", None) if thread is not None else None
        if thread is None or not thread.is_alive():
            with _OPEN_TRADE_ADMISSION_LOCK:
                _process_shutdown_requested = True
            ownership_released = _loop_thread is thread
            if ownership_released:
                _loop_thread = None
                _loop_stop_flag = None
                _loop_broker = None
                _loop_started_at = None
                _loop_strategy_name = None
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
            stop_flag = _loop_stop_flag
            if _generation_controller_enabled():
                generation = _LIVE_LOOP_CONTROLLER.current()
                if generation is not None:
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
        ownership_released = _loop_thread is thread
        if ownership_released:
            _loop_thread = None
            _loop_stop_flag = None
            _loop_broker = None
            _loop_started_at = None
            _loop_strategy_name = None
            _last_loop_end = finished_at
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


def _loop_ownership_snapshot() -> LoopOwnershipSnapshot:
    return LoopOwnershipSnapshot(
        thread=_loop_thread,
        stop_flag=_loop_stop_flag,
        broker=_loop_broker,
        started_at=_loop_started_at,
        strategy_name=_loop_strategy_name,
    )


def _clear_loop_ownership_if(thread, finished_at: float) -> bool:
    global _loop_thread, _loop_stop_flag, _loop_broker, _loop_started_at
    global _loop_strategy_name, _last_loop_end

    if _loop_thread is not thread:
        return False
    _loop_thread = None
    _loop_stop_flag = None
    _loop_broker = None
    _loop_started_at = None
    _loop_strategy_name = None
    _last_loop_end = float(finished_at)
    _mark_loop_stopped_for_display()
    return True


def _ensure_loop_stop_event():
    global _loop_stop_flag

    if _loop_stop_flag is None:
        _loop_stop_flag = threading.Event()
    return _loop_stop_flag


def _live_loop_stop_runtime() -> LiveLoopStopRuntime:
    return LiveLoopStopRuntime(
        generation_controller_enabled=_generation_controller_enabled,
        state_lock=_loop_state_lock,
        snapshot_ownership=_loop_ownership_snapshot,
        clear_ownership_if=_clear_loop_ownership_if,
        ensure_stop_event=_ensure_loop_stop_event,
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
    from backend.core.db import DUCKDB_BARS, duckdb_readonly_connection
    db_path = str(DUCKDB_BARS)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with duckdb_readonly_connection(db_path, snapshot_first=True) as conn:
                df = conn.execute(
                    "SELECT time, open, high, low, close, volume "
                    "FROM bars WHERE symbol=? AND timeframe=? "
                    "ORDER BY time DESC LIMIT ?",
                    [symbol, timeframe, n_bars]
                ).df()
            if df is None or len(df) == 0:
                logger.warning(f"DuckDB has no bars for {symbol} {timeframe}")
                return None
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

    audit 2026-06-08: Pepperstone demo broker 不返 history bar. 这个函数主要
    是 best-effort 取"最近几根"用作 sanity check. 真正预热走 _warmup_from_local_db.
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


def _record_repaired_bar_sync_health(*, timeframe: str, latest_ts: float) -> None:
    if latest_ts <= 0:
        return
    try:
        from data.live_sync.health import SyncHealth

        SyncHealth.shared().record_success(last_bar_ts_by_tf={str(timeframe or "M5"): float(latest_ts)})
    except Exception:
        logger.debug("[live] decision bar repair health update failed", exc_info=True)


def _repair_live_decision_bars(
    *,
    bridge: Any,
    symbol: str,
    timeframe: str,
    expected_closed_bar_ts: float,
    tick: int,
    log,
) -> dict[str, Any]:
    result = {
        "attempted": False,
        "status": "not_attempted",
        "inserted_bars": 0,
        "latest_repaired_bar_ts": 0.0,
        "error": "",
    }
    if bridge is None or not bool(getattr(bridge, "is_connected", False)):
        result["status"] = "bridge_unavailable"
        return result
    if not hasattr(bridge, "fetch_bars"):
        result["status"] = "bridge_fetch_bars_unavailable"
        return result

    result["attempted"] = True
    try:
        fetched = bridge.fetch_bars(timeframe=timeframe, n_bars=200)
    except TypeError:
        try:
            fetched = bridge.fetch_bars(timeframe, 200)
        except Exception as exc:
            result["status"] = "fetch_failed"
            result["error"] = f"{type(exc).__name__}: {str(exc)[:220]}"
            return result
    except Exception as exc:
        result["status"] = "fetch_failed"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:220]}"
        return result

    if fetched is None or len(fetched) == 0:
        result["status"] = "fetch_empty"
        return result

    if expected_closed_bar_ts > 0:
        try:
            fetched = fetched.loc[
                [
                    (float(idx.timestamp()) if hasattr(idx, "timestamp") else float(idx))
                    <= float(expected_closed_bar_ts)
                    for idx in fetched.index
                ]
            ]
        except Exception:
            pass
    bars = _sync_dataframe_to_store_bars(fetched)
    if not bars:
        result["status"] = "no_closed_bars"
        return result

    try:
        from data.store import DataStore

        DataStore().insert_bars(bars, symbol, timeframe)
    except Exception as exc:
        result["status"] = "insert_failed"
        result["error"] = f"{type(exc).__name__}: {str(exc)[:220]}"
        return result

    latest_ts = float(bars[-1].get("time") or 0.0)
    _record_repaired_bar_sync_health(timeframe=timeframe, latest_ts=latest_ts)
    result.update(
        {
            "status": "inserted",
            "inserted_bars": len(bars),
            "latest_repaired_bar_ts": latest_ts,
        }
    )
    try:
        log(
            f"tick {tick}: repaired stale decision bars {symbol} {timeframe} "
            f"inserted={len(bars)} latest_ts={latest_ts:.0f}"
        )
    except Exception:
        pass
    return result


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
    now_ts = time.time()
    closed_df = _closed_decision_bar_frame(df_new, timeframe=timeframe, now_ts=now_ts)
    snapshot = _decision_bar_freshness_snapshot(closed_df, timeframe=timeframe, now_ts=now_ts)
    if bool(snapshot.get("fresh", False)):
        snapshot.update({"repair_attempted": False, "repair_status": "fresh", "source": "live_decision_bar"})
        _record_decision_bar_freshness(snapshot)
        return closed_df if closed_df is not None and len(closed_df) > 0 else df_new

    repair_suppressed = ""
    try:
        from backend.services.market_session import maintenance_wait_evidence
        from config.runtime_config import shared as _runtime_cfg

        session = dict(market_session or _market_session_snapshot(bridge) or {})
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

    if repair_suppressed:
        snapshot.update(
            {
                "repair_attempted": False,
                "repair_status": repair_suppressed,
                "source": "live_decision_bar_repair_suppressed",
            }
        )
        _record_decision_bar_freshness(snapshot)
        return closed_df if closed_df is not None else df_new

    repair = _repair_live_decision_bars(
        bridge=bridge,
        symbol=symbol,
        timeframe=timeframe,
        expected_closed_bar_ts=float(snapshot.get("expected_closed_bar_ts", 0.0) or 0.0),
        tick=tick,
        log=log,
    )
    repaired_df = _warmup_from_local_db(symbol, timeframe, max(5, len(df_new))) if repair.get("attempted") else None
    if repaired_df is not None and len(repaired_df) > 0:
        closed_df = _closed_decision_bar_frame(repaired_df, timeframe=timeframe, now_ts=time.time())
    final_snapshot = _decision_bar_freshness_snapshot(closed_df, timeframe=timeframe, now_ts=time.time())
    final_snapshot.update(
        {
            "repair_attempted": bool(repair.get("attempted", False)),
            "repair_status": str(repair.get("status") or ""),
            "repair_inserted_bars": int(repair.get("inserted_bars") or 0),
            "repair_latest_bar_ts": float(repair.get("latest_repaired_bar_ts") or 0.0),
            "repair_error": str(repair.get("error") or ""),
            "source": "live_decision_bar_repair",
        }
    )
    _record_decision_bar_freshness(final_snapshot)
    if not bool(final_snapshot.get("fresh", False)):
        try:
            log(
                f"tick {tick}: decision bars stale after repair "
                f"{symbol} {timeframe} latest={final_snapshot.get('latest_bar_ts', 0):.0f} "
                f"expected={final_snapshot.get('expected_closed_bar_ts', 0):.0f} "
                f"status={final_snapshot.get('repair_status')}"
            )
        except Exception:
            pass
    return closed_df if closed_df is not None else df_new


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


def _publish_fresh_position_reconcile(result: Any, *, broker: str) -> list[dict[str, Any]]:
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
            persist=False,
            broker=broker,
            strategy_name=str(_loop_strategy_name or "factor_v4"),
            account=_live_state_get("account", {}, clone=True) or {},
        )
    except Exception as exc:
        # Enrichment/audit is advisory; the broker snapshot remains usable by
        # the safety plane when PostgreSQL or learning metadata is unavailable.
        logger.warning("[live] position snapshot enrichment unavailable: %s", exc)
    _live_state_update(
        positions=positions,
        positions_reconciled=copy.deepcopy(positions),
        positions_updated_at=observed_at,
        positions_reconcile_id=reconcile_id,
        positions_reconcile_failed_at=None,
        positions_reconcile_error=None,
        positions_component_facts=copy.deepcopy(component_facts),
    )
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


def _live_safety_planner_runtime() -> SafetyPlannerRuntime:
    """Build read-only adapters shared by two independent planning algorithms."""

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
            return copy.deepcopy(existing)
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
            loop_running=bool(_live_state_get("loop_running", True)),
        )
        context = _lifecycle_build_position_supervisor_context_payload(
            **context_inputs,
            temporal_context=timeout_context,
            position_metrics=metrics,
        )
        return evaluate_position_supervisor(context)

    def build_trailing_update(position, existing_state, price, atr, conviction):
        anchor = _runtime_config_anchor()
        return _lifecycle_build_legacy_awe_trailing_update(
            position=dict(position),
            existing_state=dict(existing_state or {}),
            current_price=float(price or 0.0),
            atr_price=float(atr or 0.0),
            conviction=float(conviction or 0.0),
            config_version=int(anchor.get("config_version") or 0),
            config_hash=str(anchor.get("config_hash") or ""),
        )

    pipeline = _factor_pipeline or {}
    awe = pipeline.get("awe")

    return SafetyPlannerRuntime(
        build_timeout_context=build_timeout_context,
        load_entry_protection_plan=load_entry_plan,
        evaluate_supervisor=evaluate_supervisor_read_only,
        build_trailing_update=build_trailing_update,
        trailing_state=lambda pid: copy.deepcopy(_trailing_state.get(int(pid), {})),
        composite_conviction=(
            awe.composite_conviction if awe is not None else lambda: 0.0
        ),
    )


def _plan_live_safety_candidates(
    *,
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
        runtime=_live_safety_planner_runtime(),
    )


def _preview_legacy_live_safety_candidates(
    *,
    positions: list[dict[str, Any]],
    cfg: Any,
    account: dict[str, Any],
    current_price: float,
    atr_price: float,
    planned_at: float,
):
    """Run the separate, read-only legacy arbitration preview."""

    return preview_legacy_safety_candidates(
        positions=positions,
        cfg=cfg,
        account=account,
        current_price=current_price,
        atr_price=atr_price,
        planned_at=planned_at,
        entry_repair_cooldown_seconds=_ENTRY_PROTECTION_REPAIR_COOLDOWN_SECONDS,
        runtime=_live_safety_planner_runtime(),
    )


def _safety_candidate_execution_runtime() -> SafetyCandidateExecutionRuntime:
    return SafetyCandidateExecutionRuntime(
        enforce_holding_timeout=_enforce_holding_timeout,
        entry_protection_repair_source=_ENTRY_PROTECTION_REPAIR_SOURCE,
        runtime_config_anchor=_runtime_config_anchor,
        protection_candidate_cls=ProtectionCandidate,
        execute_trailing_candidate=_execute_trailing_candidate,
        evaluate_position_supervisor=(
            _evaluate_position_supervisor_for_position
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
        runtime=_safety_candidate_execution_runtime(),
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
            publish_fresh_positions=_publish_fresh_position_reconcile,
            get_live_state=_live_state_get,
            update_live_state=_live_state_update,
            runtime_config=_runtime_config,
            safety_reference_price=_safety_reference_price,
            factor_pipeline=_factor_pipeline or {},
            plan_safety_candidates=_plan_live_safety_candidates,
            plan_legacy_candidates=_preview_legacy_live_safety_candidates,
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
        enabled=bool(_phase2_feature_flags().ctrader_execution_outcome_v2_enabled),
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
            strategy_name=str(_loop_strategy_name or "factor_v4"),
        ),
    )


def _live_loop_tick_runtime() -> LiveLoopTickRuntime:
    return LiveLoopTickRuntime(
        phase2_active=_phase2_v2_active,
        legacy_tick_body=_run_live_loop_tick_body_legacy,
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
        loop_strategy_name=str(_loop_strategy_name or "factor_v4"),
        restore_session_state=_restore_session_state_for_day,
        evaluate_daily_drawdown=_evaluate_daily_drawdown,
        market_session_snapshot=_market_session_snapshot,
        warmup_from_local_db=_warmup_from_local_db,
        ensure_decision_bars_fresh=_ensure_live_decision_bars_fresh,
        get_safety_plane=_get_live_safety_plane,
        process_tick=_process_tick,
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


def _legacy_live_loop_tick_runtime() -> LegacyLiveLoopTickRuntime:
    return LegacyLiveLoopTickRuntime(
        get_ctrader=_get_ctrader,
        reconcile_positions=_explicit_position_reconcile,
        run_safety_cycle=_run_live_safety_cycle,
        persist_safety_fail_closed=_persist_safety_fail_closed,
        live_state_update=_live_state_update,
        market_session_snapshot=_market_session_snapshot,
        set_loop_diagnostic=_set_loop_diagnostic,
        market_closed_log_message=_loop_market_closed_log_message,
        bridge_readiness_label=_loop_bridge_readiness_label,
        ensure_spot_subscription=_ensure_spot_subscription,
        logger_debug=logger.debug,
        kickoff_account_refresh=kickoff_account_refresh,
        live_state_get=_live_state_get,
        retry_session_restore=_retry_legacy_session_restore,
        loop_strategy_name=str(_loop_strategy_name or "factor_v4"),
        bootstrap_position_recovery=_bootstrap_position_recovery,
        evaluate_daily_drawdown=_evaluate_daily_drawdown,
        warmup_from_local_db=_warmup_from_local_db,
        ensure_decision_bars_fresh=_ensure_live_decision_bars_fresh,
        new_risk_reconciliation_blockers=(
            _new_risk_reconciliation_blockers
        ),
        no_new_risk_latched=no_new_risk_latched,
        process_shutdown_requested=lambda: _process_shutdown_requested,
        compare_spot_to_bar=_loop_compare_spot_quote_to_latest_bar,
        quote_is_fresh=_quote_is_fresh,
        process_tick=_process_tick,
    )


def _run_live_loop_tick_body_legacy(
    *,
    broker: str,
    bridge_cfg: Any,
    timeframe: str,
    tick: int,
    recovery_bootstrapped: bool,
    stop_requested,
    log,
) -> dict[str, Any]:
    return _runtime_run_legacy_live_loop_tick_body(
        broker=broker,
        bridge_cfg=bridge_cfg,
        timeframe=timeframe,
        tick=tick,
        recovery_bootstrapped=recovery_bootstrapped,
        stop_requested=stop_requested,
        log=log,
        runtime=_legacy_live_loop_tick_runtime(),
    )


def _update_live_loop_risk_metrics(*, tick: int, log) -> None:
    try:
        acct = _live_state_get("account_reconciled", {}, clone=True) or {}
        _repair_session_start_balance_from_account()
        equity = float(acct.get("equity") or 0.0)
        eq_hist = _live_state_get("trade_equity_history", [], clone=True) or []
        if equity > 0:
            eq_hist = _append_trade_equity(equity)

        from backend.risk.var import VaRCalculator as _VaRCalc
        _var_calc = _VaRCalc(confidence=0.95)
        if len(eq_hist) >= 10:
            _set_risk_metric("var", _var_calc.calculate(eq_hist))
        else:
            _set_risk_metric("var", _var_calc.get_status(eq_hist))

        from backend.risk.kelly import KellyCriterion as _KellyCalc
        _kelly_calc = _KellyCalc()
        sw = int(_live_state_get("session_winning", 0))
        sl = int(_live_state_get("session_losing", 0))
        total = sw + sl
        trade_pnls = [
            float(x)
            for x in (_live_state_get("session_trade_pnls", [], clone=True) or [])
            if float(x or 0.0) != 0.0
        ]
        wins = [x for x in trade_pnls if x > 0]
        losses = [-x for x in trade_pnls if x < 0]
        if wins or losses:
            kelly_total = len(wins) + len(losses)
            win_rate = len(wins) / kelly_total if kelly_total > 0 else 0.0
            avg_win = sum(wins) / len(wins) if wins else 0.0
            avg_loss = sum(losses) / len(losses) if losses else 0.01
            kelly_status = _kelly_calc.calculate(win_rate, avg_win, max(avg_loss, 0.01))
            kelly_status["closed_trades"] = kelly_total
            _set_risk_metric("kelly", kelly_status)
        elif total > 0:
            win_rate = sw / total
            session_pnl = float(_live_state_get("session_pnl", 0.0))
            if sw > 0 and sl > 0 and session_pnl != 0:
                avg_win = (session_pnl / total) * (1 + win_rate)
                avg_loss = abs((session_pnl / total) * (1 - win_rate)) if win_rate < 1 else 0.01
                avg_loss = max(avg_loss, 0.01)
            else:
                avg_win = 0.0
                avg_loss = 0.01
            kelly_status = _kelly_calc.calculate(win_rate, avg_win, avg_loss)
            kelly_status["closed_trades"] = total
            _set_risk_metric("kelly", kelly_status)
        else:
            kelly_status = _kelly_calc.get_status()
            kelly_status["closed_trades"] = 0
            _set_risk_metric("kelly", kelly_status)

        from backend.risk.stress_test import StressTest as _StressTest
        _stress = _StressTest()
        if len(eq_hist) >= 10:
            _set_risk_metric("stress", _stress.run(eq_hist))
        else:
            _set_risk_metric("stress", _stress.get_status())

        from backend.risk.concentration import ConcentrationChecker as _ConcCheck
        _conc = _ConcCheck()
        _set_risk_metric("concentration", _conc.check())
    except Exception as risk_e:
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
        logger.exception("[live] generation %s failed", generation_id or "legacy")
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
        # A natural/fatal loop exit must not leave scheduler jobs mutating the
        # state of a dead generation.  stop cleanup calls this again safely.
        _stop_live_scheduler()
        _stop_live_safety_watchdog()


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
        phase2_active=_phase2_v2_active,
        update_risk_metrics=_update_live_loop_risk_metrics,
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
    )
    if warmup is None:
        return
    df = warmup.frame

    global _DECISION_LOG, _DECISION_LOG_RUN_ID
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
            if _DECISION_LOG is None:
                _DECISION_LOG = DecisionLogStore()
                _DECISION_LOG_RUN_ID = int(time.time())
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
                timeout_sec=10.0,
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


# ── Background account/positions cache writer ─────────────────────────
# audit 2026-06-10: 之前 _process_tick 每 60s 同步调 bridge.account_info() +
# bridge.get_positions() 写共享缓存. 改读缓存后这个写路径被删了, WS 1s
# 推送就拿到 start_loop 启动时的占位符 (balance=0, equity=0). 修复:
# _run_loop 的 60s 等待期间, 兼容 worker 调显式 account/position reconcile
# 并仅按 broker observed_at 写 _live_state。cache/event/failed 绝不刷新事实时间。
# Phase2 safety plane 启用后不使用这个并发兼容 worker。
def _refresh_account_positions_sync(bridge, broker: str) -> None:
    """One-shot synchronous write to _live_state. Used by the background
    thread; tests call this directly. Best-effort: never raises.

    ★ v9-fix: 连接断开时立刻返回, 不做 API 调用防 timeout 风暴.
    """
    if not _ACCOUNT_REFRESH_LOCK.acquire(blocking=False):
        return
    try:
        # 连接预检: 断开时不调用, 避免 10s timeout 堆积
        if hasattr(bridge, 'is_connected') and not bridge.is_connected:
            return
        now_ts = time.time()
        acct = _live_state_get("account", {}, clone=True) or {}
        account_updated_at = float(_live_state_get("account_updated_at") or 0.0)
        positions_updated_at = float(_live_state_get("positions_updated_at") or 0.0)
        account_fresh = account_updated_at > 0 and (now_ts - account_updated_at) < _ACCOUNT_REFRESH_MIN_INTERVAL
        positions_fresh = (
            positions_updated_at > 0
            and (now_ts - positions_updated_at) < _POSITION_RECONCILE_MIN_INTERVAL
        )
        if account_fresh and positions_fresh:
            return
        if not account_fresh:
            try:
                account_reconcile = _explicit_account_reconcile(bridge)
            except Exception as e:
                logger.warning(f"[{broker}] background account reconcile failed: {e}")
                account_reconcile = None
            raw = (
                _reconcile_value(account_reconcile, "account", None)
                if account_reconcile is not None
                else None
            )
            account_observed_at = float(
                _reconcile_value(account_reconcile, "observed_at", 0.0) or 0.0
            )
            if raw is not None and account_observed_at > 0:
                # 统一转 dict: CTraderBridge 返 AccountInfo dataclass
                acct = asdict(raw) if is_dataclass(raw) else dict(raw)
                # audit 2026-06-10: ensure the cached account has `ok=True` so the
                # WS snapshot doesn't mistake it for an error envelope.
                acct.setdefault("ok", True)
                acct.setdefault("broker", broker)
                _live_state_update(
                    account=acct,
                    account_reconciled=copy.deepcopy(acct),
                    account_updated_at=account_observed_at,
                    account_reconcile_id=str(
                        _reconcile_value(account_reconcile, "reconcile_id", "") or ""
                    ),
                    account_reconcile_failed_at=None,
                    account_reconcile_error=None,
                )
            else:
                _mark_account_reconcile_failed("background_account_reconcile_failed")
        if not positions_fresh:
            try:
                positions_reconcile = _explicit_position_reconcile(bridge)
                if str(
                    _reconcile_value(positions_reconcile, "status", "failed") or "failed"
                ) == "fresh":
                    _publish_fresh_position_reconcile(
                        positions_reconcile,
                        broker=broker,
                    )
                else:
                    _mark_positions_reconcile_failed(
                        str(
                            _reconcile_value(positions_reconcile, "error_code", "")
                            or "background_positions_reconcile_failed"
                        )
                    )
            except Exception as e:
                logger.warning(f"[{broker}] background positions reconcile failed: {e}")
                _mark_positions_reconcile_failed(
                    f"background_positions_reconcile_exception:{type(e).__name__}"
                )
    finally:
        _ACCOUNT_REFRESH_LOCK.release()


def kickoff_account_refresh(bridge, broker: str, interval_sec: float = 5.0) -> threading.Thread:
    """Spawn a daemon thread that periodically calls
    _refresh_account_positions_sync. Used by _run_loop during its 60s
    wait so the next WS tick has fresh account/positions data.

    The thread loops: refresh once, then sleep interval_sec, until the
    global _loop_stop_flag is set OR the process exits (daemon=True).

    ★ v9-fix: 连接断开时不做 API 调用 + 指数退避, 防 timeout 风暴.
    ★ v11-fix: 单例检查, 避免每 tick 创建新线程 (P0-4 线程泄漏).
    """
    global _refresh_thread
    if _refresh_thread is not None and _refresh_thread.is_alive():
        return _refresh_thread

    stop_flag_ref = _loop_stop_flag  # captured at call time
    _fail_count = 0
    _MAX_BACKOFF = 300  # 最大退避 5min

    def _worker():
        nonlocal _fail_count
        while True:
            try:
                if stop_flag_ref is not None and stop_flag_ref.is_set():
                    break

                # v9-fix: 连接断开时跳过调用, 不做 API 调用避免 timeout 风暴
                if hasattr(bridge, 'is_connected') and not bridge.is_connected:
                    _sleep_sliced(min(interval_sec, 5.0), stop_flag_ref)
                    continue

                _refresh_account_positions_sync(bridge, broker)
                _fail_count = 0  # 成功后重置失败计数

                # Sleep interval
                _sleep_sliced(interval_sec, stop_flag_ref)
            except Exception as e:
                _fail_count += 1
                backoff = min(_MAX_BACKOFF, interval_sec * (2 ** min(_fail_count, 5)))
                logger.warning(
                    f"[{broker}] account-refresh error #{_fail_count}: {e}, "
                    f"backoff {backoff:.0f}s"
                )
                _sleep_sliced(backoff, stop_flag_ref)

    def _sleep_sliced(duration: float, stop_flag) -> None:
        """在 stop_flag 检查之间分片休眠, 保证快速响应停止信号."""
        slept = 0.0
        while slept < duration:
            if stop_flag is not None and stop_flag.is_set():
                return
            chunk = min(0.5, duration - slept)
            time.sleep(chunk)
            slept += chunk

    t = threading.Thread(
        target=_worker, daemon=True,
        name=f"acct-refresh-{broker}",
    )
    t.start()
    _refresh_thread = t
    return t


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
            return _process_tick_factor_pipeline(
                bridge, _factor_pipeline, df_new, last_bar, broker, tick, log,
                stop_requested=stop_requested,
                protection_already_run=protection_already_run,
            )
        except Exception as e:
            log(f"tick {tick}: factor pipeline error: {e}")

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
_refresh_thread: threading.Thread | None = None  # v11-fix (P0-4): account refresh 单例
_cross_asset_covar: "CrossAssetCovariance | None" = None  # 跨品种协方差


# ═══════════════════════════════════════════════════════════
# 审计日志 (统一使用 DecisionLogStore → PostgreSQL state store)
# ═══════════════════════════════════════════════════════════
import json as _json


def _should_send_orders(broker: str) -> bool:
    """True = 真发单; False = dry-run (记 log, 不下单)."""
    if broker == "ctrader":
        from backend.services.execution_semantics import current_execution_semantics

        semantics = current_execution_semantics()
        if semantics.blocking_reason:
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
    """Resolve/read execution intents without making emergency close depend on PG.

    The production cTrader bridge owns the full recovery contract.  The
    compatibility fallback only observes the fsync'd local unknown-outcome
    ledger; when execution outcome v2 is enabled, a missing bridge recovery
    API is itself an unresolved state.
    """

    if hasattr(bridge, "recover_execution_intents"):
        return dict(bridge.recover_execution_intents() or {})
    try:
        from backend.services.live_safety_state import unresolved_broker_outcome_mutations

        unresolved = list(unresolved_broker_outcome_mutations())
    except Exception as exc:
        return {
            "schema": "broker_execution_intent_recovery.v1",
            "ready": False,
            "enabled": bool(_phase2_feature_flags().ctrader_execution_outcome_v2_enabled),
            "unresolved_count": None,
            "unresolved": [],
            "error": f"local_execution_recovery_unavailable:{type(exc).__name__}:{exc}",
        }
    enabled = bool(_phase2_feature_flags().ctrader_execution_outcome_v2_enabled)
    if enabled:
        return {
            "schema": "broker_execution_intent_recovery.v1",
            "ready": False,
            "enabled": True,
            "unresolved_count": None,
            "unresolved": unresolved,
            "error": "bridge_execution_recovery_contract_missing",
        }
    return {
        "schema": "broker_execution_intent_recovery.v1",
        "ready": not unresolved,
        "enabled": False,
        "unresolved_count": len(unresolved),
        "unresolved": unresolved,
    }


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
        entry_decision_id = _LEDGER.log_composite_decision(
            **ledger_payloads["composite_decision_payload"]
        )
        _pos_entry_decisions[int(pid)] = entry_decision_id
        _LEDGER.log_order_event(
            decision_id=entry_decision_id,
            **ledger_payloads["submitted_order_payload"],
        )
        _LEDGER.log_order_event(
            decision_id=entry_decision_id,
            **ledger_payloads["filled_order_payload"],
        )
        _LEDGER.log_position_event(**ledger_payloads["position_event_payload"])
        return entry_decision_id
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
            strategy_name=str(_loop_strategy_name or "factor_v4"),
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
        _upsert_recovery_position_state(
            recovery_payloads["state_payload"],
            **recovery_payloads["state_kwargs"],
            meta=recovery_payloads["meta"],
        )
    except Exception as recovery_err:
        logger.debug("[live] recovery open persist failed for pos %s: %s", pid, recovery_err)


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
) -> str:
    """Persist open context after a market fill, even if SL/TP amend fails."""
    base_volume = float(base_requested_volume if base_requested_volume is not None else requested_volume or 0.0)
    trade_attribution_payload = _record_filled_open_attribution(
        attr_engine=attr_engine,
        pid=pid,
        current_price=current_price,
        actual_api_volume=actual_api_volume,
        composite=composite,
    )
    entry_decision_id = ""
    if _LEDGER:
        try:
            ledger_learning_context = _open_learning_context_payload(
                bridge=bridge, bar=bar, positions_before=pos, composite=composite,
                symbol="XAUUSD+", pid=int(pid), actual_api_volume=float(actual_api_volume or 0.0),
                requested_volume=float(requested_volume or 0.0), base_requested_volume=base_volume,
                current_price=float(current_price or 0.0), fill_price=float(fill_price or 0.0),
                sl_price=float(sl_price or 0.0), tp_price=float(tp_price or 0.0),
                sl_dist=float(sl_dist or 0.0), tp_dist=float(tp_dist or 0.0),
                event_sizing_context=event_sizing_context or {}, sizing_trace=sizing_trace or {},
                risk_verdict=risk_verdict, market_session=market_session,
            )
            entry_decision_id = _log_filled_open_ledger(
                cfg=cfg, bar=bar, tick=tick, pid=pid, actual_api_volume=actual_api_volume,
                requested_volume=requested_volume, fill_price=fill_price, current_price=current_price,
                sl_price=sl_price, tp_price=tp_price, acct=acct, pos=pos, composite=composite,
                gate_result=gate_result, learning_context=ledger_learning_context,
                risk_verdict=risk_verdict, sizing_trace=sizing_trace,
            )
        except Exception as ledger_err:
            logger.debug("[live] ledger open persist failed for pos %s: %s", pid, ledger_err)
    try:
        recovery_learning_context = _open_learning_context_payload(
            bridge=bridge, bar=bar, positions_before=pos, composite=composite,
            symbol="XAUUSD+", pid=int(pid), actual_api_volume=float(actual_api_volume or 0.0),
            requested_volume=float(requested_volume or 0.0), base_requested_volume=base_volume,
            current_price=float(current_price or 0.0), fill_price=float(fill_price or 0.0),
            sl_price=float(sl_price or 0.0), tp_price=float(tp_price or 0.0),
            sl_dist=float(sl_dist or 0.0), tp_dist=float(tp_dist or 0.0),
            event_sizing_context=event_sizing_context or {}, risk_verdict=risk_verdict,
            market_session=market_session,
        )
        _upsert_filled_open_recovery(
            broker=broker, tick=tick, pid=pid, actual_api_volume=actual_api_volume,
            requested_volume=requested_volume, fill_price=fill_price, current_price=current_price,
            sl_price=sl_price, tp_price=tp_price, composite=composite,
            entry_decision_id=entry_decision_id, trade_attribution_payload=trade_attribution_payload,
            learning_context=recovery_learning_context,
        )
    except Exception as recovery_err:
        logger.debug("[live] recovery open persist failed for pos %s: %s", pid, recovery_err)

    return entry_decision_id


def _collect_closed_position_attribution(
    *,
    cpid: int,
    real_pnl: dict | None,
    attr_engine: Any,
    current_price: float,
    tick: int,
    log,
) -> dict[str, Any]:
    close_reason = _consume_close_reason(int(cpid), "broker_close")
    close_verdict = _consume_close_verdict(int(cpid), close_reason)
    close_ts = float((real_pnl or {}).get("exec_timestamp") or time.time())
    attribution_integrity = (
        attr_engine.open_integrity(cpid)
        if attr_engine is not None and hasattr(attr_engine, "open_integrity")
        else "missing"
    )
    mc = attr_engine.record_close(cpid, close_price=current_price, close_ts=close_ts, real_pnl=real_pnl)
    if not mc:
        attribution_integrity = "missing"
    close_source = _classify_close_source(int(cpid), close_reason, close_ts)
    fallback_pnl = 0.0
    if not real_pnl and not mc:
        try:
            fallback_pnl = _estimate_close_pnl_from_cached_state(int(cpid), float(current_price))
        except Exception as _e2:
            log(f"tick {tick}: attribution close pos={cpid} PnL fallback error: {_e2}")
    total_pnl = _tick_select_close_total_pnl(
        real_pnl=real_pnl,
        factor_contributions=mc,
        fallback_pnl=fallback_pnl,
    )
    log(f"tick {tick}: attribution close pos={cpid} pnl={total_pnl:.2f} factors={len(mc)}")
    _pos_open_api_volume.pop(int(cpid), None)
    return {
        "close_reason": close_reason,
        "close_verdict": close_verdict,
        "close_ts": close_ts,
        "attribution_integrity": attribution_integrity,
        "factor_contributions": mc,
        "close_source": close_source,
        "total_pnl": total_pnl,
    }


def _write_close_decision_log_after_tick(
    *,
    cpid: int,
    bar: dict,
    total_pnl: float,
    current_price: float,
    tick: int,
) -> None:
    if not _DECISION_LOG:
        return
    bar_ts = bar.get("time", 0)
    bar_date = time.strftime("%Y-%m-%d", time.gmtime(bar_ts)) if bar_ts else ""
    _safe_decision_log(
        _DECISION_LOG,
        run_id=_DECISION_LOG_RUN_ID,
        ts=bar_ts or time.time(),
        bar_date=bar_date,
        decision_type="close",
        strategy="factor_v4",
        direction=0,
        confidence=round(total_pnl, 2),
        decision="closed",
        meta=_json.dumps(
            _tick_build_close_decision_audit_meta(
                position_id=int(cpid),
                total_pnl=float(total_pnl),
                current_price=float(current_price),
                tick=tick,
            ),
            ensure_ascii=False,
        ),
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
    if not _LEDGER:
        return "", context_integrity
    try:
        repaired_entry_decision_id = _ensure_open_ledger_for_recovered_close(
            int(cpid),
            broker=broker,
            close_ts=close_ts,
            close_price=float(current_price),
            real_pnl=real_pnl,
            close_reason=close_reason,
        )
        if repaired_entry_decision_id:
            context_integrity = _lookup_recovery_context_integrity(int(cpid), context_integrity)
        close_ledger_payloads = _tick_build_close_ledger_payloads(
            position_id=int(cpid),
            timeframe=str(getattr(cfg, "timeframe", "") or ""),
            decision_ts=bar.get("time", close_ts),
            close_ts=close_ts,
            account=acct,
            session_pnl=_live_state_get("session_pnl", 0),
            risk_state=_risk_state_with_verdict_dict(close_verdict),
            total_pnl=float(total_pnl),
            current_price=float(current_price),
            tick=tick,
            close_reason=close_reason,
            close_source=close_source,
            attribution_integrity=attribution_integrity,
            close_verdict=close_verdict,
            factor_contributions=factor_contributions,
            real_pnl=real_pnl,
        )
        exit_decision_id = _LEDGER.log_decision(**close_ledger_payloads["decision"])
        _LEDGER.log_position_event(**close_ledger_payloads["position_event"])
        return exit_decision_id, context_integrity
    except Exception:
        logger.exception("[live] ledger close failed for pos {}", cpid)
        return "", context_integrity


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
    if not (_TRADE_REVIEWER and _EXPERIENCE_BUILDER and _POLICY_SUGGESTER):
        return
    try:
        review = _TRADE_REVIEWER.review_closed_trade(
            **_tick_build_trade_review_payload(
                position_id=int(cpid),
                total_pnl=float(total_pnl),
                current_price=float(current_price),
                close_ts=close_ts,
                factor_contributions=factor_contributions,
                exit_decision_id=exit_decision_id,
                real_pnl=real_pnl,
                close_reason=close_reason,
                context_integrity=context_integrity,
                attribution_integrity=attribution_integrity,
                close_source=close_source,
            )
        )
        if review.get("accepted", True):
            experience = _EXPERIENCE_BUILDER.build_from_review(review)
            _POLICY_SUGGESTER.suggest_from_experience(experience)
        else:
            logger.info(
                "[live] skipped unverified trade review for pos %s: %s",
                cpid,
                review.get("skip_reason", "unknown"),
            )
    except Exception:
        logger.exception("[live] post-trade learning failed for pos {}", cpid)


def _cleanup_closed_position_after_tick(
    *,
    cpid: int,
    close_reason: str,
    total_pnl: float,
    close_ts: float,
    real_pnl: dict | None,
    factor_contributions: dict,
) -> bool:
    recovery_projection_ready = True
    try:
        _mark_recovery_position_closed(
            int(cpid),
            close_reason=close_reason,
            close_pnl=float(total_pnl),
            closed_at=close_ts,
            meta={"real_pnl": real_pnl or {}, "factor_contributions": factor_contributions or {}},
        )
    except Exception as _recovery_close_err:
        recovery_projection_ready = False
        logger.debug("[live] recovery close persist failed for pos %s: %s", cpid, _recovery_close_err)
    _trailing_state.pop(cpid, None)
    _pos_entry_scores.pop(cpid, None)
    _pos_entry_decisions.pop(int(cpid), None)
    _pending_open_attach_until.pop(int(cpid), None)
    return recovery_projection_ready


def _handle_closed_positions_after_tick(
    *,
    closed_pids: set[int],
    real_pnls: dict[int, dict],
    attr_engine: Any,
    current_price: float,
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
    confirmed_close_ids: set[int] = set()
    recovery_projected_ids: set[int] = set()
    for cpid in closed_pids:
        real_pnl = real_pnls.get(cpid)
        try:
            if not _authoritative_close_pnl(real_pnl):
                cursor = dict((close_deal_cursors or {}).get(int(cpid)) or {})
                _defer_close_until_authoritative_deal(
                    int(cpid),
                    broker=broker,
                    tick=tick,
                    recovery_evidence=(
                        {
                            "pending_kind": "final_close",
                            **cursor,
                        }
                        if cursor
                        else None
                    ),
                )
                log(
                    f"tick {tick}: close pos={cpid} deferred until authoritative "
                    "cTrader close deal is available"
                )
                continue
            confirmed_close_ids.add(int(cpid))
            # The prior risk projection no longer includes all realized broker
            # facts.  Block admission before any auxiliary attribution/audit
            # work that may fail; the deterministic rebuild below is the only
            # path back to ``available``.
            _live_state_update(
                session_state_status="unavailable",
                session_state_source="post_close_projection_pending",
                session_risk_blockers=[
                    f"post_close_projection_pending:{pid}"
                    for pid in sorted(confirmed_close_ids)
                ],
                session_observed_at=0.0,
                accepting_new_risk=False,
            )
            close_payload = _collect_closed_position_attribution(
                cpid=int(cpid),
                real_pnl=real_pnl,
                attr_engine=attr_engine,
                current_price=current_price,
                tick=tick,
                log=log,
            )
            total_pnl = float(close_payload["total_pnl"])
            close_ts = float(close_payload["close_ts"])
            close_reason = str(close_payload["close_reason"])
            close_source = close_payload["close_source"]
            close_verdict = close_payload["close_verdict"]
            attribution_integrity = str(close_payload["attribution_integrity"])
            factor_contributions = close_payload["factor_contributions"]
            _write_close_decision_log_after_tick(
                cpid=int(cpid),
                bar=bar,
                total_pnl=total_pnl,
                current_price=current_price,
                tick=tick,
            )
            context_integrity = _lookup_recovery_context_integrity(int(cpid), _RECOVERY_CONTEXT_FULL)
            exit_decision_id, context_integrity = _log_closed_position_ledger_after_tick(
                cpid=int(cpid),
                broker=broker,
                close_ts=close_ts,
                current_price=current_price,
                real_pnl=real_pnl,
                close_reason=close_reason,
                context_integrity=context_integrity,
                cfg=cfg,
                bar=bar,
                acct=acct,
                total_pnl=total_pnl,
                tick=tick,
                close_source=close_source,
                attribution_integrity=attribution_integrity,
                close_verdict=close_verdict,
                factor_contributions=factor_contributions,
            )
            _run_closed_position_learning_after_tick(
                cpid=int(cpid),
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
            )
            recovery_projection_ready = _cleanup_closed_position_after_tick(
                cpid=int(cpid),
                close_reason=close_reason,
                total_pnl=total_pnl,
                close_ts=close_ts,
                real_pnl=real_pnl,
                factor_contributions=factor_contributions,
            )
            if recovery_projection_ready is not False:
                recovery_projected_ids.add(int(cpid))
        except Exception as exc:
            log(f"tick {tick}: attribution close pos={cpid} error: {exc}")
            if int(cpid) in confirmed_close_ids:
                _record_risk_reduction_aux_failure(
                    "post_close_auxiliary_processing_failed",
                    position_id=int(cpid),
                    action="close_position",
                    error=exc,
                )
                try:
                    _mark_recovery_position_closed(
                        int(cpid),
                        close_reason="broker_close_auxiliary_deferred",
                        close_pnl=float((real_pnl or {}).get("net") or 0.0),
                        closed_at=float(
                            (real_pnl or {}).get("exec_timestamp")
                            or time.time()
                        ),
                        meta={
                            "real_pnl": real_pnl or {},
                            "auxiliary_processing_error": (
                                f"{type(exc).__name__}:{exc}"
                            ),
                        },
                    )
                    recovery_projected_ids.add(int(cpid))
                except Exception as recovery_exc:
                    _record_risk_reduction_aux_failure(
                        "post_close_recovery_projection_failed",
                        position_id=int(cpid),
                        action="close_position",
                        error=recovery_exc,
                    )

    if confirmed_close_ids:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        account_reconcile = (
            _explicit_account_reconcile(bridge)
            if broker_open_position_ids is not None
            else None
        )
        account_value = (
            _reconcile_value(account_reconcile, "account", None)
            if account_reconcile is not None
            else None
        )
        account_ready = account_value is not None
        if account_ready:
            account_payload = (
                asdict(account_value)
                if is_dataclass(account_value)
                else dict(account_value)
            )
            account_payload.update({"ok": True, "broker": broker})
            _live_state_update(
                account=account_payload,
                account_reconciled=copy.deepcopy(account_payload),
                account_updated_at=float(
                    _reconcile_value(account_reconcile, "observed_at", 0.0)
                    or 0.0
                ),
                account_reconcile_id=str(
                    _reconcile_value(account_reconcile, "reconcile_id", "")
                    or ""
                ),
                account_reconcile_failed_at=None,
                account_reconcile_error=None,
            )
        else:
            _live_state_update(
                account_reconcile_failed_at=time.time(),
                account_reconcile_error="post_close_account_reconcile_failed",
            )
        restored = bool(
            account_ready
            and broker_open_position_ids is not None
            and _restore_session_state_for_day(
                trade_date,
                broker_open_position_ids={
                    int(pid)
                    for pid in broker_open_position_ids
                    if int(pid or 0) > 0
                },
                confirmed_closed_position_ids=set(confirmed_close_ids),
            )
        )
        pending_projection_ids = set(confirmed_close_ids)
        if restored:
            for position_id in sorted(
                confirmed_close_ids & recovery_projected_ids
            ):
                _release_session_close_deal_latch(
                    position_id,
                    real_pnls[position_id],
                )
            pending_projection_ids -= recovery_projected_ids
        if pending_projection_ids:
            for position_id in sorted(pending_projection_ids):
                cursor = dict(
                    (close_deal_cursors or {}).get(position_id) or {}
                )
                _defer_close_until_authoritative_deal(
                    position_id,
                    broker=broker,
                    tick=tick,
                    reason="post_close_session_projection_unavailable",
                    recovery_evidence={
                        "pending_kind": "final_close",
                        **cursor,
                        "confirmed_deal_ids": list(
                            real_pnls[position_id].get("deal_ids") or []
                        ),
                    },
                )
            _live_state_update(
                session_state_status="unavailable",
                session_state_source="post_close_projection_unavailable",
                session_risk_blockers=[
                    f"post_close_projection_pending:{pid}"
                    for pid in sorted(pending_projection_ids)
                ],
                session_observed_at=0.0,
                accepting_new_risk=False,
            )
            _record_risk_reduction_aux_failure(
                "post_close_session_projection_unavailable",
                action="close_position",
                error="authoritative_session_restore_unavailable",
                payload={
                    "position_ids": sorted(pending_projection_ids),
                    "session_projection_restored": bool(restored),
                },
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
        entry_decision_id = _LEDGER.log_composite_decision(
            **open_ledger_payloads["decision"]
        )
        _pos_entry_decisions[int(pid)] = entry_decision_id
        _LEDGER.log_order_event(
            decision_id=entry_decision_id,
            **open_ledger_payloads["submitted_order"],
        )
        _LEDGER.log_order_event(
            decision_id=entry_decision_id,
            **open_ledger_payloads["filled_order"],
        )
        _LEDGER.log_position_event(
            **open_ledger_payloads["position_event"],
        )
        return entry_decision_id
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
) -> None:
    try:
        recovery_payloads = _lifecycle_build_filled_open_recovery_payloads(
            position_id=int(pid),
            broker=broker,
            strategy_name=str(_loop_strategy_name or "factor_v4"),
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
        _upsert_recovery_position_state(
            recovery_payloads["state_payload"],
            **recovery_payloads["state_kwargs"],
            meta=recovery_payloads["meta"],
        )
    except Exception as _recovery_open_err:
        logger.debug("[live] recovery open persist failed for pos %s: %s", pid, _recovery_open_err)


def _write_amended_open_decision_log(
    *,
    bar: dict,
    composite: Any,
    pid: int,
    actual_api_volume: float,
    requested_volume: float,
    base_requested_volume: float,
    event_sizing_context: dict[str, Any],
    sizing_trace: dict[str, Any],
    current_price: float,
    sl_price: float,
    tp_price: float,
    tick: int,
) -> None:
    if not _DECISION_LOG:
        return
    payload = _tick_build_open_decision_log_payload(
        bar=bar,
        composite=composite,
        position_id=int(pid),
        actual_api_volume=float(actual_api_volume),
        requested_volume=float(requested_volume),
        base_requested_volume=float(base_requested_volume),
        event_sizing_context=event_sizing_context,
        sizing_trace=sizing_trace,
        current_price=float(current_price),
        sl_price=float(sl_price),
        tp_price=float(tp_price),
        tick=tick,
        fallback_ts=time.time(),
    )
    _safe_decision_log(
        _DECISION_LOG,
        run_id=_DECISION_LOG_RUN_ID,
        ts=payload["ts"],
        bar_date=payload["bar_date"],
        decision_type=payload["decision_type"],
        strategy=payload["strategy"],
        direction=payload["direction"],
        confidence=payload["confidence"],
        decision=payload["decision"],
        meta=_json.dumps(payload["meta"], ensure_ascii=False),
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
) -> None:
    _mark_amended_open_success_local_state(
        pid=pid,
        sl_price=sl_price,
        tp_price=tp_price,
        tick=tick,
        actual_api_volume=actual_api_volume,
        composite=composite,
        direction_name=direction_name,
        log=log,
    )
    _record_amended_open_execution_quality(
        bar=bar, current_price=current_price, fill_price=fill_price,
        composite=composite, actual_api_volume=actual_api_volume, pid=pid,
        submit_started_at=submit_started_at, fill_received_at=fill_received_at,
    )
    try:
        trade_attr = _record_amended_open_attribution(
            attr_engine=attr_engine, pid=pid, current_price=current_price,
            actual_api_volume=actual_api_volume, composite=composite, tick=tick, log=log,
        )
        learning_context = _open_learning_context_payload(
            bridge=bridge,
            bar=bar,
            positions_before=pos,
            composite=composite,
            symbol="XAUUSD+",
            pid=int(pid),
            actual_api_volume=float(actual_api_volume or 0.0),
            requested_volume=float(requested_volume or 0.0),
            base_requested_volume=float(base_requested_volume or 0.0),
            current_price=float(current_price or 0.0),
            fill_price=float(fill_price or 0.0),
            sl_price=float(sl_price or 0.0),
            tp_price=float(tp_price or 0.0),
            sl_dist=float(sl_dist or 0.0),
            tp_dist=float(tp_dist or 0.0),
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            risk_verdict=risk_verdict,
            market_session=market_session,
        )
        entry_decision_id = _log_amended_open_ledger(
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
            acct=acct,
            pos=pos,
            composite=composite,
            gate_result=gate_result,
            risk_verdict=risk_verdict,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            learning_context=learning_context,
        )
        _upsert_amended_open_recovery(
            broker=broker,
            tick=tick,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            composite=composite,
            entry_decision_id=entry_decision_id,
            entry_protection_plan=entry_protection_plan,
            trade_attr=trade_attr,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            learning_context=learning_context,
        )
        _write_amended_open_decision_log(
            bar=bar,
            composite=composite,
            pid=pid,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            base_requested_volume=base_requested_volume,
            event_sizing_context=event_sizing_context,
            sizing_trace=sizing_trace,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            tick=tick,
        )
    except Exception as attr_err:
        log(f"tick {tick}: attribution record_open error: {attr_err}")


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
) -> None:
    # The position already exists at the broker but its required entry
    # protection is not confirmed.  Block *additional* risk immediately while
    # leaving the serial safety path free to repair or close this position.
    # This local latch does not depend on PostgreSQL or the audit ledger below.
    try:
        _persist_safety_fail_closed(
            blockers=("entry_protection_unverified",),
            source="entry_protection",
            error=str(status_error or ledger_action_reason or "entry_protection_failed"),
        )
    except Exception as latch_exc:
        try:
            _record_risk_reduction_aux_failure(
                "entry_protection_fail_closed_unavailable",
                position_id=int(pid or 0),
                action="amend_position_sltp",
                error=latch_exc,
                payload={"status_error": str(status_error or "")},
            )
        except Exception:
            pass
    if failure_log and log is not None:
        log(failure_log)
    _record_filled_position_open_context(
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
    )
    _update_entry_protection_plan_status(
        int(pid),
        status="failed",
        error=status_error,
        attempted=True,
    )
    if _LEDGER:
        try:
            amend_failed_payloads = _tick_build_amend_failed_ledger_payloads(
                composite=composite,
                gate_result=gate_result,
                cfg=cfg,
                bar=bar,
                account=acct,
                positions_before=pos,
                risk_state=_live_state_get("risk", {}, clone=True) or {},
                pid=int(pid),
                requested_volume=float(requested_volume),
                fill_price=float(fill_price),
                sl_price=float(sl_price),
                tp_price=float(tp_price),
                actual_api_volume=float(actual_api_volume),
                tick=tick,
                action_reason=ledger_action_reason,
                comment=ledger_comment,
                error=ledger_error,
                decision_ts_fallback=time.time(),
            )
            amend_decision_id = _LEDGER.log_composite_decision(
                **amend_failed_payloads["decision"]
            )
            _LEDGER.log_order_event(
                decision_id=amend_decision_id,
                **amend_failed_payloads["order_event"],
            )
        except Exception as _ledger_err:
            logger.debug(ledger_debug_message, pid, _ledger_err)


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
    try:
        from backend.services.model_influence import shared_model_influence_service

        meta_cap = shared_model_influence_service().apply_meta_risk_cap(
            volume=volume,
            subject_id=f"XAUUSD+:{int(float(bar.get('time') or time.time()))}",
            cfg=cfg,
        )
        capped_volume = float(meta_cap.get("volume") or 0.0)
        if capped_volume < volume:
            volume = _floor_api_volume_to_step(capped_volume, bridge_meta)
        sizing_trace["meta_model_risk_cap"] = {**meta_cap, "rounded_api_volume": volume}
    except Exception as exc:
        sizing_trace["meta_model_risk_cap"] = {
            "applied": False,
            "reason": f"meta_model_cap_unavailable:{type(exc).__name__}",
        }

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


def _submit_open_trade_order(bridge: Any, composite: Any, volume: float):
    if composite.direction == 1:
        return bridge.market_buy(volume=volume, sl=0.0, tp=0.0, comment="quant-v4")
    if composite.direction == -1:
        return bridge.market_sell(volume=volume, sl=0.0, tp=0.0, comment="quant-v4")
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
) -> None:
    try:
        _upsert_recovery_position_state(
            {
                "position_id": position_id,
                "symbol": "XAUUSD+",
                "direction": composite.direction,
                "open_price": float(fill_price or current_price),
                "volume": float(actual_api_volume),
                "entry_decision_id": _lookup_entry_decision_id(int(position_id)),
            },
            broker=broker,
            strategy_name=str(_loop_strategy_name or "factor_v4"),
            status="open",
            meta={
                "tick": tick,
                "sl": round(sl_price, 2),
                "tp": round(tp_price, 2),
                "entry_protection_plan": entry_protection_plan,
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
    try:
        amend_res = bridge.amend_position_sltp(
            position_id=position_id,
            sl=sl_price,
            tp=tp_price,
        )
        if getattr(amend_res, "success", False):
            projection = _explicit_position_reconcile(bridge)
            verification = _verify_position_protection_projection(
                projection,
                position_id=position_id,
                expected_stop_loss=sl_price,
                expected_take_profit=tp_price,
                precision=int((getattr(bridge, "_symbol_meta", None) or {}).get("digits", 2) or 2),
            )
            if bool(verification.get("ok")):
                _publish_fresh_position_reconcile(projection, broker=broker)
                _release_entry_protection_pending_latch(
                    position_id,
                    reconcile=projection,
                    expected_stop_loss=sl_price,
                    expected_take_profit=tp_price,
                )
                _record_amended_open_success_context(
                    attr_engine=attr_engine,
                    bridge=bridge,
                    broker=broker,
                    cfg=cfg,
                    bar=bar,
                    tick=tick,
                    pid=position_id,
                    actual_api_volume=actual_api_volume,
                    requested_volume=requested_volume,
                    base_requested_volume=base_requested_volume,
                    fill_price=fill_price,
                    current_price=current_price,
                    sl_price=sl_price,
                    tp_price=tp_price,
                    sl_dist=sl_dist,
                    tp_dist=tp_dist,
                    acct=account,
                    pos=positions,
                    composite=composite,
                    gate_result=gate_result,
                    risk_verdict=candidate.risk_verdict,
                    market_session=candidate.market_session,
                    event_sizing_context=candidate.event_sizing_context,
                    sizing_trace=candidate.sizing_trace,
                    entry_protection_plan=entry_protection_plan,
                    direction_name=candidate.direction_name,
                    log=log,
                    submit_started_at=submit_started_at,
                    fill_received_at=fill_received_at,
                )
                return

            projection_reason = str(
                verification.get("reason") or "position_reconcile_failed"
            )
            amend_failure_reason = (
                f"entry_protection_projection_unverified:{projection_reason}"
            )
            _record_risk_reduction_aux_failure(
                "entry_protection_projection_unverified",
                position_id=int(position_id),
                action="amend_position_sltp",
                error=amend_failure_reason,
                payload={"verification": verification},
            )
            _record_amend_failure_after_fill(
                attr_engine=attr_engine,
                bridge=bridge,
                broker=broker,
                cfg=cfg,
                bar=bar,
                tick=tick,
                pid=position_id,
                actual_api_volume=actual_api_volume,
                requested_volume=requested_volume,
                base_requested_volume=base_requested_volume,
                fill_price=fill_price,
                current_price=current_price,
                sl_price=sl_price,
                tp_price=tp_price,
                acct=account,
                pos=positions,
                composite=composite,
                gate_result=gate_result,
                risk_verdict=candidate.risk_verdict,
                market_session=candidate.market_session,
                event_sizing_context=candidate.event_sizing_context,
                sizing_trace=candidate.sizing_trace,
                sl_dist=sl_dist,
                tp_dist=tp_dist,
                status_error=amend_failure_reason,
                ledger_action_reason=amend_failure_reason,
                ledger_comment=str(getattr(amend_res, "comment", "") or ""),
                failure_log=(
                    f"tick {tick}: v4 {candidate.direction_name} AMEND UNVERIFIED "
                    f"pos={position_id}: {amend_failure_reason}"
                ),
                log=log,
            )
            return

        amend_failure_reason = str(
            getattr(amend_res, "comment", "")
            or getattr(amend_res, "error", "")
            or "amend_failed"
        )
        _record_amend_failure_after_fill(
            attr_engine=attr_engine,
            bridge=bridge,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=position_id,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            base_requested_volume=base_requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            acct=account,
            pos=positions,
            composite=composite,
            gate_result=gate_result,
            risk_verdict=candidate.risk_verdict,
            market_session=candidate.market_session,
            event_sizing_context=candidate.event_sizing_context,
            sizing_trace=candidate.sizing_trace,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            status_error=amend_failure_reason,
            ledger_action_reason=str(getattr(amend_res, "comment", "amend_failed") or "amend_failed"),
            ledger_comment=str(getattr(amend_res, "comment", "") or ""),
            failure_log=(
                f"tick {tick}: v4 {candidate.direction_name} AMEND FAILED "
                f"pos={position_id}: {amend_failure_reason}"
            ),
            log=log,
        )
    except Exception as exc:
        _record_amend_failure_after_fill(
            attr_engine=attr_engine,
            bridge=bridge,
            broker=broker,
            cfg=cfg,
            bar=bar,
            tick=tick,
            pid=position_id,
            actual_api_volume=actual_api_volume,
            requested_volume=requested_volume,
            base_requested_volume=base_requested_volume,
            fill_price=fill_price,
            current_price=current_price,
            sl_price=sl_price,
            tp_price=tp_price,
            acct=account,
            pos=positions,
            composite=composite,
            gate_result=gate_result,
            risk_verdict=candidate.risk_verdict,
            market_session=None,
            event_sizing_context=candidate.event_sizing_context,
            sizing_trace=candidate.sizing_trace,
            sl_dist=sl_dist,
            tp_dist=tp_dist,
            status_error=f"amend_exception:{type(exc).__name__}:{str(exc)[:220]}",
            ledger_action_reason=f"amend_exception:{type(exc).__name__}",
            ledger_error=str(exc)[:300],
            ledger_debug_message="[live] ledger amend exception event failed for pos %s: %s",
            failure_log=f"tick {tick}: v4 {candidate.direction_name} amend exception: {exc}",
            log=log,
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
        failed_decision_id = _LEDGER.log_composite_decision(
            **order_failed_payloads["decision"]
        )
        _LEDGER.log_order_event(
            decision_id=failed_decision_id,
            **order_failed_payloads["order_event"],
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
) -> None:
    fill_price = _tick_resolve_order_fill_price(result, current_price=current_price)
    position_id = _tick_resolve_order_position_id(result, positions_before=positions)
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
    refreshed_positions = bridge.get_positions(getattr(bridge, "symbol", "") or "")
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
    stop_requested=None,
) -> bool:
    def _finalize_nursery(consumed: bool) -> None:
        reservation_id = str(getattr(candidate, "nursery_reservation_id", "") or "")
        if not reservation_id:
            return
        try:
            from backend.services.nursery_exploration_budget import NurseryExplorationBudgetService

            NurseryExplorationBudgetService().finalize(
                reservation_id,
                consumed=consumed,
            )
        except Exception as exc:
            logger.warning("[live] nursery exploration reservation finalize failed: %s", exc)

    final_admission = _probe_final_open_admission(
        bridge=bridge,
        candidate=candidate,
    )
    with _OPEN_TRADE_ADMISSION_LOCK:
        if _open_trade_draining(stop_requested):
            log(f"tick {tick}: v4 open SKIP (loop_draining stage=broker_submit)")
            _finalize_nursery(False)
            return False
        if not bool(final_admission.get("ok")):
            blockers = tuple(final_admission.get("blockers") or ())
            failure_error = str(
                (final_admission.get("postgres") or {}).get("error")
                or (final_admission.get("spot_quote") or {}).get("error")
                or ""
            )
            _persist_safety_fail_closed(
                blockers=blockers,
                source="final_open_admission",
                error=failure_error,
            )
            log(
                f"tick {tick}: v4 open SKIP (final_open_admission "
                f"blockers={','.join(str(item) for item in blockers)})"
            )
            _finalize_nursery(False)
            return False
        try:
            submit_started_at = time.time()
            result = _submit_open_trade_order(bridge, composite, candidate.volume)
            fill_received_at = time.time()
        except Exception as exc:
            log(f"tick {tick}: v4 {candidate.direction_name} order exception: {exc}")
            _finalize_nursery(False)
            return True

    # The market RPC was admitted.  Post-fill resolution, pending protection,
    # SL/TP attach, and ledger/recovery writes must finish even if draining is
    # requested after the RPC returns; process shutdown joins this loop thread.
    broker_open_succeeded = bool(result is not None and getattr(result, "success", False))
    try:
        if broker_open_succeeded:
            _finalize_nursery(True)
            _handle_open_trade_order_success(
                result=result,
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
                submit_started_at=submit_started_at,
                fill_received_at=fill_received_at,
            )
        elif result is not None and not getattr(result, "success", False):
            _finalize_nursery(False)
            _record_open_trade_order_failure(
                result=result,
                cfg=cfg,
                bar=bar,
                account=account,
                positions=positions,
                composite=composite,
                gate_result=gate_result,
                candidate=candidate,
                current_price=current_price,
                tick=tick,
                log=log,
            )
        else:
            _finalize_nursery(False)
            log(f"tick {tick}: v4 {candidate.direction_name} order returned no result")
    except Exception as exc:
        if broker_open_succeeded:
            # Broker risk already exists.  No post-fill exception may fall back
            # to a log-only path: persist no-new-risk first, then refresh broker
            # truth so the serial safety cycle can repair or close the position.
            failure_error = f"{type(exc).__name__}:{exc}"
            _persist_safety_fail_closed(
                blockers=("confirmed_open_post_fill_processing_failed",),
                source="entry_protection_initialization",
                error=failure_error,
            )
            reconcile = _explicit_position_reconcile(bridge)
            if bool(reconcile.get("success")):
                _publish_fresh_position_reconcile(reconcile, broker=broker)
            try:
                append_safety_outbox(
                    event_type="confirmed_open_post_fill_processing_failed",
                    payload={
                        "broker": str(broker or ""),
                        "tick": int(tick),
                        "position_id": int(getattr(result, "position_id", 0) or 0),
                        "intent_id": str(getattr(result, "intent_id", "") or ""),
                        "reconcile_id": str(reconcile.get("reconcile_id") or ""),
                        "reconcile_success": bool(reconcile.get("success")),
                    },
                    error=failure_error,
                )
            except Exception:
                pass
            log(
                f"tick {tick}: v4 {candidate.direction_name} confirmed open post-fill "
                f"processing failed closed: {exc}"
            )
        else:
            _finalize_nursery(False)
            log(f"tick {tick}: v4 {candidate.direction_name} order exception: {exc}")
    return True


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
    _live_state_update(final_open_admission=result)
    return result


def _new_risk_reconciliation_blockers(*, now_ts: float | None = None) -> list[str]:
    """Validate broker facts at the final open-order admission boundary."""

    checked_at = float(time.time() if now_ts is None else now_ts)
    blockers: list[str] = []
    account = _live_state_get("account_reconciled", {}, clone=True) or {}
    account_at = float(_live_state_get("account_updated_at", 0.0) or 0.0)
    account_id = str(_live_state_get("account_reconcile_id", "") or "")
    account_failed_at = float(
        _live_state_get("account_reconcile_failed_at", 0.0) or 0.0
    )
    if not account or not bool(account.get("ok")) or account_at <= 0 or not account_id:
        blockers.append("account_reconcile_unknown")
    elif checked_at < account_at - 1.0 or checked_at - account_at > 15.0:
        blockers.append("account_reconcile_stale")
    if account_failed_at > account_at:
        blockers.append("account_reconcile_failed")

    positions = _live_state_get("positions_reconciled", None, clone=True)
    positions_at = float(_live_state_get("positions_updated_at", 0.0) or 0.0)
    positions_id = str(_live_state_get("positions_reconcile_id", "") or "")
    positions_failed_at = float(
        _live_state_get("positions_reconcile_failed_at", 0.0) or 0.0
    )
    if not isinstance(positions, list) or positions_at <= 0 or not positions_id:
        blockers.append("positions_reconcile_unknown")
    elif checked_at < positions_at - 1.0 or checked_at - positions_at > 15.0:
        blockers.append("positions_reconcile_stale")
    if positions_failed_at > positions_at:
        blockers.append("positions_reconcile_failed")

    return sorted(set(blockers))


def _open_trade_draining(stop_requested=None) -> bool:
    if _process_shutdown_requested:
        return True
    # The durable latch is checked again while the admission lock is held by
    # _submit_open_trade_candidate.  This linearizes emergency activation with
    # the broker open RPC and fails closed if the latch ledger is unreadable.
    if no_new_risk_latched(fail_closed=True):
        return True
    if _generation_controller_enabled():
        if not _LIVE_LOOP_CONTROLLER.accepting_new_risk(_current_generation_id()):
            return True
    if bool(_live_state_get("loop_running", False)):
        # Generation ownership and the live/session fact projection are
        # independent authorities and must both allow admission.  A controller
        # that was ready before a same-tick close cannot override a failed
        # post-close session rebuild.  Safety reductions do not use this gate.
        if not bool(_live_state_get("accepting_new_risk", False)):
            return True
        if str(
            _live_state_get("session_state_status", "unknown") or "unknown"
        ) != "available":
            return True
        if bool(_live_state_get("circuit_breaker", False)):
            return True
        reconcile_blockers = _new_risk_reconciliation_blockers()
        _live_state_update(new_risk_reconcile_blockers=reconcile_blockers)
        if reconcile_blockers:
            return True
    return bool(stop_requested is not None and stop_requested())


def _loop_draining_gate_result(*, tick: int, stage: str, log):
    log(f"tick {tick}: v4 open SKIP (loop_draining stage={stage})")
    return _blocked_open_trade_gate_result("loop_draining")


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
    stop_requested=None,
):
    if not (composite.direction != 0 and gate_result.passed and send):
        return gate_result
    if _open_trade_draining(stop_requested):
        return _loop_draining_gate_result(tick=tick, stage="before_candidate", log=log)
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
        stop_requested=stop_requested,
    )
    if not admitted:
        return _blocked_open_trade_gate_result("loop_draining")
    return gate_result


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
        current_price=current_price,
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
    _write_live_trade_log_factor(
        tick, current_price, acct, pos, None, None, _live_state,
    )
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
    # ── 决策审计: signal ──
    if _DECISION_LOG:
        decision_log_payload = _decision_build_signal_decision_log_payload(
            bar=bar,
            composite=composite,
            gate_result=gate_result,
            tick=tick,
        )
        if decision_log_payload:
            _safe_decision_log(
                _DECISION_LOG,
                run_id=_DECISION_LOG_RUN_ID,
                ts=decision_log_payload["ts"],
                bar_date=decision_log_payload["bar_date"],
                decision_type=decision_log_payload["decision_type"],
                strategy=decision_log_payload["strategy"],
                direction=decision_log_payload["direction"],
                confidence=decision_log_payload["confidence"],
                decision=decision_log_payload["decision"],
                meta=_json.dumps(decision_log_payload["meta"], ensure_ascii=False),
            )

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
    if _LEDGER and composite.direction != 0:
        try:
            _LEDGER.log_composite_decision(
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
                action_reason="signal_detected",
                action_json={"tick": tick},
            )
        except Exception as _ledger_err:
            logger.debug("[live] ledger signal failed: %s", _ledger_err)

    # ── 平仓检测: 对比 _prev_position_ids 找出被 broker 关闭的仓位 ──
    current_pids = _tick_collect_position_ids(pos)
    pending_open_attach_ids = _active_pending_open_attach_ids(current_pids)
    attr_engine = pipeline.get("attribution")
    positions_snapshot_ready = bool(_live_state_get("positions_updated_at", 0.0))
    closed_pids, current_pids, close_detection_deferred = _tick_resolve_closed_position_ids(
        previous_position_ids=_prev_position_ids,
        current_position_ids=current_pids,
        positions_snapshot_ready=positions_snapshot_ready,
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
        current_price=current_price,
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
        stop_requested=stop_requested,
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

    # ── 结构化日志 ──
    _write_live_trade_log_factor(
        tick, current_price, acct, pos, composite, gate_result,
        _live_state,
    )

    # ── 统一持仓保护仲裁: timeout > supervisor > legacy AWE trailing ──
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


# ── AWE 自适应追踪止损 ──────────────────────────────────────

def _update_trailing_stops(
    bridge, pos: list, current_price: float, pipeline: dict,
    atr_price: float, tick: int, log,
) -> list[ProtectionCandidate]:
    """Build legacy AWE trailing-stop candidates without mutating broker state.

    追踪松紧度由 AWE composite_conviction() 动态决定:
        ≥0.7 → 紧追踪 (1.5×ATR), 快速锁利
        0.4~0.7 → 中等 (2.0×ATR)
        <0.4 → 松追踪 (3.0×ATR), 只保本
    """
    global _trailing_state
    candidates: list[ProtectionCandidate] = []
    awe = pipeline.get("awe")
    if awe is None:
        return candidates

    try:
        conviction = awe.composite_conviction()
    except Exception:
        return candidates

    for p in pos:
        try:
            pid = int((p or {}).get("position_id") or (p or {}).get("ticket") or 0)
            anchor = _runtime_config_anchor()
            update = _lifecycle_build_legacy_awe_trailing_update(
                position=dict(p or {}),
                existing_state=_trailing_state.get(pid),
                current_price=current_price,
                atr_price=atr_price,
                conviction=conviction,
                config_version=int(anchor.get("config_version") or 0),
                config_hash=str(anchor.get("config_hash") or ""),
            )
            pid = int(update.get("position_id") or pid or 0)
            if pid <= 0:
                continue
            _trailing_state[pid] = dict(update.get("state") or {})
            if update.get("activated_now"):
                log(f"tick {tick}: trail activated pos={pid} "
                    f"move={float(update.get('price_move') or 0.0):.2f} conviction={conviction:.2f}")
            payload = update.get("candidate")
            if payload:
                candidates.append(ProtectionCandidate(**payload))
        except Exception as _e2:
            logger.debug("[live] trail candidate failed: %s", _e2)
    return candidates


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
        strategy_name=str(_loop_strategy_name or "factor_v4"),
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


def _execute_trailing_candidate(
    candidate: ProtectionCandidate,
    *,
    bridge,
    cfg,
    tick: int,
    log,
    acct: dict | None = None,
) -> bool:
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
        )
        max_holding_seconds = float(close_context.get("max_holding_seconds", 0.0) or 0.0)
        holding_seconds = float(close_context.get("holding_seconds", 0.0) or 0.0)
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
    if not pos or bridge is None or cfg is None:
        return {"timeout": [], "entry_repair": [], "supervisor": [], "trailing_applied": [], "trailing_superseded": []}

    cycle_ts = float(decision_ts if decision_ts is not None else time.time())
    stage_errors: list[dict[str, str]] = []
    selected_candidates: list[SafetyCandidate] = []
    arbitration: list[dict[str, Any]] = []

    def record_selected(candidate: SafetyCandidate, *, priority: int) -> None:
        if any(item.fingerprint == candidate.fingerprint for item in selected_candidates):
            return
        selected_candidates.append(candidate)
        arbitration.append({
            "fingerprint": candidate.fingerprint,
            "decision": "selected",
            "priority": int(priority),
        })

    def record_superseded(candidate: SafetyCandidate, *, priority: int, reason: str) -> None:
        arbitration.append({
            "fingerprint": candidate.fingerprint,
            "decision": "superseded",
            "priority": int(priority),
            "reason": str(reason or ""),
        })

    def record_stage_error(stage: str, exc: Exception, *, position_id: int = 0) -> None:
        logger.warning(
            "[live] protection stage %s failed%s: %s",
            stage,
            f" for pos {position_id}" if position_id else "",
            exc,
        )
        stage_errors.append({
            "stage": stage,
            "position_id": str(int(position_id or 0)),
            "error": f"{type(exc).__name__}: {exc}",
        })
        _record_risk_reduction_aux_failure(
            "position_protection_stage_failed",
            position_id=position_id,
            action=stage,
            error=exc,
        )

    trailing_candidates: list[ProtectionCandidate] = []
    if atr_price > 0:
        try:
            trailing_candidates = _update_trailing_stops(
                bridge,
                pos,
                current_price,
                pipeline,
                atr_price,
                tick,
                log,
            )
        except Exception as exc:
            record_stage_error("trailing_candidate_collection", exc)

    try:
        timeout_handled = _enforce_holding_timeout(
            bridge,
            pos,
            cfg=cfg,
            tick=tick,
            log=log,
            decision_ts=cycle_ts,
            candidate_recorder=lambda candidate: record_selected(candidate, priority=10),
        )
    except Exception as exc:
        record_stage_error("holding_timeout", exc)
        timeout_handled = set()
    try:
        entry_repair_candidates = _entry_protection_repair_candidates(
            pos,
            current_price=current_price,
            tick=tick,
            decision_ts=cycle_ts,
        )
    except Exception as exc:
        record_stage_error("entry_protection_candidate_collection", exc)
        entry_repair_candidates = []
    entry_repair_applied: set[int] = set()
    for candidate in sorted(entry_repair_candidates, key=lambda item: item.priority):
        if candidate.position_id in timeout_handled:
            _log_protection_candidate_superseded(candidate, cfg=cfg, tick=tick, reason="holding_timeout", acct=acct)
            record_superseded(
                protection_candidate_to_safety(candidate),
                priority=20,
                reason="holding_timeout",
            )
            continue
        try:
            if _execute_trailing_candidate(candidate, bridge=bridge, cfg=cfg, tick=tick, log=log, acct=acct):
                entry_repair_applied.add(candidate.position_id)
                record_selected(protection_candidate_to_safety(candidate), priority=20)
        except Exception as exc:
            record_stage_error(
                "entry_protection_execution",
                exc,
                position_id=int(candidate.position_id or 0),
            )

    try:
        supervisor_handled = _run_position_supervision(
            bridge,
            pos,
            cfg=cfg,
            acct=acct,
            tick=tick,
            log=log,
            skip_position_ids=set(timeout_handled) | set(entry_repair_applied),
            decision_ts=cycle_ts,
            candidate_recorder=lambda candidate: record_selected(candidate, priority=30),
            record_partial_close_execution=(
                getattr(pipeline.get("attribution"), "record_partial_close", None)
            ),
        )
    except Exception as exc:
        record_stage_error("position_supervisor", exc)
        supervisor_handled = set()
    protected_pids = set(timeout_handled) | set(entry_repair_applied) | set(supervisor_handled)
    trailing_applied: set[int] = set()
    trailing_superseded: set[int] = set()
    for candidate in sorted(trailing_candidates, key=lambda item: item.priority):
        supersede_reason = _lifecycle_protection_candidate_supersede_reason(
            position_id=candidate.position_id,
            timeout_handled=set(timeout_handled),
            protected_position_ids=protected_pids,
        )
        if supersede_reason:
            trailing_superseded.add(candidate.position_id)
            _log_protection_candidate_superseded(candidate, cfg=cfg, tick=tick, reason=supersede_reason, acct=acct)
            record_superseded(
                protection_candidate_to_safety(candidate),
                priority=50,
                reason=supersede_reason,
            )
            continue
        try:
            if _execute_trailing_candidate(candidate, bridge=bridge, cfg=cfg, tick=tick, log=log, acct=acct):
                trailing_applied.add(candidate.position_id)
                protected_pids.add(candidate.position_id)
                record_selected(protection_candidate_to_safety(candidate), priority=50)
        except Exception as exc:
            record_stage_error(
                "trailing_execution",
                exc,
                position_id=int(candidate.position_id or 0),
            )

    result = _lifecycle_build_position_protection_cycle_result(
        timeout_handled=set(timeout_handled),
        entry_repair_applied=entry_repair_applied,
        supervisor_handled=set(supervisor_handled),
        trailing_applied=trailing_applied,
        trailing_superseded=trailing_superseded,
    )
    if stage_errors:
        result["stage_errors"] = stage_errors
    selected_candidates.sort(key=lambda item: (item.position_id, item.action, item.fingerprint))
    result["safety_candidates"] = [asdict(item) for item in selected_candidates]
    result["safety_arbitration"] = arbitration
    return result


def _write_live_trade_log_factor(
    tick: int, price: float, acct: dict, pos: list,
    composite, gate_result, state: dict,
) -> None:
    """因子管道版结构化审计日志 (写入 DecisionLogStore → PostgreSQL state store)。"""
    try:
        meta = {
            "tick": tick, "price": round(price, 2),
            "balance": acct.get("balance", 0),
            "equity": acct.get("equity", 0),
            "n_positions": len(pos),
            "session_pnl": round(float(state.get("session_pnl", 0)), 2),
            "session_trades": int(state.get("session_trades", 0)),
            "circuit_breaker": bool(state.get("circuit_breaker", False)),
            "v4": True,
        }
        if gate_result:
            meta["gate_result"] = {
                "passed": bool(getattr(gate_result, "passed", False)),
                "reason": str(getattr(gate_result, "reason", "")),
            }
        direction = 0
        confidence = 0.0
        if composite and composite.direction != 0:
            direction = composite.direction
            confidence = composite.score
            meta["signal"] = {
                "direction": composite.direction,
                "score": round(composite.score, 4),
                "tactical_score": round(composite.tactical_score, 4),
                "macro_score": round(composite.macro_score, 4),
                "n_active": composite.n_active_factors,
                "n_abstain": composite.n_abstain_factors,
                "gate": gate_result.reason if gate_result else "",
                "tags": composite.tags_breakdown,
            }
        if _DECISION_LOG:
            _safe_decision_log(
                _DECISION_LOG,
                run_id=_DECISION_LOG_RUN_ID,
                ts=time.time(),
                bar_date="",
                decision_type="signal",
                strategy="factor_v4",
                direction=direction,
                confidence=confidence,
                decision="signal",
                meta=_json.dumps(meta, ensure_ascii=False),
            )
    except Exception as _e2:
        logger.debug("[live] _write_live_trade_log_factor failed: %s", _e2)


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
