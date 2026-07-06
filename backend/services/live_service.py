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
import json
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any

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
from risk.policy_service import RiskPolicyService
from risk.runtime_policy import RiskLimitSnapshot
from backend.services.market_session import evaluate_market_session
from backend.services.live_runtime_state import (
    cache_get_or_refresh as _runtime_cache_get_or_refresh,
    default_live_state,
    state_get as _runtime_state_get,
    state_set as _runtime_state_set,
    state_update as _runtime_state_update,
)
from backend.services.live_ctrader_runtime import CTraderRuntime
from backend.services.live_data_sync_job import make_data_sync_job as _make_data_sync_job
from backend.services.live_loop_shell import (
    adaptive_weight_config as _loop_adaptive_weight_config,
    apply_spot_quote_to_latest_bar as _loop_apply_spot_quote_to_latest_bar,
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
    subscribe_spot_depth_once as _loop_subscribe_spot_depth_once,
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

_local_positions: dict[int, _LocalSLTP] = {}
_local_positions_lock = threading.Lock()
_ENTRY_PROTECTION_PLAN_SCHEMA = "entry_protection_plan.v1"
_ENTRY_PROTECTION_REPAIR_SOURCE = "entry_protection_repair"
_ENTRY_PROTECTION_REPAIR_COOLDOWN_SECONDS = 20.0
_PENDING_OPEN_ATTACH_TTL_SECONDS = 300.0

# P1-d: module-level state for _scheduled_param_tune
_PARAM_TUNE_STATE: dict[str, Any] = {}

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


def _remember_supervisor_reentry_block(
    *,
    position: Any,
    action: str,
    reason: str,
    cfg,
    current_price: float = 0.0,
    tick: int = 0,
) -> None:
    direction = _direction_from_position_payload(position)
    if direction == 0:
        return
    symbol = _position_symbol_value(position)
    cooldown_seconds = _supervisor_reentry_cooldown_seconds(cfg)
    if cooldown_seconds <= 0:
        return
    now = time.time()
    try:
        pid = int(_payload_get(position, "position_id", 0) or _payload_get(position, "ticket", 0) or 0)
    except Exception:
        pid = 0
    payload = _lifecycle_build_supervisor_reentry_block_payload(
        symbol=symbol,
        direction=direction,
        position_id=pid,
        action=action,
        reason=reason,
        started_at=now,
        cooldown_seconds=cooldown_seconds,
        current_price=float(current_price or _payload_get(position, "current_price", 0.0) or 0.0),
        tick=tick,
    )
    with _supervisor_reentry_blocks_lock:
        _supervisor_reentry_blocks[_supervisor_reentry_key(symbol, direction)] = payload


def _active_supervisor_reentry_block(*, symbol: str, direction: int) -> dict[str, Any] | None:
    if int(direction or 0) == 0:
        return None
    key = _supervisor_reentry_key(symbol, direction)
    now = time.time()
    with _supervisor_reentry_blocks_lock:
        block = dict(_supervisor_reentry_blocks.get(key) or {})
        view = _lifecycle_supervisor_reentry_block_view(block, now_ts=now)
        if view is None:
            _supervisor_reentry_blocks.pop(key, None)
            return None
    return view


def _pending_supervisor_reentry_block_from_positions(
    positions: list[Any],
    *,
    symbol: str,
    direction: int,
    cfg,
) -> dict[str, Any] | None:
    if int(direction or 0) == 0:
        return None
    allow_reduce_block = bool(getattr(cfg, "risk_supervisor_reentry_block_reduce", True))
    for position in positions or []:
        pos_direction = _direction_from_position_payload(position)
        if pos_direction != int(direction):
            continue
        if _position_symbol_value(position, symbol) != _position_symbol_value({"symbol": symbol}):
            continue
        supervisor = _payload_get(position, "supervisor", {}) or {}
        action = str((supervisor.get("action") if hasattr(supervisor, "get") else "") or _payload_get(position, "supervisor_action", "") or "").lower()
        reason = str((supervisor.get("summary_reason") if hasattr(supervisor, "get") else "") or _payload_get(position, "supervisor_reason", "") or "")
        evidence = (supervisor.get("evidence") if hasattr(supervisor, "get") else {}) or {}
        thesis_status = str(evidence.get("thesis_status") or _payload_get(position, "thesis_status", "") or "").lower()
        should_block = action == "close" or (allow_reduce_block and action == "reduce") or thesis_status == "broken"
        if not should_block:
            continue
        try:
            pid = int(_payload_get(position, "position_id", 0) or _payload_get(position, "ticket", 0) or 0)
        except Exception:
            pid = 0
        return _lifecycle_build_pending_supervisor_reentry_block_payload(
            symbol=_position_symbol_value(position, symbol),
            direction=direction,
            position_id=pid,
            action=action,
            reason=reason,
            thesis_status=thesis_status,
            remaining_seconds=_supervisor_reentry_cooldown_seconds(cfg),
        )
    return None


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
    risk_snapshot = _live_state_get("risk", {}, clone=True) or {}
    loop_running = bool(_live_state_get("loop_running", True))
    bridge_connected = bool(getattr(bridge, "is_connected", False))
    now = time.time()
    timeframe = str(getattr(cfg, "timeframe", "M5") or "M5")
    runtime_health_context = _loop_collect_open_risk_runtime_health(
        timeframe=timeframe,
        now_ts=now,
        account_updated_at=float(_live_state_get("account_updated_at", 0.0) or 0.0),
        positions_updated_at=float(_live_state_get("positions_updated_at", 0.0) or 0.0),
    )
    temporal_context = _temporal_context_for_trade(
        decision_ts=float(decision_ts or now),
        evaluated_at_ts=now,
        timeframe=timeframe,
        session_last_trade_ts=float(_live_state_get("session_last_trade_ts", 0.0) or 0.0),
        loop_started_at=float(_live_state_get("loop_started_at", 0.0) or 0.0),
    )
    active_supervisor_block = _active_supervisor_reentry_block(symbol=symbol, direction=direction)
    pending_supervisor_block = _pending_supervisor_reentry_block_from_positions(
        positions or [],
        symbol=symbol,
        direction=direction,
        cfg=cfg,
    )
    supervisor_reentry_block = pending_supervisor_block or active_supervisor_block
    entry_cluster_context = _build_entry_cluster_context(
        positions_before=positions or [],
        direction=direction,
        symbol=symbol,
        now_ts=now,
        new_position_id=0,
        new_api_volume=0.0,
    )
    timeframe_seconds = float(temporal_context.get("timeframe_seconds", 0.0) or 0.0)
    same_direction_cooldown_seconds = max(
        60.0,
        float(int(getattr(cfg, "risk_cooldown_bars", 3) or 3)) * (timeframe_seconds or 300.0),
    )

    decision_quality = dict(decision_quality_context or {})
    entry_quality_gate = _entry_quality_gate_from_learning_policy(
        policy=_active_entry_quality_learning_policy(now_ts=now),
        decision_quality=decision_quality,
        signal_score=float(signal_score or 0.0),
    )

    return _lifecycle_build_open_trade_risk_context_payload(
        cfg=cfg,
        acct=acct,
        positions=positions,
        requested_api_volume=requested_api_volume,
        signal_score=signal_score,
        symbol=symbol,
        direction=direction,
        current_price=current_price,
        atr_price=atr_price,
        risk_snapshot=risk_snapshot,
        session_state={
            "pnl": _live_state_get("session_pnl", 0.0),
            "start_balance": _live_state_get("session_start_balance", 0.0),
            "trades": _live_state_get("session_trades", 0),
            "consecutive_losses": _live_state_get("session_consecutive_loss", 0),
            "drawdown_pct": _live_state_get("session_max_drawdown_pct", 0.0),
            "circuit_breaker": _live_state_get("circuit_breaker", False),
        },
        total_api_volume=_tracked_total_api_volume(positions or []),
        event_sizing_context=event_sizing_context,
        event_filter_context=event_filter_context,
        event_window_learning_policy=_active_event_window_learning_policy(now_ts=now),
        entry_quality_gate=entry_quality_gate,
        entry_cluster_context=entry_cluster_context,
        entry_cluster_learning_policy=_active_entry_cluster_learning_policy(now_ts=now),
        same_direction_cooldown_seconds=same_direction_cooldown_seconds,
        max_abs_entry_score=_max_abs_entry_score_for_positions(positions or []),
        loop_running=loop_running,
        bridge_connected=bridge_connected,
        data_lag_seconds=float(runtime_health_context.get("data_lag_seconds", 0.0) or 0.0),
        runtime_health=runtime_health_context.get("runtime_health", {}) or {},
        temporal_context=temporal_context,
        supervisor_reentry_block=supervisor_reentry_block,
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


def _active_entry_cluster_learning_policy(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _ENTRY_CLUSTER_POLICY_CACHE_LOCK:
        cached = _ENTRY_CLUSTER_POLICY_CACHE.get("value") or {}
        if float(_ENTRY_CLUSTER_POLICY_CACHE.get("expires_at") or 0.0) > now:
            return copy.deepcopy(cached)

    controls: list[dict[str, Any]] = []
    try:
        from backend.core.db import get_state_pg_conn

        conn = get_state_pg_conn(read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT suggestion_id, scope_key, action, confidence, reason,
                       evidence_json, reviewed_at, created_at
                FROM policy_suggestion
                WHERE scope_type='entry_cluster'
                  AND status IN ('approved', 'applied')
                  AND action IN ('increase_same_direction_cooldown', 'raise_pyramid_entry_threshold')
                ORDER BY reviewed_at DESC, created_at DESC
                LIMIT 20
                """
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            item = dict(row)
            evidence = item.get("evidence_json") or {}
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence or "{}")
                except Exception:
                    evidence = {}
            scope_key = str(item.get("scope_key") or "")
            threshold = 1
            if scope_key.endswith("_ge_3"):
                threshold = 3
            elif scope_key.endswith("_ge_2"):
                threshold = 2
            elif scope_key.endswith("_ge_1"):
                threshold = 1
            controls.append(
                {
                    "suggestion_id": str(item.get("suggestion_id") or ""),
                    "scope_key": scope_key,
                    "action": str(item.get("action") or ""),
                    "confidence": float(item.get("confidence") or 0.0),
                    "reason": str(item.get("reason") or ""),
                    "min_same_direction_open_count": threshold,
                    "evidence": {
                        "sample_count": (evidence or {}).get("sample_count"),
                        "bad_rate": (evidence or {}).get("bad_rate"),
                        "avg_reward": (evidence or {}).get("avg_reward"),
                    },
                }
            )
    except Exception as exc:
        logger.warning("[live] entry_cluster learning policy unavailable: {}", exc)

    value = {
        "active": bool(controls),
        "controls": controls,
        "min_same_direction_open_count": min(
            [int(item.get("min_same_direction_open_count") or 999) for item in controls] or [0]
        ),
        "source": "policy_suggestion",
        "loaded_at": now,
    }
    with _ENTRY_CLUSTER_POLICY_CACHE_LOCK:
        _ENTRY_CLUSTER_POLICY_CACHE["value"] = copy.deepcopy(value)
        _ENTRY_CLUSTER_POLICY_CACHE["expires_at"] = now + 60.0
    return value


def _active_entry_quality_learning_policy(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _ENTRY_QUALITY_POLICY_CACHE_LOCK:
        cached = _ENTRY_QUALITY_POLICY_CACHE.get("value") or {}
        if float(_ENTRY_QUALITY_POLICY_CACHE.get("expires_at") or 0.0) > now:
            return copy.deepcopy(cached)

    controls: list[dict[str, Any]] = []
    try:
        from backend.core.db import get_state_pg_conn

        conn = get_state_pg_conn(read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT suggestion_id, scope_key, action, confidence, reason,
                       evidence_json, reviewed_at, created_at
                FROM policy_suggestion
                WHERE scope_type='entry_quality'
                  AND status IN ('approved', 'applied')
                  AND action IN (
                      'raise_weak_signal_threshold',
                      'require_factor_agreement',
                      'suppress_recent_worst_factor'
                  )
                ORDER BY reviewed_at DESC, created_at DESC
                LIMIT 50
                """
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            item = dict(row)
            evidence = item.get("evidence_json") or {}
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence or "{}")
                except Exception:
                    evidence = {}
            controls_payload = (evidence or {}).get("recommended_controls") or {}
            controls.append(
                {
                    "suggestion_id": str(item.get("suggestion_id") or ""),
                    "scope_key": str(item.get("scope_key") or ""),
                    "action": str(item.get("action") or ""),
                    "confidence": float(item.get("confidence") or 0.0),
                    "reason": str(item.get("reason") or ""),
                    "min_abs_signal_score": float(controls_payload.get("min_abs_signal_score") or 0.0),
                    "max_factor_conflict_ratio": float(controls_payload.get("max_factor_conflict_ratio") or 0.0),
                    "strong_signal_override": float(controls_payload.get("strong_signal_override") or 0.0),
                    "suppressed_factor": str(controls_payload.get("suppressed_factor") or item.get("scope_key") or ""),
                    "evidence": {
                        "sample_count": (evidence or {}).get("sample_count"),
                        "bad_rate": (evidence or {}).get("bad_rate"),
                        "avg_reward": (evidence or {}).get("avg_reward"),
                        "worst_factor": (evidence or {}).get("worst_factor"),
                    },
                }
            )
    except Exception as exc:
        logger.warning("[live] entry_quality learning policy unavailable: {}", exc)

    value = {
        "active": bool(controls),
        "controls": controls,
        "source": "policy_suggestion",
        "loaded_at": now,
    }
    with _ENTRY_QUALITY_POLICY_CACHE_LOCK:
        _ENTRY_QUALITY_POLICY_CACHE["value"] = copy.deepcopy(value)
        _ENTRY_QUALITY_POLICY_CACHE["expires_at"] = now + 60.0
    return value


def _active_event_window_learning_policy(*, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    with _EVENT_WINDOW_POLICY_CACHE_LOCK:
        cached = _EVENT_WINDOW_POLICY_CACHE.get("value") or {}
        if float(_EVENT_WINDOW_POLICY_CACHE.get("expires_at") or 0.0) > now:
            return copy.deepcopy(cached)

    controls: list[dict[str, Any]] = []
    try:
        from backend.core.db import get_state_pg_conn

        conn = get_state_pg_conn(read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT suggestion_id, scope_key, action, confidence, reason,
                       evidence_json, reviewed_at, created_at
                FROM policy_suggestion
                WHERE scope_type='event_window'
                  AND status IN ('approved', 'applied')
                  AND action IN ('tighten_event_window_sizing', 'extend_event_post_window_review')
                ORDER BY reviewed_at DESC, created_at DESC
                LIMIT 50
                """
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            item = dict(row)
            evidence = item.get("evidence_json") or {}
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence or "{}")
                except Exception:
                    evidence = {}
            scope_key = str(item.get("scope_key") or "")
            event_name, _, window_bucket = scope_key.rpartition(":")
            controls.append(
                {
                    "suggestion_id": str(item.get("suggestion_id") or ""),
                    "scope_key": scope_key,
                    "event_name": event_name,
                    "window_bucket": window_bucket,
                    "action": str(item.get("action") or ""),
                    "confidence": float(item.get("confidence") or 0.0),
                    "reason": str(item.get("reason") or ""),
                    "evidence": {
                        "sample_count": (evidence or {}).get("sample_count"),
                        "bad_rate": (evidence or {}).get("bad_rate"),
                        "avg_reward": (evidence or {}).get("avg_reward"),
                    },
                }
            )
    except Exception as exc:
        logger.warning("[live] event_window learning policy unavailable: {}", exc)

    value = {
        "active": bool(controls),
        "controls": controls,
        "source": "policy_suggestion",
        "loaded_at": now,
    }
    with _EVENT_WINDOW_POLICY_CACHE_LOCK:
        _EVENT_WINDOW_POLICY_CACHE["value"] = copy.deepcopy(value)
        _EVENT_WINDOW_POLICY_CACHE["expires_at"] = now + 60.0
    return value


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
    now_ts = time.time()
    direction = int(getattr(composite, "direction", 0) or 0)
    entry_cluster = _build_entry_cluster_context(
        positions_before=positions_before,
        direction=direction,
        symbol=symbol,
        now_ts=now_ts,
        new_position_id=int(pid or 0),
        new_api_volume=float(actual_api_volume or 0.0),
    )
    market_micro = _market_micro_context_snapshot(
        bridge=bridge,
        current_price=float(current_price or 0.0),
        fill_price=float(fill_price or 0.0),
        direction=direction,
        now_ts=now_ts,
    )
    risk_payload = risk_verdict.to_dict() if hasattr(risk_verdict, "to_dict") else (risk_verdict or {})
    runtime_health = (((risk_payload or {}).get("audit_payload") or {}).get("state") or {}).get("runtime_health") or {}
    return _lifecycle_build_open_learning_context_payload(
        entry_cluster=entry_cluster,
        market_micro=market_micro,
        bar=bar,
        composite=composite,
        total_api_volume_before=_tracked_total_api_volume(positions_before or []),
        actual_api_volume=actual_api_volume,
        requested_volume=requested_volume,
        base_requested_volume=base_requested_volume,
        current_price=current_price,
        fill_price=fill_price,
        sl_price=sl_price,
        tp_price=tp_price,
        sl_dist=sl_dist,
        tp_dist=tp_dist,
        sizing_trace=sizing_trace,
        event_sizing_context=event_sizing_context,
        runtime_health=runtime_health,
        market_session=market_session or _live_state_get("market_session", {}, clone=True) or {},
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
    if cfg is None:
        try:
            from config.runtime_config import shared as _rc

            cfg = _rc()
        except Exception:
            cfg = None
    now = float(decision_ts or time.time())
    open_meta = _lookup_open_decision_context(int(position_id))
    entry_ts = _position_open_timestamp(position) or float(open_meta.get("entry_ts", 0.0) or 0.0)
    timeframe = str(open_meta.get("timeframe") or getattr(cfg, "timeframe", "M5") or "M5")
    temporal_context = _temporal_context_for_trade(
        decision_ts=now,
        timeframe=timeframe,
    )
    max_holding_bars = int(getattr(cfg, "risk_max_holding_bars", 0) or 0)
    return _lifecycle_build_close_position_risk_context_payload(
        position_id=position_id,
        close_reason=close_reason,
        mode=mode,
        broker=broker,
        symbol=symbol,
        entry_ts=entry_ts,
        entry_ts_source=str(open_meta.get("source") or ("broker_position" if position is not None else "")),
        temporal_context=temporal_context,
        max_holding_bars=max_holding_bars,
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


def _position_path_metrics_for_position(
    position: Any,
    *,
    cfg=None,
    now_ts: float | None = None,
    persist: bool = False,
    broker: str = "",
    strategy_name: str = "",
) -> dict[str, Any]:
    pid = _position_id_value(position)
    if pid <= 0:
        return {}

    holding = _holding_summary_for_position(position, cfg=cfg, now_ts=now_ts)
    recovery_row = _load_recovery_position_row(pid)
    entry_ctx = _lookup_open_decision_context(pid)
    inputs = _lifecycle_build_position_path_metrics_inputs(
        position=position,
        recovery_row=recovery_row,
        entry_context=entry_ctx,
        holding_summary=holding,
        current_regime=_current_regime_hint(),
        current_pnl=_position_unrealized_pnl(position),
        now_ts=float(now_ts or time.time()),
        broker=broker,
        strategy_name=strategy_name,
        loop_strategy_name=_loop_strategy_name,
        default_context_integrity=_RECOVERY_CONTEXT_FULL,
    )
    path_update = _lifecycle_build_position_path_metrics_update(
        recovery_meta=inputs["recovery_meta"],
        entry_context=inputs["entry_context"],
        current_pnl=inputs["current_pnl"],
        now_ts=inputs["now_ts"],
        holding_seconds=inputs["holding_seconds"],
        max_holding_seconds=inputs["max_holding_seconds"],
        current_regime=inputs["current_regime"],
        normalize_path_state_fn=normalize_path_state,
        update_position_path_metrics_fn=update_position_path_metrics,
    )

    if persist:
        upsert_defaults = inputs["upsert_defaults"]
        _upsert_recovery_position_state(
            position,
            broker=upsert_defaults["broker"],
            strategy_name=upsert_defaults["strategy_name"],
            status=upsert_defaults["status"],
            context_integrity=upsert_defaults["context_integrity"],
            meta=path_update["next_meta"],
        )
    return path_update["result"]


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
        entry_decision_id=_lookup_entry_decision_id(int(position.get("position_id") or position.get("ticket") or 0)),
        risk_snapshot=_live_state_get("risk", {}, clone=True) or {},
        total_api_volume=_tracked_total_api_volume(positions or []),
        loop_running=bool(_live_state_get("loop_running", True)),
    )
    return _lifecycle_build_position_supervisor_context_payload(
        **context_inputs,
        temporal_context=temporal_context,
        position_metrics=position_metrics,
    )


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
    context = _build_position_supervisor_context(position, cfg=cfg, acct=acct, now_ts=now_ts, positions=positions)
    verdict = evaluate_position_supervisor(context)
    if persist:
        pid = int(position.get("position_id") or position.get("ticket") or 0)
        row = _load_recovery_position_row(pid)
        _upsert_recovery_position_state(
            position,
            **_lifecycle_build_supervisor_state_upsert_payload(
                recovery_row=row,
                verdict=verdict,
                broker=broker,
                strategy_name=strategy_name,
                loop_strategy_name=_loop_strategy_name,
                default_context_integrity=_RECOVERY_CONTEXT_FULL,
            ),
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
    row = _load_recovery_position_row(pid)
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
    row = _load_recovery_position_row(pid)
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


def _supervisor_recently_applied(position_id: int, action: str, cooldown_seconds: float = 300.0) -> bool:
    row = _load_recovery_position_row(position_id)
    meta = dict((row or {}).get("recovery_meta") or {})
    return _lifecycle_supervisor_recently_applied_from_meta(
        recovery_meta=meta,
        action=action,
        now_ts=time.time(),
        cooldown_seconds=cooldown_seconds,
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
    return _lifecycle_build_supervisor_tighten_sl_plan(
        **_lifecycle_build_supervisor_tighten_sl_plan_inputs(
            position=position,
            target_sl=target_sl,
            quote=quote,
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
) -> set[int]:
    handled: set[int] = set()
    skip_position_ids = set(skip_position_ids or set())
    if not pos or bridge is None:
        return handled
    for raw in pos or []:
        position = dict(raw)
        pid = int(position.get("position_id") or position.get("ticket") or 0)
        if pid <= 0:
            continue
        if pid in skip_position_ids:
            handled.add(pid)
            continue
        verdict = _evaluate_position_supervisor_for_position(
            position,
            cfg=cfg,
            acct=acct,
            now_ts=time.time(),
            positions=pos,
            persist=True,
            broker="ctrader",
            strategy_name=str(_loop_strategy_name or "factor_v4"),
        )
        action = str(verdict.get("action") or "hold")
        if action == "hold":
            _log_supervisor_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="evaluated",
                outcome="hold",
                execution_status="not_required",
                acct=acct,
            )
            continue
        handled.add(pid)
        if _supervisor_recently_applied(pid, action):
            _log_supervisor_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="cooldown_skipped",
                outcome="skipped",
                execution_status="cooldown",
                execution_reason="recently_applied_same_action",
                acct=acct,
            )
            continue
        if action == "close" and str(verdict.get("summary_reason") or "") == "holding_timeout_exceeded":
            _delegate_timeout_supervisor_close(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                acct=acct,
            )
            continue

        risk_action = _lifecycle_supervisor_risk_action_for_action(action)
        if not risk_action:
            _log_supervisor_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="invalid_action",
                outcome="skipped",
                execution_status="invalid_action",
                execution_reason=action,
                acct=acct,
            )
            continue
        risk_inputs = _lifecycle_build_supervisor_runtime_risk_evaluation_inputs(
            action=action,
            risk_context=_supervisor_risk_context(position, verdict, cfg=cfg),
            loop_running=bool(_live_state_get("loop_running", True)),
            bridge_connected=bool(getattr(bridge, "is_connected", False)),
        )
        risk_context = risk_inputs.get("risk_context") or {}
        risk_verdict = _RISK_POLICY.evaluate(risk_action, risk_context).to_dict()
        decision_id = _log_supervisor_decision(
            position=position,
            verdict=verdict,
            risk_verdict=risk_verdict,
            acct=acct,
            cfg=cfg,
            event_type=f"supervisor_{action}",
            tick=tick,
        )
        if not risk_verdict.get("allowed", False):
            _log_supervisor_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="risk_rejected",
                outcome="blocked",
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_verdict,
                execution_status="blocked",
                execution_reason=str(risk_verdict.get("reason") or ""),
                acct=acct,
            )
            _remember_supervisor_state(position, verdict, broker="ctrader", strategy_name=str(_loop_strategy_name or "factor_v4"))
            continue

        controls = verdict.get("recommended_controls") or {}
        try:
            if action == "tighten":
                _execute_supervisor_tighten_action(
                    bridge=bridge,
                    position=position,
                    verdict=verdict,
                    risk_action=risk_action,
                    risk_verdict=risk_verdict,
                    decision_id=decision_id,
                    cfg=cfg,
                    tick=tick,
                    acct=acct,
                    controls=controls,
                    log=log,
                    broker="ctrader",
                    strategy_name=str(_loop_strategy_name or "factor_v4"),
                    build_tighten_execution_plan=_lifecycle_build_supervisor_tighten_execution_plan,
                    build_tighten_result_payloads=_lifecycle_build_supervisor_tighten_result_payloads,
                    log_supervisor_position_event=_log_supervisor_position_event,
                    log_supervisor_trace=_log_supervisor_trace,
                    remember_supervisor_state=_remember_supervisor_state,
                    remember_supervisor_reentry_block=_remember_supervisor_reentry_block,
                    track_local_sl_tp=_track_local_sl_tp,
                    result_is_position_not_found=_result_is_position_not_found,
                    retire_broker_missing_position=_retire_broker_missing_position,
                )
            elif action == "reduce":
                _execute_supervisor_reduce_action(
                    bridge=bridge,
                    position=position,
                    verdict=verdict,
                    risk_action=risk_action,
                    risk_verdict=risk_verdict,
                    decision_id=decision_id,
                    cfg=cfg,
                    tick=tick,
                    acct=acct,
                    controls=controls,
                    log=log,
                    ledger=_LEDGER,
                    broker="ctrader",
                    strategy_name=str(_loop_strategy_name or "factor_v4"),
                    floor_api_volume_to_step=_floor_api_volume_to_step,
                    should_full_close_untradeable_reduce=_should_full_close_untradeable_reduce,
                    build_close_position_risk_context=_build_close_position_risk_context,
                    risk_policy_evaluate=_RISK_POLICY.evaluate,
                    log_supervisor_trace=_log_supervisor_trace,
                    remember_supervisor_state=_remember_supervisor_state,
                    remember_supervisor_reentry_block=_remember_supervisor_reentry_block,
                    remember_close_reason=_remember_close_reason,
                    remember_close_verdict=_remember_close_verdict,
                    result_is_position_not_found=_result_is_position_not_found,
                    retire_broker_missing_position=_retire_broker_missing_position,
                )
            elif action == "close":
                _execute_supervisor_close_action(
                    bridge=bridge,
                    position=position,
                    verdict=verdict,
                    risk_action=risk_action,
                    risk_verdict=risk_verdict,
                    decision_id=decision_id,
                    cfg=cfg,
                    tick=tick,
                    acct=acct,
                    controls=controls,
                    log=log,
                    broker="ctrader",
                    strategy_name=str(_loop_strategy_name or "factor_v4"),
                    log_supervisor_trace=_log_supervisor_trace,
                    remember_supervisor_state=_remember_supervisor_state,
                    remember_supervisor_reentry_block=_remember_supervisor_reentry_block,
                    remember_close_reason=_remember_close_reason,
                    remember_close_verdict=_remember_close_verdict,
                    result_is_position_not_found=_result_is_position_not_found,
                    retire_broker_missing_position=_retire_broker_missing_position,
                )
        except Exception as exc:
            _log_supervisor_trace(
                position=position,
                verdict=verdict,
                cfg=cfg,
                tick=tick,
                stage="exception",
                outcome="failed",
                decision_id=decision_id,
                risk_action=risk_action,
                risk_verdict=risk_verdict,
                execution_status="exception",
                execution_reason=str(exc),
                execution={"applied_controls": controls},
                acct=acct,
            )
            logger.debug("[live] supervisor action %s failed for pos %s: %s", action, pid, exc)
    return handled


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


def _save_param_tune_state() -> None:
    """Persist param tune state to the state store + JSON backup."""
    import json, time as _time
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent.parent / "data" / "param_tune_state.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_PARAM_TUNE_STATE, indent=2, default=str))
    except Exception as e:
        logger.warning("Failed to save param tune state: %s", e)

    try:
        conn = _get_state_pg_conn()
        try:
            for key, val in _PARAM_TUNE_STATE.items():
                _state_execute(
                    conn,
                    """
                    INSERT INTO param_tune (key, value_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        updated_at=excluded.updated_at
                    """,
                    (key, json.dumps(val, default=str), _time.time())
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


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
_ACCOUNT_REFRESH_MIN_INTERVAL = 15.0
_POSITION_RECONCILE_MIN_INTERVAL = 120.0
_DATA_SYNC_LOCK = threading.Lock()


def _live_state_get(key: str, default=None, *, clone: bool = False):
    return _runtime_state_get(_live_state, _LIVE_STATE_LOCK, key, default, clone=clone)


def _live_state_set(key: str, value) -> None:
    _runtime_state_set(_live_state, _LIVE_STATE_LOCK, key, value)


def _live_state_update(**kwargs) -> None:
    _runtime_state_update(_live_state, _LIVE_STATE_LOCK, **kwargs)


def _get_state_pg_conn():
    from backend.core.db import get_state_pg_conn

    return get_state_pg_conn()


def _get_state_read_conn():
    from backend.core.db import get_state_pg_conn

    return get_state_pg_conn(read_only=True)


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
    return {
        "trade_date": trade_date,
        "session_pnl": float(_live_state_get("session_pnl", 0.0) or 0.0),
        "session_trades": int(_live_state_get("session_trades", 0) or 0),
        "session_winning": int(_live_state_get("session_winning", 0) or 0),
        "session_losing": int(_live_state_get("session_losing", 0) or 0),
        "session_trade_pnls": list(_live_state_get("session_trade_pnls", [], clone=True) or [])[-200:],
        "session_consecutive_loss": int(_live_state_get("session_consecutive_loss", 0) or 0),
        "session_max_drawdown_pct": float(_live_state_get("session_max_drawdown_pct", 0.0) or 0.0),
        "session_peak_equity": float(_live_state_get("session_peak_equity", 0.0) or 0.0),
        "session_start_balance": float(_live_state_get("session_start_balance", 0.0) or 0.0),
        "session_last_trade_ts": float(_live_state_get("session_last_trade_ts", 0.0) or 0.0),
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


def _restore_session_state_for_day(trade_date: str | None = None) -> bool:
    if not trade_date:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = _runtime_kv_get(_session_state_key(trade_date), {}) or {}
    if not isinstance(state, dict) or state.get("trade_date") != trade_date:
        return False
    _live_state_update(
        session_pnl=float(state.get("session_pnl", 0.0) or 0.0),
        session_trades=int(state.get("session_trades", 0) or 0),
        session_winning=int(state.get("session_winning", 0) or 0),
        session_losing=int(state.get("session_losing", 0) or 0),
        session_trade_pnls=list(state.get("session_trade_pnls") or [])[-200:],
        session_consecutive_loss=int(state.get("session_consecutive_loss", 0) or 0),
        session_max_drawdown_pct=float(state.get("session_max_drawdown_pct", 0.0) or 0.0),
        session_peak_equity=float(state.get("session_peak_equity", 0.0) or 0.0),
        session_start_balance=float(state.get("session_start_balance", 0.0) or 0.0),
        session_last_trade_ts=float(state.get("session_last_trade_ts", 0.0) or 0.0),
        circuit_breaker=bool(state.get("circuit_breaker", False)),
        circuit_reason=str(state.get("circuit_reason", "") or ""),
        trade_equity_history=list(state.get("trade_equity_history") or [])[-500:],
    )
    return True


def _remember_close_reason(position_id: int, reason: str) -> None:
    _lifecycle_remember_close_reason(
        pending_reasons=_pending_close_reasons,
        merge_recovery_meta=_merge_recovery_position_meta,
        position_id=position_id,
        reason=reason,
    )


def _consume_close_reason(position_id: int, default: str = "broker_close") -> str:
    return _lifecycle_consume_close_reason(
        pending_reasons=_pending_close_reasons,
        load_recovery_row=_load_recovery_position_row,
        position_id=position_id,
        default=default,
    )


def _remember_close_verdict(position_id: int, verdict) -> None:
    _lifecycle_remember_close_verdict(
        pending_verdicts=_pending_close_verdicts,
        merge_recovery_meta=_merge_recovery_position_meta,
        position_id=position_id,
        verdict=verdict,
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
) -> None:
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

    _record_session_trade(total_pnl)
    _mark_recovery_position_closed(
        position_id,
        close_reason="restart_replay",
        close_pnl=total_pnl,
        closed_at=close_ts,
        meta=payloads["recovery_meta"],
    )

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
    positions = _live_state_get("positions", [], clone=True) or []
    payload = _lifecycle_filter_removed_live_position(positions, position_id=pid)
    if payload["removed"]:
        _live_state_update(positions=payload["positions"], positions_updated_at=time.time())
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
            ).get(pid)
        finally:
            write_conn.close()
    except Exception as exc:
        logger.debug("[live] missing-position deal sync failed for pos %s: %s", pid, exc)

    _replay_recovered_close(
        broker=broker,
        position_id=pid,
        position_state=position_state,
        real_pnl=real_pnl,
        strategy_name=strategy_name,
    )
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
    if hasattr(bridge, "is_connected") and not bridge.is_connected:
        raise RuntimeError("broker not connected")
    if hasattr(bridge, "refresh_positions"):
        has_reconcile_ts = hasattr(bridge, "_last_reconcile_at")
        before_reconcile = float(getattr(bridge, "_last_reconcile_at", 0.0) or 0.0)
        try:
            positions = bridge.refresh_positions(force=True, allow_cache_fallback=False)
        except TypeError:
            positions = bridge.refresh_positions()
        after_reconcile = float(getattr(bridge, "_last_reconcile_at", 0.0) or 0.0)
        if has_reconcile_ts and after_reconcile <= before_reconcile:
            raise RuntimeError("fresh broker reconcile unavailable")
        return positions or []
    return bridge.get_positions() or []


def _bootstrap_position_recovery(
    bridge,
    *,
    broker: str,
    strategy_name: str,
    log,
) -> bool:
    global _prev_position_ids

    try:
        current_positions = _read_positions_for_recovery(bridge)
    except Exception as exc:
        log(f"recovery bootstrap skipped: get_positions failed: {exc}")
        return False

    normalized = [_normalize_position_snapshot(pos) for pos in current_positions]
    coerced_positions = _coerce_live_positions(current_positions)
    current_ids = {item["position_id"] for item in normalized if item["position_id"] > 0}
    active_rows = _list_active_recovery_positions(broker)
    if not current_ids:
        _live_state_update(positions=[], positions_updated_at=time.time())
        _prev_position_ids = set()
        suffix = f" while {len(active_rows)} persisted positions remain" if active_rows else ""
        if active_rows:
            zero_count = _recovery_zero_confirmations.get(broker, 0) + 1
            _recovery_zero_confirmations[broker] = zero_count
            if zero_count < _RECOVERY_ZERO_CONFIRMATIONS_REQUIRED:
                log(
                    "recovery bootstrap deferred: broker returned 0 positions"
                    f"{suffix}; confirmation {zero_count}/{_RECOVERY_ZERO_CONFIRMATIONS_REQUIRED}"
                )
                return False

            from execution.deal_sync import sync_close_deals_batch

            missing_ids = _lifecycle_recovery_active_position_ids(active_rows)
            lookback_from = _lifecycle_recovery_replay_lookback_from(
                active_rows=active_rows,
                replay_ids=missing_ids,
                now_ts=time.time(),
                lookback_sec=_RECOVERY_REPLAY_LOOKBACK_SEC,
            )
            conn = _get_state_pg_conn()
            try:
                replayed = sync_close_deals_batch(
                    bridge,
                    conn,
                    missing_ids,
                    from_ts=lookback_from,
                    max_rows=500,
                )
            finally:
                conn.close()
            for row in active_rows:
                position_id = int(row["position_id"])
                _replay_recovered_close(
                    broker=broker,
                    position_id=position_id,
                    position_state=row,
                    real_pnl=replayed.get(position_id),
                    strategy_name=strategy_name,
                )
            log(f"recovery bootstrap reconciled {len(active_rows)} persisted positions as closed after broker returned 0")
            return True
        _recovery_zero_confirmations.pop(broker, None)
        log(f"recovery bootstrap deferred: broker returned 0 positions{suffix}")
        return False
    _recovery_zero_confirmations.pop(broker, None)
    missing_ids = _lifecycle_recovery_missing_position_ids(
        active_rows=active_rows,
        current_ids=current_ids,
    )

    if missing_ids:
        from execution.deal_sync import sync_close_deals_batch

        lookback_from = _lifecycle_recovery_replay_lookback_from(
            active_rows=active_rows,
            replay_ids=missing_ids,
            now_ts=time.time(),
            lookback_sec=_RECOVERY_REPLAY_LOOKBACK_SEC,
        )
        conn = _get_state_pg_conn()
        try:
            replayed = sync_close_deals_batch(
                bridge,
                conn,
                missing_ids,
                from_ts=lookback_from,
                max_rows=500,
            )
        finally:
            conn.close()
        for row in active_rows:
            position_id = int(row["position_id"])
            if position_id in missing_ids:
                _replay_recovered_close(
                    broker=broker,
                    position_id=position_id,
                    position_state=row,
                    real_pnl=replayed.get(position_id),
                    strategy_name=strategy_name,
                )
        log(f"recovery bootstrap replayed {len(missing_ids)} missing closes")

    for item in normalized:
        position_id = item["position_id"]
        if position_id <= 0:
            continue
        _pos_open_prices[position_id] = item["open_price"]
        _pos_open_api_volume[position_id] = item["volume"]
        _upsert_recovery_position_state(
            item["raw"],
            broker=broker,
            strategy_name=strategy_name,
            status="recovered",
            meta={"recovered_at": time.time()},
        )

    if coerced_positions:
        _live_state_update(
            positions=coerced_positions,
            positions_updated_at=time.time(),
        )
    _prev_position_ids = current_ids.copy()
    if current_ids:
        log(f"recovery bootstrap attached {len(current_ids)} live positions after restart")
    return True


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
        session_consecutive_loss=0,
        session_max_drawdown_pct=0.0,
        session_start_balance=start_balance,
        session_last_trade_ts=0.0,
    )
    _persist_session_state()


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


def _record_session_trade(total_pnl: float) -> dict:
    with _LIVE_STATE_LOCK:
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
        _live_state.update(
            session_trades=trades,
            session_winning=winning,
            session_losing=losing,
            session_trade_pnls=trade_pnls,
            session_consecutive_loss=consecutive_loss,
            session_pnl=session_pnl,
            session_last_trade_ts=time.time(),
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
) -> None:
    _live_state_update(
        broker=broker,
        loop_running=True,
        loop_strategy=strategy_name,
        loop_started_at=started_at,
        account=account,
        account_updated_at=time.time(),
    )
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not _restore_session_state_for_day(today_str):
        _reset_session_state_for_new_day()



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
    """从 .env 构造 CTraderBridge, 支持 kwargs 覆盖.
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
    try:
        from config import load_config
        cfg = load_config()
        ctrader_cfg = cfg.get("ctrader", {}) if isinstance(cfg, dict) else {}
    except Exception:
        ctrader_cfg = {}
    try:
        from config.runtime_config import shared as _runtime_cfg

        runtime_cfg = _runtime_cfg()
    except Exception:
        runtime_cfg = None
    kw = dict(
        client_id=os.getenv("CTRADER_CLIENT_ID", ""),
        client_secret=os.getenv("CTRADER_CLIENT_SECRET", ""),
        access_token=os.getenv("CTRADER_ACCESS_TOKEN", ""),
        account_id=int(os.getenv("CTRADER_ACCOUNT_ID", "0")),
        host=str(os.getenv("CTRADER_HOST", ctrader_cfg.get("host", "demo.ctraderapi.com")) or "demo.ctraderapi.com"),
        port=int(os.getenv("CTRADER_PORT", ctrader_cfg.get("port", 5035)) or 5035),
        symbol=str(os.getenv("CTRADER_SYMBOL", ctrader_cfg.get("symbol", "XAUUSD")) or "XAUUSD"),
        request_timeout_sec=float(
            os.getenv("CTRADER_REQUEST_TIMEOUT_SEC", ctrader_cfg.get("request_timeout_sec", 10)) or 10
        ),
        proxy_url=str(
            os.getenv("CTRADER_PROXY_URL", ctrader_cfg.get("proxy_url", "")) or ""
        ),
        proxy_rdns=str(
            os.getenv("CTRADER_PROXY_RDNS", ctrader_cfg.get("proxy_rdns", True))
        ).strip().lower() not in {"0", "false", "no", "off"},
        l2_persist_enabled=bool(getattr(runtime_cfg, "l2_collection_enabled", True)),
        l2_snapshot_interval_sec=float(getattr(runtime_cfg, "l2_snapshot_interval_sec", 5.0) or 5.0),
        l2_write_batch_size=int(getattr(runtime_cfg, "l2_write_batch_size", 1000) or 1000),
        l2_write_flush_interval_sec=float(getattr(runtime_cfg, "l2_write_flush_interval_sec", 1.0) or 1.0),
    )
    kw.update(overrides)
    bridge = CTraderBridge(**kw)
    _install_ctrader_live_listener(bridge)
    return bridge, None


def _apply_l2_runtime_config(bridge) -> None:
    if bridge is None:
        return
    try:
        from config.runtime_config import shared as _runtime_cfg

        runtime_cfg = _runtime_cfg()
    except Exception:
        return
    try:
        bridge.l2_persist_enabled = bool(getattr(runtime_cfg, "l2_collection_enabled", True))
        bridge.l2_snapshot_interval_sec = max(0.0, float(getattr(runtime_cfg, "l2_snapshot_interval_sec", 5.0) or 5.0))
        bridge.l2_write_batch_size = max(1, int(getattr(runtime_cfg, "l2_write_batch_size", 1000) or 1000))
        bridge.l2_write_flush_interval_sec = max(0.1, float(getattr(runtime_cfg, "l2_write_flush_interval_sec", 1.0) or 1.0))
    except Exception as exc:
        logger.debug("[ctrader] l2 runtime config apply skipped: %s", exc)


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
                    positions = _live_state_get("positions", [], clone=True) or []
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
                        positions=patched_positions or positions,
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
                _live_state_update(account=account, account_updated_at=now_ts)
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
                    persist=True,
                    broker="ctrader",
                    strategy_name=str(_loop_strategy_name or "factor_v4"),
                )
                _live_state_update(positions=enriched, positions_updated_at=now_ts)
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
        apply_runtime_config=_apply_l2_runtime_config,
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
    quote_changed_at = float((quote or {}).get("changed_at") or _live_state_get("spot_quote_changed_at", 0.0) or 0.0)
    if quote:
        quote = {**quote, "changed_at": quote_changed_at}
    positions = _live_state_get("positions", [], clone=True) or []
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
    return state


def _ensure_spot_subscription(
    bridge,
    *,
    require_l2_depth: bool = False,
    l2_collection_enabled: bool = False,
    log=None,
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
    depth_needed = bool(require_l2_depth or l2_collection_enabled) and hasattr(bridge, "subscribe_depth") and not bool(getattr(bridge, "_depth_subscribed", False))
    if not spot_needed and not depth_needed:
        return
    if now_ts - _last_spot_subscription_attempt_ts < 60:
        return
    _last_spot_subscription_attempt_ts = now_ts
    try:
        if spot_needed and hasattr(bridge, "subscribe_spots"):
            bridge.subscribe_spots()
        if depth_needed:
            bridge.subscribe_depth()
        msg = "market subscriptions refreshed after broker connection became ready"
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
    loop = loop_status()
    diag = _live_state_get("_diag", {}, clone=True) or {}
    account = _live_state_get("account", {}, clone=True) or {}
    account_updated_at = float(_live_state_get("account_updated_at", 0.0) or 0.0)
    positions_updated_at = float(_live_state_get("positions_updated_at", 0.0) or 0.0)
    positions = _coerce_live_positions(_live_state_get("positions", clone=True))

    broker_status = "unknown"
    broker_error = None
    if broker == "ctrader":
        broker_status, broker_error = _probe_ctrader()

    loop_running = bool(loop.get("running"))
    account_ready = bool(account and account.get("ok") and account_updated_at > 0)
    positions_ready = positions_updated_at > 0
    bridge_ready = bool(diag.get("bridge_ready"))

    state = "idle"
    if loop_running:
        if bridge_ready and account_ready and positions_ready:
            state = "ready"
        elif broker_status in {"connected", "warming_up"}:
            state = "warming_up"
        else:
            state = "degraded"
    elif broker_status == "connected":
        state = "idle_connected"
    elif broker_status == "warming_up":
        state = "warming_up"
    elif broker_status in {"disconnected", "error", "no_token"}:
        state = "degraded"

    reasons: list[str] = []
    if not bridge_ready and loop_running:
        reasons.append("bridge_not_ready")
    if not account_ready:
        reasons.append("account_not_ready")
    if positions_updated_at <= 0:
        reasons.append("positions_never_synced")
    elif len(positions) == 0:
        reasons.append("positions_empty")
    if broker_error:
        reasons.append("broker_error")

    return {
        "state": state,
        "broker_status": broker_status,
        "broker_error": broker_error,
        "loop_running": loop_running,
        "bridge_ready": bridge_ready,
        "account_ready": account_ready,
        "positions_ready": positions_ready,
        "account_updated_at": account_updated_at or None,
        "positions_updated_at": positions_updated_at or None,
        "positions_count": len(positions),
        "positions": positions,
        "reasons": reasons,
    }


def get_account(broker: str) -> dict:
    """Read real broker account info. Returns dict with at minimum
    {ok, broker, balance, equity, margin, leverage, currency, error}.

    audit 2026-06-09: 如果 live loop 在跑这个 broker, 短路返回 _live_state 缓存,
    避免重复打 broker (Twisted reactor callFromThread 会阻塞主线程 50-200ms,
    直接卡前端 HTTP 请求). Loop 自己的 tick 已经每 60s 刷新 _live_state."""
    readiness = get_live_readiness(broker)
    # ── 缓存短路: loop 在跑 → 只读 _live_state ──
    if _live_state_get("loop_running") and _live_state_get("broker") == broker:
        acct = _live_state_get("account", clone=True)
        if acct and acct.get("ok"):
            result = dict(acct)
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
            info = bridge.account_info()
            if not info:
                return {"ok": False, "broker": "ctrader", "error": "account_info returned empty"}
            if not isinstance(info, dict):
                from dataclasses import asdict
                info_dict = asdict(info)
            else:
                info_dict = info
            info_dict.setdefault("ok", True)
            info_dict.setdefault("broker", "ctrader")
            # ★ 写入 _live_state, 让 WS /ws/state 立即看到数据 (不依赖 live loop)
            _live_state_update(account=info_dict, account_updated_at=time.time())
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
            account=_live_state_get("account", {}, clone=True) or {},
        )

    if _live_state_get("loop_running") and _live_state_get("broker") == broker:
        if readiness["positions_ready"]:
            return {
                "ok": True,
                "broker": broker,
                "positions": _enrich_positions(readiness["positions"]),
                "warming_up": False,
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
        cached_positions = _live_state_get("positions", clone=True)
        if cached_positions is not None and _live_state_get("loop_running"):
            return {"ok": True, "broker": "ctrader", "positions": _enrich_positions(cached_positions), "readiness": readiness}
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
            raw = bridge.get_positions(symbol)
            positions = []
            for p in raw:
                api_volume = _position_api_volume(p)
                item = {
                    "ticket": p.get("position_id"),
                    "symbol": p.get("symbol"),
                    "type": p.get("type"),
                    "volume": api_volume,
                    "api_volume": api_volume,
                    "price_open": p.get("price_open", 0.0),
                    "price_current": p.get("price_current", p.get("price_open", 0.0)),
                    "sl": p.get("sl", 0.0),
                    "tp": p.get("tp", 0.0),
                    "profit": p.get("profit") or 0.0,
                    "swap": p.get("swap", 0.0),
                    "commission": p.get("commission", 0.0),
                    "magic": p.get("magic"),
                    "open_time": p.get("open_timestamp", 0),
                }
                item.update(_holding_summary_for_position(item, cfg=cfg))
                positions.append(item)
            # ★ 写入 _live_state, 让 WS /ws/state 立即看到数据 (不依赖 live loop)
            _live_state_update(positions=positions, positions_updated_at=time.time())
            return {"ok": True, "broker": "ctrader", "positions": positions, "readiness": get_live_readiness("ctrader")}
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
# ★ v9-fix: 重启退避 + 价格僵死检测 + 备份 bar 缓存
_last_loop_end: float = 0.0
_MIN_RESTART_INTERVAL = 60  # 最小重启间隔 60s
_BAR_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / ".bar_cache.pkl"
_PRICE_STUCK_WARNED: dict[str, float] = {}  # {(broker,tf): last_price}


def _scheduled_param_tune():
    """Daily legacy parameter sweep.

    This job is observation-only now. Runtime parameter changes must flow
    through parameter templates and governance, not a direct RuntimeConfig
    patch from a legacy grid search.
    """
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parent.parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

    try:
        from scripts.tune_strategy_params import run_single_backtest
    except ImportError:
        logger.warning("[param_tune] tune_strategy_params.py not found, skip")
        return

    light_grid = {
        "strategy_rsi_period": [7, 14, 21],
        "strategy_sl_atr": [1.5, 2.0, 3.0],
        "strategy_tp_atr": [2.0, 3.0, 4.0],
        "strategy_votes_needed": [1.5, 2.0],
        "strategy_cooldown_bars": [1, 3],
    }
    import itertools
    keys = list(light_grid.keys())
    combos = list(itertools.product(*light_grid.values()))

    logger.info(f"[param_tune] starting sweep: {len(combos)} combos")
    best = None
    for i, combo in enumerate(combos):
        params = dict(zip(keys, combo))
        r = run_single_backtest(params, n_bars=3000, dry_run=False)
        if r.error:
            continue
        if best is None or (r.sharpe > 0 and (best.sharpe <= 0 or r.sharpe > best.sharpe)):
            best = r

    if best is None or best.n_trades < 5:
        logger.warning("[param_tune] no valid result, keeping current params")
        return

    logger.info(
        f"[param_tune] candidate: rsi={best.params.get('strategy_rsi_period')} "
        f"sl={best.params.get('strategy_sl_atr')} tp={best.params.get('strategy_tp_atr')} "
        f"PnL={best.net_pnl:.1f} WR={best.win_rate:.0f}% Sharpe={best.sharpe:.2f}"
    )
    # 记录运行时间
    _PARAM_TUNE_STATE["last_run_ts"] = time.time()
    _save_param_tune_state()

    try:
        from monitor.evolution_story import EvolutionStory
        EvolutionStory.shared().append(
            event_type="param_tune_candidate",
            payload={"best_params": best.params, "pnl": round(best.net_pnl, 2),
                  "sharpe": round(best.sharpe, 2), "n_combos": len(combos), "applied": False}
        )
    except Exception as _e:
        logger.debug("[param_tune] EvolutionStory.append failed: %s", _e)


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
                            logger.debug("[awe_adapt] ictracker.update failed for %s: %s", fname, _e)
            except Exception as _e2:
                logger.debug("[awe_adapt] export_factor_history failed: %s", _e2)

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
                logger.debug("[awe_adapt] blend_baseline compute failed: %s", _e2)

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
            # ★ 通过 DecisionPolicy 融合后再写 (保持一致性)
            try:
                from alpha.decision_policy import DecisionPolicy
                dp = DecisionPolicy()
                decisions = dp.fast_decide(
                    awe_patches=patches,
                    weight_policy_weights=None,
                    factor_configs=factor_configs,
                    current_weights=current_weights,
                )
                partial = DecisionPolicy.to_weights(decisions)
                merged = dict(current_weights)
                merged.update(partial)
                missing = set(current_weights) - set(merged)
                if missing:
                    logger.warning(
                        "[awe_adapt] refusing partial weight patch; missing=%s",
                        sorted(missing)[:20],
                    )
                    return
                from backend.services.runtime_config_mutation import RuntimeConfigMutationService

                RuntimeConfigMutationService().apply_patch(
                    {"factor_portfolio_weights": merged},
                    source="awe_decision_policy_update_weight",
                    run_id=f"awe_adapt_{int(time.time())}",
                    actor="system:awe_adapt",
                    action="update_weight",
                    reason="AWE weight patch merged by DecisionPolicy",
                )
                logger.info(
                    "[awe_adapt] weights pushed via DecisionPolicy (%d changed, %d total)",
                    len(partial),
                    len(merged),
                )
            except Exception as _e2:
                logger.warning("[awe_adapt] DecisionPolicy weight push failed: %s", _e2)
        else:
            logger.debug("[awe_adapt] no weight changes needed")
    except Exception as e:
        logger.warning("[awe_adapt] failed: {}", e)




# ═══════════════════════════════════════════════════════════
# Phase 3: 特征工程自动化
# ═══════════════════════════════════════════════════════════

def _scheduled_feature_engineering():
    """每天凌晨 3:00: 重新衍生特征 + PCA 压缩 + 特征筛选。

    1. 加载最近 20,000 bars
    2. 计算所有因子值
    3. FeatureDeriver → 200+ 衍生特征
    4. PCA → 压缩到 ~15 个正交因子
    5. FeatureSelector → 筛选最优子集
    6. 注册 pca_0..pca_N 因子到 factor_registry
    """
    try:
        from data.store import DataStore
        from alpha.features.selector import run_feature_selection
        from alpha.registry import factor_registry
        from monitor.evolution_story.report import EvolutionStory

        store = DataStore()
        df = store.load_bars("XAUUSD+", "M5", limit=20000)
        if df.empty or len(df) < 1000:
            logger.info("[fe] insufficient bars: %d", len(df))
            return

        # 预计算因子值
        factor_vals: dict[str, "np.ndarray"] = {}
        for name in factor_registry.list():
            try:
                fn = factor_registry.get(name)
                if fn is None:
                    continue
                vals = fn(df)
                arr = _np.asarray(vals, dtype=float)
                arr[_np.isinf(arr)] = _np.nan
                factor_vals[name] = arr
            except Exception:
                continue

        # Forward returns
        close = df["close"].values.astype(float)
        fwd_ret = _np.full(len(close), _np.nan)
        fwd_ret[:-1] = (close[1:] - close[:-1]) / close[:-1]

        # 运行特征工程
        result = run_feature_selection(df, fwd_ret, factor_vals)
        logger.info(
            "[fe] done: %d derived → %d pca (%.0f%%) → %d selected / %d candidates",
            result.get("n_derived", 0),
            result.get("pca_n_components", 0),
            result.get("pca_variance", 0) * 100,
            result.get("n_selected", 0),
            result.get("n_candidates", 0),
        )

        # 记录
        try:
            story = EvolutionStory.shared() if hasattr(EvolutionStory, "shared") else None
            if story:
                story.append(event_type="feature_engineering", payload={
                    "n_selected": result.get("n_selected"),
                    "n_candidates": result.get("n_candidates"),
                    "pca_n": result.get("pca_n_components"),
                    "pca_var": result.get("pca_variance"),
                })
        except Exception as _e:
            logger.debug("[fe] EvolutionStory.append failed: %s", _e)
    except Exception as e:
        logger.warning(f"[fe] failed: {e}", exc_info=True)


def _env_enabled(name: str, default: str = "1") -> bool:
    value = str(os.getenv(name, default) or "").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _ensure_offmarket_high_load_audit_table(conn) -> None:
    _state_execute(
        conn,
        """
        CREATE TABLE IF NOT EXISTS offmarket_high_load_job_audit (
            audit_id TEXT PRIMARY KEY,
            job_name TEXT NOT NULL,
            status TEXT NOT NULL,
            session_status TEXT DEFAULT '',
            high_load_profile TEXT DEFAULT '',
            payload_json TEXT DEFAULT '{}',
            result_json TEXT DEFAULT '{}',
            error TEXT DEFAULT '',
            started_at REAL NOT NULL,
            finished_at REAL NOT NULL
        )
        """
    )
    _state_execute(
        conn,
        """
        CREATE INDEX IF NOT EXISTS idx_offmarket_high_load_job_audit_created
        ON offmarket_high_load_job_audit(started_at)
        """
    )


def _record_offmarket_high_load_audit(
    *,
    job_name: str,
    status: str,
    session: dict[str, Any],
    payload: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str = "",
    started_at: float | None = None,
) -> dict[str, Any]:
    now_ts = time.time()
    started = float(started_at or now_ts)
    audit_id = f"{job_name}:{int(started * 1000)}"
    row = {
        "audit_id": audit_id,
        "job_name": job_name,
        "status": status,
        "session_status": str((session or {}).get("status") or ""),
        "high_load_profile": str((session or {}).get("high_load_profile") or "disabled"),
        "payload": payload or {},
        "result": result or {},
        "error": str(error or ""),
        "started_at": started,
        "finished_at": now_ts,
    }
    conn = _get_state_pg_conn()
    try:
        _ensure_offmarket_high_load_audit_table(conn)
        _state_execute(
            conn,
            """
            INSERT INTO offmarket_high_load_job_audit
            (audit_id, job_name, status, session_status, high_load_profile,
             payload_json, result_json, error, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_id) DO UPDATE SET
                job_name=excluded.job_name,
                status=excluded.status,
                session_status=excluded.session_status,
                high_load_profile=excluded.high_load_profile,
                payload_json=excluded.payload_json,
                result_json=excluded.result_json,
                error=excluded.error,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at
            """,
            (
                audit_id,
                row["job_name"],
                row["status"],
                row["session_status"],
                row["high_load_profile"],
                json.dumps(row["payload"], ensure_ascii=False, sort_keys=True),
                json.dumps(row["result"], ensure_ascii=False, sort_keys=True),
                row["error"],
                row["started_at"],
                row["finished_at"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return row


def _offmarket_high_load_allowed(session: dict[str, Any]) -> tuple[bool, str]:
    status = str((session or {}).get("status") or "")
    if status not in {"closed_confirmed", "closed_pending_positions"}:
        return False, f"market_session_not_offmarket:{status or 'unknown'}"
    if not bool((session or {}).get("high_load_allowed", False)):
        return False, "high_load_not_allowed"
    return True, "ok"


def _scheduled_offmarket_position_quality_lightgbm() -> dict[str, Any]:
    """Off-market LightGBM sidecar training.

    This is strictly advisory/shadow-only. It never places orders, closes
    positions, changes risk limits, or touches cTrader execution.
    """
    job_name = "offmarket_position_quality_lightgbm"
    started_at = time.time()
    session = _live_state_get("market_session", {}, clone=True) or {}
    if not session:
        session = _market_session_snapshot(None)
    allowed, reason = _offmarket_high_load_allowed(session)
    profile = str(session.get("high_load_profile") or "disabled")
    payload = {
        "job_name": job_name,
        "market_session": session,
        "limit": 500 if profile == "full" else 250,
        "shadow_limit": 100 if profile == "full" else 30,
        "min_samples": 20,
        "profile": profile,
    }
    if not allowed:
        result = {"ok": False, "skipped": True, "reason": reason}
        audit = _record_offmarket_high_load_audit(
            job_name=job_name,
            status="skipped",
            session=session,
            payload=payload,
            result=result,
            started_at=started_at,
        )
        logger.info("[offmarket_high_load] {} skipped: {}", job_name, reason)
        return {"ok": True, "skipped": True, "reason": reason, "audit": audit}

    try:
        from backend.core.db import STATE_DB
        from research.position_quality_lightgbm import PositionQualityLightGBMService

        service = PositionQualityLightGBMService(db_path=STATE_DB)
        train_result = service.train(
            limit=int(payload["limit"]),
            holdout_ratio=0.2,
            min_samples=int(payload["min_samples"]),
            register=True,
            symbol="XAUUSD+",
            timeframe="M5",
        )
        result: dict[str, Any] = {"train": train_result}
        if train_result.get("ok"):
            result["shadow"] = service.score_samples(
                artifact_path=train_result.get("artifact_path"),
                limit=int(payload["shadow_limit"]),
                mode="offmarket_shadow_after_train",
            )
        status = "done" if train_result.get("ok") else "failed"
        audit = _record_offmarket_high_load_audit(
            job_name=job_name,
            status=status,
            session=session,
            payload=payload,
            result=result,
            error=str(train_result.get("error") or ""),
            started_at=started_at,
        )
        logger.info(
            "[offmarket_high_load] {} {} profile={} samples={} shadow={}",
            job_name,
            status,
            profile,
            (train_result.get("metrics") or {}).get("sample_count") or train_result.get("sample_count"),
            (result.get("shadow") or {}).get("count"),
        )
        return {"ok": status == "done", "status": status, "audit": audit, "result": result}
    except Exception as exc:
        audit = _record_offmarket_high_load_audit(
            job_name=job_name,
            status="error",
            session=session,
            payload=payload,
            error=f"{type(exc).__name__}: {exc}"[:500],
            started_at=started_at,
        )
        logger.warning("[offmarket_high_load] {} error: {}", job_name, exc)
        return {"ok": False, "status": "error", "audit": audit, "error": str(exc)}



def _start_live_scheduler():
    """注册并启动自进化 Scheduler (11 job). 幂等: 已运行时跳过."""
    from backend.runtime.scheduler import InProcessScheduler
    sched = InProcessScheduler()
    if getattr(sched, "_started", False):
        return
    run_heavy_jobs = _env_enabled("QUANT_BACKEND_HEAVY_JOBS", "0")

    if run_heavy_jobs:
        # ★ 初始化 EvolutionKernel (注册中枢 + quality gate + governor)
        from backend.runtime.evolution_kernel import EvolutionKernel

        kernel = EvolutionKernel.shared()
        kernel.set_pipeline(_factor_pipeline)
        kernel.start()  # registers evolution_hourly + awe_adapt + system_health
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
        "*/5 * * * *",
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
        # ★ awe_adapt / evolution_hourly / system_health 已由 EvolutionKernel 注册
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
        return _loop_status_snapshot(
            state_get=_live_state_get,
            thread=_loop_thread,
            broker=_loop_broker,
            started_at=_loop_started_at,
            strategy_name=_loop_strategy_name,
        )


def start_loop(
    broker: str,
    strategy_name: str = "v1_minimal_ma_cross",
    *,
    persist_desired: bool = True,
    trigger_reason: str = "manual",
) -> dict:
    """Spawn the live loop as a background thread in this backend process.
    Refuses if a loop is already running. Requires the broker to be reachable."""
    global _loop_thread, _loop_stop_flag, _loop_broker, _loop_started_at, _loop_strategy_name
    global _last_loop_end

    with _loop_state_lock:
        if _loop_thread is not None and _loop_thread.is_alive():
            return {
                "ok": False,
                "error": f"live loop already running (broker={_loop_broker})",
                "broker": _loop_broker,
                "started_at": _loop_started_at,
                "strategy_name": _loop_strategy_name,
            }
        if broker not in ("ctrader",):
            return {"ok": False, "error": f"unknown broker: {broker}"}

        # ★ v9-fix: 重启退避 — 上次停止后至少等 _MIN_RESTART_INTERVAL 秒
        # audit v3: _MIN_RESTART_INTERVAL=60s 太长, 小程序重启只等2秒就调start
        # 用户主动重启时不应阻塞, 只对自动重启(如auto_recovery)保留退避
        since_end = time.time() - _last_loop_end if _last_loop_end else 999

    # 退避只在上次停止后很短时间内生效 (防止 auto_recovery 立即重启崩溃的 loop)
    # 用户主动 stop→start 间隔一般 >2s, 不会触发
    if _last_loop_end and since_end < 3:
        wait = 3 - since_end
        logger.warning(f"[live] restart backoff: waiting {wait:.1f}s")
        time.sleep(wait)

    with _loop_state_lock:
        # 再次检查是否有人在等的时候启动了
        if _loop_thread is not None and _loop_thread.is_alive():
            return {"ok": False, "error": "another loop started during backoff wait"}

        # Pre-flight: broker connection must be live
        acct = {"ok": True, "broker": broker, "balance": 0, "equity": 0,
                "margin": 0, "margin_free": 0, "leverage": 0, "currency": ""}

        _loop_stop_flag = threading.Event()
        _loop_broker = broker
        _loop_started_at = time.time()
        _loop_strategy_name = strategy_name  # audit 2026-06-08
        if persist_desired:
            _persist_loop_desired_state(
                True,
                broker=broker,
                strategy_name=strategy_name,
                reason=trigger_reason,
            )
        # ⚠️ audit 2026-06-09: 启动前立即填充共享缓存, 否则 WS 1s 推送读到
        # _live_state["account"]=None → equity=0, 要等 60s 第一个 tick 才恢复.
        _prime_live_loop_state(
            broker=broker,
            strategy_name=strategy_name,
            started_at=_loop_started_at,
            account=acct,
        )
        # 启动自进化 Scheduler (5 job)
        _start_live_scheduler()
        _loop_thread = threading.Thread(
            target=_run_loop,
            args=(broker, _loop_stop_flag),
            name=f"live_loop_{broker}",
            daemon=True,
        )
        _loop_thread.start()
        logger.info(f"live loop started: broker={broker} strategy={strategy_name} thread_id={_loop_thread.ident}")

    return {
        "ok": True,
        "broker": broker,
        "started_at": _loop_started_at,
        "thread_id": _loop_thread.ident,
        "pid": _loop_thread.ident,  # audit 2026-06-09: alias for FE uniformity (paper/start returns pid; thread.ident is the closest equivalent for a background thread)
        "strategy_name": strategy_name,
        "trigger_reason": trigger_reason,
        "msg": f"live loop thread started. Read /api/live/loop-status to monitor.",
    }


def stop_loop(
    *,
    persist_desired: bool = True,
    trigger_reason: str = "manual",
) -> dict:
    """Signal the loop thread to stop. Returns immediately;
    blocking cleanup (thread join + scheduler shutdown) runs in background.
    audit v9: 停止后保留最后数据不变 (account/positions/session 冻结), 前端持续显示.
    audit v3: 立即清 _loop_thread, 让 start_loop 不再误判"already running"
    """
    global _loop_thread, _loop_stop_flag, _loop_broker, _loop_started_at
    global _last_loop_end

    with _loop_state_lock:
        if _loop_thread is None or not _loop_thread.is_alive():
            if persist_desired:
                _persist_loop_desired_state(
                    False,
                    broker=_loop_broker or "ctrader",
                    strategy_name=_loop_strategy_name or "factor_v4",
                    reason=trigger_reason,
                )
            return {"ok": True, "was_running": False, "broker": None, "msg": "no loop running"}
        broker = _loop_broker
        if _loop_stop_flag is not None:
            _loop_stop_flag.set()
        thread = _loop_thread
        # ★ 立即清 _loop_thread, 让 start_loop 能检测到"已停止"
        # 后台清理线程只负责 join + scheduler shutdown
        _loop_thread = None
        _loop_stop_flag = None
        _loop_broker = None
        _loop_started_at = None

    # ★ 立即标记停止, 前端立刻看到状态变化
    _mark_loop_stopped_for_display()  # 清策略名，防止 WS pipeline 误判运行中
    if persist_desired:
        _persist_loop_desired_state(False, broker=broker or "ctrader", strategy_name=_loop_strategy_name or "factor_v4", reason=trigger_reason)
    _runtime_kv_set(
        _RUNTIME_KV_LAST_SHUTDOWN,
        {"broker": broker, "ts": time.time(), "trigger_reason": trigger_reason},
    )
    _last_loop_end = time.time()

    # 阻塞清理移到后台线程, stop 端点秒返
    def _cleanup() -> None:
        thread.join(timeout=5)
        if thread.is_alive():
            logger.warning(f"live loop thread for {broker} did not stop within 5s; will continue in background")
        _stop_live_scheduler()
        logger.info("[live] loop stopped, data frozen for display")

    threading.Thread(target=_cleanup, name="stop_loop_cleanup", daemon=True).start()
    logger.info("[live] stop signaled, cleanup in background")
    return {"ok": True, "was_running": True, "broker": broker, "trigger_reason": trigger_reason}


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


def _run_live_loop_tick_body(
    *,
    broker: str,
    bridge_cfg: Any,
    timeframe: str,
    tick: int,
    recovery_bootstrapped: bool,
    stop_requested,
    log,
) -> dict[str, Any]:
    market_session = _market_session_snapshot(None)
    if str(market_session.get("status") or "") == "closed_confirmed":
        bridge, err, warming = _get_ctrader()
        bridge_ready = bool(bridge is not None and not warming and bridge.is_connected)
        if err:
            _market_session_snapshot(None, broker_error=err)
        _set_loop_diagnostic(tick, "market_closed", bridge_ready=bridge_ready)
        log(
            _loop_market_closed_log_message(
                tick=tick,
                market_session=market_session,
                bridge_ready=bridge_ready,
                warming=warming,
            )
        )
        return {"recovery_bootstrapped": recovery_bootstrapped, "wait_seconds": 300.0, "break_loop": False}

    bridge, err, warming = _get_ctrader()
    if err:
        _market_session_snapshot(None, broker_error=err)
        log(f"tick {tick}: {err}; reconnect next tick")
        return {"recovery_bootstrapped": recovery_bootstrapped, "wait_seconds": 60.0, "break_loop": False}
    bridge_ready = bridge is not None and not warming and bridge.is_connected
    market_session = _market_session_snapshot(bridge)
    if str(market_session.get("status") or "") == "closed_confirmed":
        _set_loop_diagnostic(tick, "market_closed", bridge_ready=bridge_ready)
        log(
            _loop_market_closed_log_message(
                tick=tick,
                market_session=market_session,
                bridge_ready=bridge_ready,
                warming=warming,
                after_broker_check=True,
            )
        )
        return {"recovery_bootstrapped": recovery_bootstrapped, "wait_seconds": 300.0, "break_loop": False}
    _set_loop_diagnostic(
        tick,
        _loop_bridge_readiness_label(bridge_ready=bridge_ready, warming=warming),
        bridge_ready=bridge_ready,
    )
    if not bridge_ready:
        log(f"tick {tick}: cTrader warming/disconnected, running pipeline dry")
    else:
        try:
            require_l2_depth = bool(getattr(bridge_cfg, "risk_require_l2_depth", False))
            l2_collection_enabled = bool(getattr(bridge_cfg, "l2_collection_enabled", True))
            _ensure_spot_subscription(
                bridge,
                require_l2_depth=require_l2_depth,
                l2_collection_enabled=l2_collection_enabled,
                log=log,
            )
        except Exception as _spot_sub_err:
            logger.debug("[live] spot subscription refresh skipped: %s", _spot_sub_err)

    if bridge_ready:
        kickoff_account_refresh(bridge, broker, interval_sec=30.0)
        if not recovery_bootstrapped:
            try:
                recovery_bootstrapped = _bootstrap_position_recovery(
                    bridge,
                    broker=broker,
                    strategy_name=str(_loop_strategy_name or "factor_v4"),
                    log=log,
                )
            except Exception as _recovery_err:
                log(f"tick {tick}: recovery bootstrap failed (non-fatal): {_recovery_err}")

    df_new = _warmup_from_local_db("XAUUSD+", timeframe, 5)
    if df_new is None or len(df_new) == 0:
        log(f"tick {tick}: local DB has no bars (waiting for CTraderPuller)")
        return {"recovery_bootstrapped": recovery_bootstrapped, "wait_seconds": None, "break_loop": False}

    quote = bridge.get_spot_quote() if bridge is not None and hasattr(bridge, "get_spot_quote") else {}
    if quote:
        _live_state_update(spot_quote=quote)
    spot_result = _loop_apply_spot_quote_to_latest_bar(
        df_new=df_new,
        quote=quote,
        quote_is_fresh=_quote_is_fresh,
    )
    if spot_result["too_far"]:
        log(
            f"tick {tick}: spot={spot_result['spot']:.2f} too far from "
            f"bar close={spot_result['last_close']:.2f}, using DataStore price"
        )

    cb_tripped = _live_state_get("circuit_breaker", False)
    if cb_tripped:
        log(f"tick {tick}: circuit breaker tripped, skip trading")
        return {"recovery_bootstrapped": recovery_bootstrapped, "wait_seconds": None, "break_loop": False}
    dd_state = _evaluate_daily_drawdown()
    if dd_state["tripped"]:
        log(f"tick {tick}: CIRCUIT BREAKER: daily drawdown {dd_state['dd_pct']:.1f}%")
        return {"recovery_bootstrapped": recovery_bootstrapped, "wait_seconds": None, "break_loop": False}
    last_bar = df_new.iloc[-1]
    _process_tick(bridge, None, df_new, last_bar, broker, tick, log)
    if stop_requested():
        log(f"tick {tick}: stop requested during processing, exiting")
        return {"recovery_bootstrapped": recovery_bootstrapped, "wait_seconds": None, "break_loop": True}
    return {"recovery_bootstrapped": recovery_bootstrapped, "wait_seconds": None, "break_loop": False}


def _update_live_loop_risk_metrics(*, tick: int, log) -> None:
    try:
        acct = _live_state_get("account", {}, clone=True) or {}
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
            _set_risk_metric("kelly", _kelly_calc.calculate(win_rate, avg_win, max(avg_loss, 0.01)))
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
            _set_risk_metric("kelly", _kelly_calc.calculate(win_rate, avg_win, avg_loss))
        else:
            _set_risk_metric("kelly", _kelly_calc.get_status())

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


def _run_loop(broker: str, stop_flag: threading.Event) -> None:
    """Live trading loop — 全由 Factor Takeover v4 因子管道驱动。"""
    import sys
    from pathlib import Path
    # ── 时间框架 (从 RuntimeConfig 读取) ──
    from config.runtime_config import shared as _rcc
    _rcfg = _rcc()
    TF = _rcfg.timeframe  # "M5"
    project_root = Path(__file__).resolve().parent.parent.parent
    log_path = project_root / "logs" / "live_loop.log"
    log_path.parent.mkdir(exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [live_loop:{broker}] {msg}"
        log_fh.write(line + "\n")
        log_fh.flush()
        logger.info(line)

    log(f"live loop started (broker={broker}, timeframe={TF})")

    # ── Phase 1: warmup ──
    # audit 2026-06-08: Pepperstone demo broker ProtoOAGetTrendbarsReq 不返 history
    # (任何 period 都 0 bar). 改优先读本地 DataStore("data/ctrader_data.duckdb") 拉 XAUUSD+
    # M15 200 根, 再 fallback 到 broker fetch_bars.
    df = None
    df_source = None
    if broker == "ctrader":
        df = _warmup_from_local_db("XAUUSD+", TF, 200)
        if df is not None and len(df) >= 30:
            df_source = "local_db"
            last_ts = df.index[-1]
            age_hours = (pd.Timestamp.now("UTC").tz_localize(None) - last_ts.tz_localize(None)).total_seconds() / 3600 if last_ts.tzinfo else 0
            if age_hours > 24:
                logger.warning(
                    f"local DB bars are {age_hours:.1f}h stale (last bar: {last_ts}). "
                    f"Strategy will warm up on outdated data. Consider running live_sync."
                )
    if df is None or len(df) < 30:
        # fallback: broker fetch_bars
        try:
            if broker == "ctrader":
                # audit 2026-06-10: 3-tuple + 阻塞等连好 (loop 线程里可等)
                bridge, err, warming = _get_ctrader()
                if err:
                    log(f"FATAL: {err}")
                    return
                if warming or not bridge.is_connected:
                    wait_err = _wait_ctrader_ready(bridge, timeout_sec=30.0)
                    if wait_err:
                        log(f"FATAL: {wait_err}")
                        return
                df = _fetch_bars_with_retry(bridge, timeframe=TF, n_bars=200)
            else:
                log(f"FATAL: unknown broker {broker}")
                return
            df_source = "broker"
        except Exception as e:
            log(f"FATAL: warmup exception: {type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}")
            return

    if df is None or len(df) < 30:
        # ★ v9-fix: 尝试从备份缓存加载
        cache_df = _load_bar_cache()
        if cache_df is not None and len(cache_df) >= 30:
            df = cache_df
            df_source = "cache"
            log(f"WARNING: loaded {len(df)} bars from backup cache, "
                f"last close={df['close'].iloc[-1]:.2f}")
        else:
            log(f"FATAL: insufficient history bars (got {0 if df is None else len(df)} < 30) "
                f"— local DB empty, broker returned 0, and no backup cache")
            return
    warmup_price = float(df["close"].iloc[-1])
    warmup_ts = time.time()
    try:
        last_index = df.index[-1]
        if hasattr(last_index, "timestamp"):
            warmup_ts = float(last_index.timestamp())
    except Exception:
        warmup_ts = time.time()
    _publish_latest_price(warmup_price, source=f"warmup_{df_source or 'bar'}", ts=warmup_ts)
    log(f"warmed up: {len(df)} bars (source={df_source}), last close={warmup_price:.2f}")
    # ★ v9-fix: 成功后缓存 bar 供下次启动使用
    _save_bar_cache(df)

    # ── Factor Takeover v4 管道初始化 ──
    global _factor_pipeline
    global _DECISION_LOG, _DECISION_LOG_RUN_ID
    global _LEDGER, _TRADE_REVIEWER, _EXPERIENCE_BUILDER, _POLICY_SUGGESTER
    _factor_pipeline = None
    try:
        from config.runtime_config import shared as _rcc
        _rcfg = _rcc()
        from alpha.streaming_factor_engine import StreamingFactorEngine
        from alpha.signal_normalizer import SignalNormalizer
        from alpha.portfolio_compositor import PortfolioCompositor
        from alpha.execution_gate import ExecutionGate

        engine = StreamingFactorEngine(max_buffer=200, factor_runtime_config=_rcfg.factor_signal_config)
        normalizer = SignalNormalizer(_rcfg.factor_signal_config)
        compositor = PortfolioCompositor(
            _merge_portfolio_configs(
                _rcfg.factor_signal_config,
                _rcfg.factor_portfolio_weights,
                _rcfg.factor_tactical_alpha,
                _rcfg.factor_signal_threshold,
            )
        )
        gate = ExecutionGate(_loop_execution_gate_config(_rcfg))
        from alpha.attribution_engine import AttributionEngine
        from alpha.adaptive_weight_engine import AdaptiveWeightEngine
        from alpha.ic_tracker import ICTracker
        attr = AttributionEngine()
        ictracker = ICTracker(window=5000)
        awe = AdaptiveWeightEngine(_loop_adaptive_weight_config(_rcfg), ictracker=ictracker)
        awe.initialize(_rcfg.factor_portfolio_weights, ictracker=ictracker)
        event_sizing = None
        try:
            from backend.core.db import DUCKDB_EVENTS
            from execution.event_sizing import EventSizing
            event_sizing = EventSizing(db_path=str(DUCKDB_EVENTS), enabled=True)
        except Exception as _event_sizing_err:
            logger.debug("[live] event sizing init skipped: %s", _event_sizing_err)
        _factor_pipeline = {
            "engine": engine, "normalizer": normalizer,
            "compositor": compositor, "gate": gate,
            "attribution": attr, "awe": awe, "ic_tracker": ictracker,
            "event_sizing": event_sizing,
        }
        log(f"Factor Takeover v4 pipeline initialized "
            f"(ctrader_demo={_rcfg.ctrader_send_orders})")
        # ── 订阅 RuntimeConfig 变更, 热更新 compositor 权重 ──
        try:
            from config.runtime_config import subscribe as _rc_subscribe
            def _on_config_change(cfg, version):
                try:
                    merged_cfg = _merge_portfolio_configs(
                        cfg.factor_signal_config,
                        cfg.factor_portfolio_weights,
                        cfg.factor_tactical_alpha,
                        cfg.factor_signal_threshold,
                    )
                    _loop_apply_factor_pipeline_config_update(
                        pipelines=_loop_unique_factor_pipelines(_factor_pipeline, _factor_pipelines),
                        cfg=cfg,
                        merged_config=merged_cfg,
                    )
                    logger.debug("[live] factor pipeline hot-reloaded (v%d)", version)
                except Exception as _e:
                    logger.debug("[live] factor pipeline hot-reload: %s", _e)
            _rc_subscribe(_on_config_change)
            log("RuntimeConfig subscription active: factor pipeline will hot-reload configs")
        except Exception as e:
            log(f"RuntimeConfig subscription skipped: {e}")
        # ── 初始化决策审计日志 ──
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
        # ── 多品种管道初始化 (Phase 6: _factor_pipelines) ──
        global _factor_pipelines
        _factor_pipelines = {}
        try:
            from config.runtime_config import shared as _rcfg2
            cfg2 = _rcfg2()
            symbols = _loop_enabled_symbols_from_config(cfg2)
            _factor_pipelines = _loop_build_extra_symbol_factor_pipelines(
                symbols=symbols,
                primary_symbol="XAUUSD+",
                primary_pipeline=_factor_pipeline,
                cfg=cfg2,
                shared_components={
                    "attribution": attr,
                    "awe": awe,
                    "ic_tracker": ictracker,
                    "event_sizing": event_sizing,
                },
                streaming_engine_cls=StreamingFactorEngine,
                normalizer_cls=SignalNormalizer,
                compositor_cls=PortfolioCompositor,
                gate_cls=ExecutionGate,
                merge_portfolio_configs=_merge_portfolio_configs,
            )
            if len(symbols) > 1:
                log(f"Multi-symbol pipelines initialized: {symbols}")
        except Exception as e:
            log(f"Multi-symbol pipeline init skipped: {e}")
            _factor_pipelines = {"XAUUSD+": _factor_pipeline} if _factor_pipeline else {}
        # Phase 6: 初始化跨品种协方差
        global _cross_asset_covar
        try:
            from risk.cross_asset import CrossAssetCovariance
            symbols = _loop_cross_asset_symbols_for_config(_rcfg)
            if symbols:
                _cross_asset_covar = CrossAssetCovariance(
                    symbols, window=_rcfg.cross_asset_covariance_window
                )
                log(f"Cross-asset covariance initialized: {symbols}")
        except Exception as e:
            log(f"Cross-asset covariance init skipped: {e}")
            _cross_asset_covar = None
    except Exception as e:
        log(f"Factor pipeline init failed: {e}")
        import traceback as _tb
        log(f"  Traceback: {_tb.format_exc()[-600:]}")
        _factor_pipeline = None

    # 把 warmup bars 喂给
    if _factor_pipeline is not None:
        fp = _factor_pipeline
        try:
            fp["engine"].reset()
            snapshots = []
            min_warmup = int(getattr(fp["engine"], "MIN_BARS", 50) or 50)
            warmup_limit = int(getattr(_rcfg, "live_factor_warmup_bars", 80) or 80)
            warmup_feed = _loop_build_warmup_feed(
                df,
                timeframe=TF,
                min_warmup=min_warmup,
                warmup_limit=warmup_limit,
            )
            warmup_df = warmup_feed["warmup_df"]
            warmup_bars = warmup_feed["warmup_bars"]
            log(f"Factor pipeline warmup feeding {len(warmup_df)} / {len(df)} bars")
            if hasattr(fp["engine"], "warmup_bars"):
                snapshots = fp["engine"].warmup_bars(warmup_bars)
            else:
                for bar in warmup_bars:
                    fv = fp["engine"].append_bar(bar)
                    if fv:
                        snapshots.append(fv)
            if snapshots:
                fp["normalizer"].warmup(snapshots)
            # ★ 预热完成后立即跑一次 compose+gate, 生成初始因子投票数据
            if fp["engine"].is_warm and snapshots:
                try:
                    last_fv = snapshots[-1]
                    last_bar = {
                        "open": float(df["open"].iloc[-1]),
                        "high": float(df["high"].iloc[-1]),
                        "low": float(df["low"].iloc[-1]),
                        "close": float(df["close"].iloc[-1]),
                        "volume": float(df["volume"].iloc[-1]) if "volume" in df.columns else 0.0,
                        "time": float(df.index[-1].timestamp()) if hasattr(df.index[-1], "timestamp") else 0.0,
                        "timeframe": TF,
                        "complete": True,
                    }
                    signals = fp["normalizer"].normalize(last_fv)
                    composite = fp["compositor"].compose(signals, last_fv)
                    gate_result = fp["gate"].filter(composite, last_fv, last_bar)
                    fp["gate"].tick()
                    _set_factor_snapshot(
                        _tick_build_factor_votes(
                            signals,
                            last_fv,
                            getattr(composite, "factor_roles", {}),
                            getattr(composite, "active_weights", {}),
                        ),
                        _tick_build_factor_snapshot_summary(composite, gate_result, now=time.time()),
                    )
                    dir_name = {1: "LONG", -1: "SHORT"}.get(composite.direction, "FLAT")
                    log(f"warmup signal: {dir_name} score={composite.score:.4f} "
                        f"n={composite.n_active_factors} gate={gate_result.reason}")
                except Exception as e:
                    log(f"warmup signal generation failed (non-fatal): {e}")
            log(f"Factor pipeline warmed up: {len(df)} bars, "
                f"buffer={fp['engine'].buffer_size}, "
                f"warm={fp['engine'].is_warm}")
        except Exception as e:
            log(f"Factor pipeline warmup failed: {e}")

    # 订阅 cTrader 实时报价；warmup local_db 路径从 _get_ctrader() 拿真 bridge 并短等 ready.
    if broker == "ctrader":
        try:
            require_l2_depth = bool(getattr(_rcfg, "risk_require_l2_depth", False))
            l2_collection_enabled = bool(getattr(_rcfg, "l2_collection_enabled", True))
            _loop_subscribe_spot_depth_once(
                get_ctrader=_get_ctrader,
                wait_ctrader_ready=_wait_ctrader_ready,
                require_l2_depth=require_l2_depth,
                l2_collection_enabled=l2_collection_enabled,
                log=log,
                timeout_sec=10.0,
            )
        except Exception as e:
            log(f"subscribe_spots failed (non-fatal): {e}")

    # ── Phase 3: 主循环 (60s tick) ──
    tick = 0
    recovery_bootstrapped = False
    _current_trade_date: str = ""
    while not stop_flag.is_set():
        tick += 1
        # 诊断: 记录 tick 计数和桥状态
        _set_loop_diagnostic(tick, "checking")

        # ── 跨日重置熔断 + 会话统计 ──
        try:
            from datetime import datetime, timezone
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today_str != _current_trade_date:
                if _current_trade_date:
                    log(f"new trading day {today_str}, resetting session stats")
                    _current_trade_date = today_str
                    _reset_session_state_for_new_day()
                elif _restore_session_state_for_day(today_str):
                    log(f"restored session risk state for {today_str}")
                    _current_trade_date = today_str
                else:
                    _current_trade_date = today_str
                    _reset_session_state_for_new_day()
        except Exception as _e2:
            log(f"tick {tick}: session reset failed (non-fatal): {_e2}")

        # ── 主循环体: 账户刷新 + 数据读取 + 交易 ──
        try:
            tick_result = _run_live_loop_tick_body(
                broker=broker,
                bridge_cfg=_rcfg,
                timeframe=TF,
                tick=tick,
                recovery_bootstrapped=recovery_bootstrapped,
                stop_requested=stop_flag.is_set,
                log=log,
            )
            recovery_bootstrapped = bool(tick_result["recovery_bootstrapped"])
            if tick_result["break_loop"]:
                break
            wait_seconds = tick_result.get("wait_seconds")
            if wait_seconds is not None:
                if stop_flag.wait(float(wait_seconds)):
                    break
                continue
        except Exception as e:
            log(f"tick {tick} error: {type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")

        # ── 风险模块自动计算 (每 tick, 不阻塞主循环) ──
        _update_live_loop_risk_metrics(tick=tick, log=log)

        if stop_flag.wait(60):
            break

    log(f"loop stopped after {tick} ticks")


def _merge_portfolio_configs(
    signal_config: dict, weight_config: dict,
    tactical_alpha: float, signal_threshold: float,
) -> dict:
    """合并 factor_signal_config (含 tags/mode) 和 factor_portfolio_weights (含 weight)
    为 PortfolioCompositor 所需的格式: {name: {weight, tags, mode, enabled, ...}}"""
    merged = {}
    try:
        from alpha.runtime_factor_selection import active_discovered_factor_ids

        discovered_names = set(active_discovered_factor_ids(signal_config))
    except Exception:
        discovered_names = set()
    all_names = set(signal_config) | set(weight_config) | discovered_names
    for name in all_names:
        sc = signal_config.get(name, {})
        if not isinstance(sc, dict):
            sc = {}
        default_weight = 0.3 if name in discovered_names and name not in weight_config else 1.0
        wc = weight_config.get(name, default_weight)
        weight = wc if isinstance(wc, (int, float)) else wc.get("weight", 1.0)
        merged[name] = {
            "weight": weight,
            "tags": sc.get("tags", ["GP发现"] if name in discovered_names else []),
            "mode": sc.get("mode", "rank_mapping"),
            "role": sc.get("role", "alpha"),
            "enabled": sc.get("enabled", True),
            "source": sc.get("source", "discovered" if name in discovered_names else "builtin"),
        }
    merged["_tactical_alpha"] = tactical_alpha
    merged["_signal_threshold"] = signal_threshold
    return merged


# ── Background account/positions cache writer ─────────────────────────
# audit 2026-06-10: 之前 _process_tick 每 60s 同步调 bridge.account_info() +
# bridge.get_positions() 写共享缓存. 改读缓存后这个写路径被删了, WS 1s
# 推送就拿到 start_loop 启动时的占位符 (balance=0, equity=0). 修复:
# _run_loop 的 60s 等待期间, 后台 daemon thread 调一次 account_info +
# get_positions, 写 _live_state. tick 主体保持非阻塞, 只有这个 writer
# 异步. 失败时静默 (下次 tick 重试), 不让后台错误炸主循环.
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
                raw = bridge.refresh_account_info() if hasattr(bridge, "refresh_account_info") else bridge.account_info()
            except Exception as e:
                logger.warning(f"[{broker}] background account_info failed: {e}")
                raw = None
            if raw:
                # 统一转 dict: CTraderBridge 返 AccountInfo dataclass
                if not isinstance(raw, dict):
                    from dataclasses import asdict
                    acct = asdict(raw)
                else:
                    acct = raw
                # audit 2026-06-10: ensure the cached account has `ok=True` so the
                # WS snapshot doesn't mistake it for an error envelope.
                acct.setdefault("ok", True)
                acct.setdefault("broker", broker)
                _live_state_update(account=acct, account_updated_at=now_ts)
        pos_raw = None
        if not positions_fresh:
            try:
                if hasattr(bridge, "refresh_positions"):
                    has_reconcile_ts = hasattr(bridge, "_last_reconcile_at")
                    before_reconcile_raw = getattr(bridge, "_last_reconcile_at", 0.0)
                    before_reconcile = (
                        float(before_reconcile_raw)
                        if isinstance(before_reconcile_raw, (int, float))
                        else 0.0
                    )
                    try:
                        pos_raw = bridge.refresh_positions(force=True, allow_cache_fallback=False)
                    except TypeError:
                        pos_raw = bridge.refresh_positions()
                    after_reconcile_raw = getattr(bridge, "_last_reconcile_at", 0.0)
                    after_reconcile = (
                        float(after_reconcile_raw)
                        if isinstance(after_reconcile_raw, (int, float))
                        else before_reconcile + 1.0
                    )
                    if has_reconcile_ts and after_reconcile <= before_reconcile:
                        logger.warning(f"[{broker}] background positions reconcile did not advance; skip cache write")
                        pos_raw = None
                else:
                    pos_raw = bridge.get_positions() or []
            except Exception as e:
                logger.warning(f"[{broker}] background get_positions failed: {e}")
                pos_raw = None
        if pos_raw is not None:
            try:
                from config.runtime_config import shared as _rc

                cfg = _rc()
            except Exception:
                cfg = None
            enriched = _enrich_positions_with_path_metrics(
                pos_raw,
                cfg=cfg,
                now_ts=time.time(),
                persist=False,
                broker=broker,
                strategy_name=str(_loop_strategy_name or "factor_v4"),
                account=acct,
            )
            _live_state_update(positions=enriched, positions_updated_at=time.time())
    finally:
        _ACCOUNT_REFRESH_LOCK.release()


def kickoff_account_refresh(bridge, broker: str, interval_sec: float = 30.0) -> threading.Thread:
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
def _process_tick(bridge, strategy, df_new, last_bar, broker: str, tick: int, log) -> None:
    """处理一根新 bar — 全部由 Factor Takeover v4 因子管道驱动。"""
    global _factor_pipeline
    if _factor_pipeline is not None:
        try:
            return _process_tick_factor_pipeline(
                bridge, _factor_pipeline, df_new, last_bar, broker, tick, log,
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

def emergency_close(broker: str, symbol: str | None = None) -> dict:
    """Close all positions (or one symbol) on the given broker."""
    if broker == "ctrader":
        # audit 2026-06-10: 3-tuple + 短等 (emergency close 用户主动点, 可接受 5s 等)
        bridge, err, warming = _get_ctrader()
        if err:
            return {"ok": False, "error": err}
        if warming or not bridge.is_connected:
            wait_err = _wait_ctrader_ready(bridge, timeout_sec=5.0)
            if wait_err:
                return {"ok": False, "error": f"cTrader not ready: {wait_err}"}
        try:
            # cTrader close_position() 必须传 position_id, 没传 server 必拒
            # (audit 2026-06-08: 之前分支里 close_position() 不带参会 fail).
            # symbol 路径: 强制走 broker reconcile + filter by symbol_id + close 一个个.
            # 紧急平仓不能依赖缓存，否则会把 stale position 当成真实持仓。
            try:
                positions = bridge.refresh_positions(force=True, allow_cache_fallback=False)
            except TypeError:
                positions = bridge.refresh_positions()
            if symbol:
                # symbol 这里可能是 symbol 名 (XAUUSD) 或 id (int), 简单按 name 匹配 fallback
                target_positions = [p for p in positions if str(p.get("symbol_id")) == symbol or p.get("symbol") == symbol]
            else:
                target_positions = positions
            closed = 0
            failures: list[dict] = []
            for p in target_positions:
                # 优先用 position_id; 旧 dict 形式也兼容
                pid = p.get("position_id") or p.get("ticket")
                if pid is None:
                    continue
                volume = _position_api_volume(p)
                if volume <= 0:
                    failures.append({
                        "position_id": int(pid),
                        "error_code": "invalid_close_volume",
                        "comment": f"live broker position has invalid volume={volume}",
                    })
                    logger.error("[live] emergency close skipped pos=%s invalid volume=%s", pid, volume)
                    continue
                close_context = _build_close_position_risk_context(
                    position_id=int(pid),
                    close_reason="emergency_close",
                    mode="live",
                    broker=broker,
                    symbol=str(p.get("symbol") or symbol or ""),
                    position=p,
                )
                close_verdict = _RISK_POLICY.evaluate(
                    "close_position",
                    close_context,
                )
                if not close_verdict.allowed:
                    logger.warning(
                        "[live] emergency close blocked by risk policy pos=%s reason=%s",
                        pid,
                        close_verdict.reason,
                    )
                    failures.append({
                        "position_id": int(pid),
                        "error_code": "risk_blocked",
                        "comment": str(close_verdict.reason or ""),
                    })
                    continue
                result = bridge.close_position(int(pid), volume=volume)
                if getattr(result, "success", False):
                    _remember_close_reason(int(pid), "emergency_close")
                    _remember_close_verdict(int(pid), close_verdict)
                    closed += 1
                else:
                    failures.append({
                        "position_id": int(pid),
                        "error_code": str(getattr(result, "error_code", "") or ""),
                        "comment": str(getattr(result, "comment", "") or ""),
                    })
            attempted = len(target_positions)
            return {
                "ok": not failures,
                "broker": "ctrader",
                "symbol": symbol or "ALL",
                "attempted": attempted,
                "closed": closed,
                "failed": len(failures),
                "failures": failures,
            }
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-300:]}
    else:
        return {"ok": False, "error": f"unknown broker: {broker}"}


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
    _record_session_trade(total_pnl)
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
) -> None:
    try:
        _mark_recovery_position_closed(
            int(cpid),
            close_reason=close_reason,
            close_pnl=float(total_pnl),
            closed_at=close_ts,
            meta={"real_pnl": real_pnl or {}, "factor_contributions": factor_contributions or {}},
        )
    except Exception as _recovery_close_err:
        logger.debug("[live] recovery close persist failed for pos %s: %s", cpid, _recovery_close_err)
    _trailing_state.pop(cpid, None)
    _pos_entry_scores.pop(cpid, None)
    _pos_entry_decisions.pop(int(cpid), None)
    _pending_open_attach_until.pop(int(cpid), None)


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
) -> None:
    for cpid in closed_pids:
        try:
            real_pnl = real_pnls.get(cpid)
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
            _cleanup_closed_position_after_tick(
                cpid=int(cpid),
                close_reason=close_reason,
                total_pnl=total_pnl,
                close_ts=close_ts,
                real_pnl=real_pnl,
                factor_contributions=factor_contributions,
            )
        except Exception as exc:
            log(f"tick {tick}: attribution close pos={cpid} error: {exc}")


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
) -> None:
    try:
        _exec_quality.record(_ExecTrade(
            signal_time=bar.get("time", time.time()),
            submit_time=time.time(),
            fill_time=time.time(),
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


def _process_tick_factor_pipeline(
    bridge, pipeline: dict, df_new, last_bar, broker: str,
    tick: int, log,
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

    engine = pipeline["engine"]
    normalizer = pipeline["normalizer"]
    compositor = pipeline["compositor"]
    gate = pipeline["gate"]

    # 1. 构造 bar dict
    bar = _tick_build_factor_bar(last_bar, df_new, _tf)

    # 2. 流式因子计算 → 归一化 → 组合 → 闸门
    engine.refresh_factor_list()
    factor_values = engine.append_bar(bar)
    if not factor_values or not engine.is_warm:
        log(f"tick {tick}: factor engine not ready (is_warm={engine.is_warm})")
        gate.tick()
        return

    signals = normalizer.normalize(factor_values)
    composite = compositor.compose(signals, factor_values, timestamp=bar.get("time", time.time()))
    try:
        from backend.services.context_policy import ContextPolicyService

        context_policy = (
            ContextPolicyService().evaluate(getattr(composite, "context_state", {}) or {}, cfg).to_dict()
            if bool(getattr(cfg, "context_policy_enabled", True))
            else {"signal_threshold_delta": 0.0, "position_multiplier": 1.0, "reason": "disabled", "applied": False}
        )
        setattr(composite, "context_policy", context_policy)
        base_threshold = float(getattr(cfg, "factor_signal_threshold", 0.3) or 0.3)
        gate._threshold = max(0.0, min(1.0, base_threshold + float(context_policy.get("signal_threshold_delta") or 0.0)))
    except Exception:
        setattr(composite, "context_policy", {})
    gate_result = gate.filter(composite, factor_values, bar)
    gate.tick()
    # ★ 保存因子投票快照到 _live_state, 前端「因子投票」面板读取
    try:
            _set_factor_snapshot(
                _tick_build_factor_votes(
                    signals,
                    factor_values,
                    getattr(composite, "factor_roles", {}),
                    getattr(composite, "active_weights", {}),
                ),
                _tick_build_factor_snapshot_summary(composite, gate_result, now=time.time()),
            )
    except Exception as _e:
        log(f"tick {tick}: factor votes save failed (non-fatal): {_e}")
    # ── 决策审计: signal ──
    if _DECISION_LOG:
        bar_ts = bar.get("time", 0)
        if bar_ts:
            bar_date = time.strftime("%Y-%m-%d", time.gmtime(bar_ts))
            _safe_decision_log(
                _DECISION_LOG,
                run_id=_DECISION_LOG_RUN_ID,
                ts=bar_ts,
                bar_date=bar_date,
                decision_type="signal",
                strategy="factor_v4",
                direction=composite.direction,
                confidence=composite.score,
                decision=("execute" if gate_result.passed
                          and composite.direction != 0 else "hold"),
                meta=_json.dumps({
                    "gate_reason": gate_result.reason,
                    "tick": tick,
                    "tactical_score": composite.tactical_score,
                    "macro_score": composite.macro_score,
                    "n_active": composite.n_active_factors,
                    "n_abstain": composite.n_abstain_factors,
                }, ensure_ascii=False),
            )
    # 3. 构造 Signal (兼容旧 _send_order 逻辑)
    from alpha.portfolio_compositor import CompositeSignal

    # 4. 发单 (仅非 dry_run 且门通过)
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
    if closed_pids and bridge is not None:
        try:
            from execution.deal_sync import sync_close_deals_batch
            _sconn = _get_state_pg_conn()
            try:
                _real_pnls = sync_close_deals_batch(bridge, _sconn, closed_pids)
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

    # ── 执行 ──
    atr_val = factor_values.get("atr_ratio", 0)
    atr_price = atr_val * current_price if atr_val and atr_val > 0 else 0
    sl_price = 0.0
    tp_price = 0.0
    if composite.direction != 0 and gate_result.passed and send and pending_open_attach_ids:
        log(
            f"tick {tick}: v4 open SKIP (pending_open_attach "
            f"positions={pending_open_attach_ids})"
        )

    if composite.direction != 0 and gate_result.passed and send and not pending_open_attach_ids:
        # 从 bridge metadata 取小数位 → 舍入 SL/TP 防 cTrader 拒绝
        _meta = getattr(bridge, '_symbol_meta', None) or {}
        if not _meta.get('api_min_volume') and bridge is not None and hasattr(bridge, '_resolve_symbol_id'):
            try:
                bridge._resolve_symbol_id()
                _meta = getattr(bridge, '_symbol_meta', None) or {}
            except Exception:
                pass
        preflight = _tick_build_open_order_preflight(
            direction=int(composite.direction or 0),
            current_price=float(current_price or 0.0),
            atr_price=float(atr_price or 0.0),
            strategy_sl_atr=float(getattr(cfg, "strategy_sl_atr", 0.0) or 0.0),
            strategy_tp_atr=float(getattr(cfg, "strategy_tp_atr", 0.0) or 0.0),
            bridge_meta=_meta,
            protection_prices=_protection_prices_from_reference,
        )
        direction_name = str(preflight["direction_name"])
        sl_dist = float(preflight["sl_dist"])
        tp_dist = float(preflight["tp_dist"])
        _digits = int(preflight["digits"])
        sl_price = float(preflight["sl_price"])
        tp_price = float(preflight["tp_price"])

        # ── 风控: Kelly 仓位 ──
        acct_clean = _live_state_get("account", {}, clone=True) or {}
        sizing_result = _risk_kelly_sizing(
            cfg, composite.direction, current_price, sl_price, _meta, acct_clean,
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
            bridge_meta=_meta,
            sizing_trace=sizing_trace,
        )
        adjusted_volume = float(event_sizing_result.get("volume") or 0.0)
        sizing_trace = dict(event_sizing_result.get("trace") or {})
        sizing_block_reason = str(event_sizing_result.get("blocked_reason") or "")
        # Event sizing now feeds the unified RiskPolicy. If a fractional event
        # reduction falls below broker minimum, RiskPolicy decides whether the
        # window is hard enough to block or whether the min/base order can pass.
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
        context_policy = dict(getattr(composite, "context_policy", {}) or {})
        try:
            context_mult = float(context_policy.get("position_multiplier", 1.0) or 1.0)
        except (TypeError, ValueError):
            context_mult = 1.0
        if context_policy and abs(context_mult - 1.0) > 1e-9:
            context_raw_volume = volume * context_mult
            context_volume = _floor_api_volume_to_step(context_raw_volume, _meta)
            volume = context_volume if context_volume > 0 else volume
            sizing_trace["context_policy"] = {
                **context_policy,
                "raw_api_volume": context_raw_volume,
                "adjusted_api_volume": volume,
            }
        log(f"tick {tick}: v4 {direction_name} req_api_volume={volume:.0f} "
            f"(Kelly enabled={getattr(cfg, 'kelly_enabled', False)} "
            f"event_mult={event_multiplier:.2f} base_api_volume={base_volume:.0f})")

        # ── Phase B: 统一风控裁决 ──
        event_filter_context = _event_filter_context_for_risk_policy(
            cfg=cfg,
            direction=int(composite.direction or 0),
            bar=bar,
            factor_values=factor_values,
        )
        risk_context = _build_open_trade_risk_context(
            cfg=cfg,
            bridge=bridge,
            acct=acct_clean,
            positions=pos,
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
        order_blocked = bool(order_block["order_blocked"])
        block_reason = str(order_block["block_reason"])

        if order_blocked:
            log(f"tick {tick}: v4 {direction_name} SKIP ({block_reason})")
            gate_result = type('GateResult', (), {
                'passed': False, 'reason': block_reason,
            })()
            if _LEDGER:
                try:
                    learning_context = _open_learning_context_payload(
                        bridge=bridge,
                        bar=bar,
                        positions_before=pos,
                        composite=composite,
                        symbol="XAUUSD+",
                        pid=0,
                        actual_api_volume=0.0,
                        requested_volume=float(volume or 0.0),
                        base_requested_volume=float(base_volume or 0.0),
                        current_price=float(current_price or 0.0),
                        fill_price=0.0,
                        sl_price=float(sl_price or 0.0),
                        tp_price=float(tp_price or 0.0),
                        sl_dist=float(sl_dist or 0.0),
                        tp_dist=float(tp_dist or 0.0),
                        event_sizing_context=event_sizing_context,
                        sizing_trace=sizing_trace,
                        risk_verdict=risk_verdict,
                        market_session=market_session,
                    )
                    _LEDGER.log_composite_decision(
                        **_tick_build_skip_ledger_payload(
                            composite=composite,
                            gate_result=gate_result,
                            cfg=cfg,
                            bar=bar,
                            account=acct,
                            positions_before=pos,
                            risk_state=_risk_state_with_verdict(risk_verdict),
                            risk_verdict=risk_verdict,
                            block_reason=block_reason,
                            skip_stage=str(order_block["skip_stage"]),
                            tick=tick,
                            sizing_trace=sizing_trace,
                            market_session=market_session,
                            event_sizing_context=event_sizing_context,
                            learning_context=learning_context,
                            decision_ts_fallback=time.time(),
                        )
                    )
                except Exception as _ledger_err:
                    logger.debug("[live] ledger risk policy skip failed: %s", _ledger_err)
        else:
            try:
                if composite.direction == 1:
                    result = bridge.market_buy(volume=volume, sl=0.0, tp=0.0, comment="quant-v4")
                elif composite.direction == -1:
                    result = bridge.market_sell(volume=volume, sl=0.0, tp=0.0, comment="quant-v4")
                else:
                    result = None

                if result is not None and getattr(result, "success", False):
                    fill_price = _tick_resolve_order_fill_price(result, current_price=current_price)
                    pid = _tick_resolve_order_position_id(result, positions_before=pos)
                    if pid > 0:
                        refreshed_positions = bridge.get_positions(getattr(bridge, 'symbol', '') or '')
                        actual_api_volume = _resolve_position_api_volume(
                            pid,
                            refreshed_positions,
                            volume,
                        )
                        protection_prices = _tick_resolve_open_protection_prices(
                            direction=int(composite.direction or 0),
                            fill_price=float(fill_price or 0.0),
                            current_price=float(current_price or 0.0),
                            sl_dist=float(sl_dist or 0.0),
                            tp_dist=float(tp_dist or 0.0),
                            digits=int(_digits or 2),
                            position_id=int(pid),
                            refreshed_positions=refreshed_positions,
                            position_open_price=_position_open_price,
                            protection_prices=_protection_prices_from_reference,
                        )
                        sl_price = float(protection_prices["sl_price"])
                        tp_price = float(protection_prices["tp_price"])
                        _remember_pending_open_attach(int(pid))
                        entry_protection_plan = _entry_protection_plan_payload(
                            position_id=int(pid),
                            direction=composite.direction,
                            entry_price=float(fill_price or current_price),
                            target_stop_loss=sl_price,
                            target_take_profit=tp_price,
                            requested_volume=volume,
                            actual_api_volume=actual_api_volume,
                            tick=tick,
                            status="pending",
                        )
                        try:
                            _upsert_recovery_position_state(
                                {
                                    "position_id": pid,
                                    "symbol": "XAUUSD+",
                                    "direction": composite.direction,
                                    "open_price": float(fill_price or current_price),
                                    "volume": float(actual_api_volume),
                                    "entry_decision_id": _lookup_entry_decision_id(int(pid)),
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
                                pid,
                                _protection_plan_err,
                            )
                        try:
                            amend_res = bridge.amend_position_sltp(
                                position_id=pid, sl=sl_price, tp=tp_price,
                            )
                            if getattr(amend_res, "success", False):
                                _record_amended_open_success_context(
                                    attr_engine=attr_engine,
                                    bridge=bridge,
                                    broker=broker,
                                    cfg=cfg,
                                    bar=bar,
                                    tick=tick,
                                    pid=pid,
                                    actual_api_volume=actual_api_volume,
                                    requested_volume=volume,
                                    base_requested_volume=base_volume,
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
                                )
                            else:
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
                                    pid=pid,
                                    actual_api_volume=actual_api_volume,
                                    requested_volume=volume,
                                    base_requested_volume=base_volume,
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
                                    event_sizing_context=event_sizing_context,
                                    sizing_trace=sizing_trace,
                                    sl_dist=sl_dist,
                                    tp_dist=tp_dist,
                                    status_error=amend_failure_reason,
                                    ledger_action_reason=str(getattr(amend_res, "comment", "amend_failed") or "amend_failed"),
                                    ledger_comment=str(getattr(amend_res, "comment", "") or ""),
                                    failure_log=(
                                        f"tick {tick}: v4 {direction_name} AMEND FAILED "
                                        f"pos={pid}: {amend_failure_reason}"
                                    ),
                                    log=log,
                                )
                        except Exception as e:
                            _record_amend_failure_after_fill(
                                attr_engine=attr_engine,
                                bridge=bridge,
                                broker=broker,
                                cfg=cfg,
                                bar=bar,
                                tick=tick,
                                pid=pid,
                                actual_api_volume=actual_api_volume,
                                requested_volume=volume,
                                base_requested_volume=base_volume,
                                fill_price=fill_price,
                                current_price=current_price,
                                sl_price=sl_price,
                                tp_price=tp_price,
                                acct=acct,
                                pos=pos,
                                composite=composite,
                                gate_result=gate_result,
                                risk_verdict=risk_verdict,
                                market_session=None,
                                event_sizing_context=event_sizing_context,
                                sizing_trace=sizing_trace,
                                sl_dist=sl_dist,
                                tp_dist=tp_dist,
                                status_error=f"amend_exception:{type(e).__name__}:{str(e)[:220]}",
                                ledger_action_reason=f"amend_exception:{type(e).__name__}",
                                ledger_error=str(e)[:300],
                                ledger_debug_message="[live] ledger amend exception event failed for pos %s: %s",
                                failure_log=f"tick {tick}: v4 {direction_name} amend exception: {e}",
                                log=log,
                            )
                    else:
                        log(f"tick {tick}: v4 {direction_name} ORDER OK (no position_id) "
                            f"vol={volume}")
                elif result is not None and not getattr(result, "success", False):
                    log(f"tick {tick}: v4 {direction_name} ORDER FAILED: "
                        f"{getattr(result, 'error_code', '?')} {getattr(result, 'comment', '')}")
                    if _LEDGER:
                        try:
                            order_failed_payloads = _tick_build_order_failed_ledger_payloads(
                                composite=composite,
                                gate_result=gate_result,
                                cfg=cfg,
                                bar=bar,
                                account=acct,
                                positions_before=pos,
                                risk_state=_live_state_get("risk", {}, clone=True) or {},
                                requested_volume=float(volume),
                                current_price=float(current_price),
                                sl_price=float(sl_price),
                                tp_price=float(tp_price),
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
            except Exception as e:
                log(f"tick {tick}: v4 {direction_name} order exception: {e}")

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
    if pos and bridge is not None and cfg is not None:
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
) -> list[ProtectionCandidate]:
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
        if last_attempt_ts > 0 and time.time() - last_attempt_ts < _ENTRY_PROTECTION_REPAIR_COOLDOWN_SECONDS:
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
    risk_verdict = _RISK_POLICY.evaluate(candidate.risk_action, risk_context).to_dict()
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
) -> set[int]:
    handled: set[int] = set()
    max_holding_bars = int(getattr(cfg, "risk_max_holding_bars", 0) or 0)
    if max_holding_bars <= 0:
        return handled

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
        )
        max_holding_seconds = float(close_context.get("max_holding_seconds", 0.0) or 0.0)
        holding_seconds = float(close_context.get("holding_seconds", 0.0) or 0.0)
        if not _lifecycle_holding_timeout_is_expired(close_context):
            continue

        close_verdict = _RISK_POLICY.evaluate("close_position", close_context)
        verdict_payload = _lifecycle_build_holding_timeout_verdict_payload(
            position_id=pid,
            decision_ts=time.time(),
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
            result = bridge.close_position(pid)
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
) -> dict[str, Any]:
    if not pos or bridge is None or cfg is None:
        return {"timeout": [], "entry_repair": [], "supervisor": [], "trailing_applied": [], "trailing_superseded": []}

    trailing_candidates: list[ProtectionCandidate] = []
    if atr_price > 0:
        trailing_candidates = _update_trailing_stops(
            bridge,
            pos,
            current_price,
            pipeline,
            atr_price,
            tick,
            log,
        )

    timeout_handled = _enforce_holding_timeout(
        bridge,
        pos,
        cfg=cfg,
        tick=tick,
        log=log,
    )
    entry_repair_candidates = _entry_protection_repair_candidates(
        pos,
        current_price=current_price,
        tick=tick,
    )
    entry_repair_applied: set[int] = set()
    for candidate in sorted(entry_repair_candidates, key=lambda item: item.priority):
        if candidate.position_id in timeout_handled:
            _log_protection_candidate_superseded(candidate, cfg=cfg, tick=tick, reason="holding_timeout", acct=acct)
            continue
        if _execute_trailing_candidate(candidate, bridge=bridge, cfg=cfg, tick=tick, log=log, acct=acct):
            entry_repair_applied.add(candidate.position_id)

    supervisor_handled = _run_position_supervision(
        bridge,
        pos,
        cfg=cfg,
        acct=acct,
        tick=tick,
        log=log,
        skip_position_ids=set(timeout_handled) | set(entry_repair_applied),
    )
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
            continue
        if _execute_trailing_candidate(candidate, bridge=bridge, cfg=cfg, tick=tick, log=log, acct=acct):
            trailing_applied.add(candidate.position_id)
            protected_pids.add(candidate.position_id)

    return _lifecycle_build_position_protection_cycle_result(
        timeout_handled=set(timeout_handled),
        entry_repair_applied=entry_repair_applied,
        supervisor_handled=set(supervisor_handled),
        trailing_applied=trailing_applied,
        trailing_superseded=trailing_superseded,
    )


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
