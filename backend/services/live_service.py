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
from backend.services.position_metrics import normalize_path_state, update_position_path_metrics
from backend.services.position_supervisor import evaluate_position_supervisor
_DECISION_LOG: DecisionLogStore | None = None
_DECISION_LOG_RUN_ID: int = 0
_LEDGER: DecisionLedger | None = None
_TRADE_REVIEWER: TradeReviewer | None = None
_EXPERIENCE_BUILDER: ExperienceBuilder | None = None
_POLICY_SUGGESTER: PolicySuggester | None = None
_RISK_POLICY = RiskPolicyService.shared()

# ── Local SL/TP tracking (live loop only) ──────────────────────────
# audit 2026-06-10: 之前 SL/TP 完全靠本地 Python 监控 1 bar 延迟的
# check_sl_tp(), 实际 market_buy 时 bridge 协议不传 SL/TP 字段
# (MARKET 单限制). 改成: market_buy 成交后立即 amend_position_sltp 推
# server. _local_positions 跟踪每个 position_id 的 SL/TP, amend 成功后
# 覆盖, amend 失败时保留旧值(下次 tick 重试).
from dataclasses import dataclass

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

_local_positions: dict[int, _LocalSLTP] = {}
_local_positions_lock = threading.Lock()

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
# ── 追踪止损状态 ──
# position_id → {best_price, activated, entry_price, direction}
_trailing_state: dict[int, dict] = {}
# ── 金字塔规则: position_id → 开仓时的 composite.score
# 用于判断新信号是否比已有持仓更强, 避免递减加仓
_pos_entry_scores: dict[int, float] = {}
_pos_entry_decisions: dict[int, str] = {}
_pending_close_reasons: dict[int, str] = {}
_pending_close_verdicts: dict[int, dict] = {}

_RUNTIME_KV_LOOP_DESIRED = "live.loop.desired_state"
_RUNTIME_KV_LAST_SHUTDOWN = "live.loop.last_shutdown"
_RECOVERY_CONTEXT_PARTIAL = "partial"
_RECOVERY_CONTEXT_FULL = "full"
_RECOVERY_REPLAY_LOOKBACK_SEC = 7 * 24 * 3600
_AUTO_RESUME_DELAY_SEC = 4.0


# ═══════════════════════════════════════════════════════════
# 风控集成: VaR 闸门 + Kelly 仓位
# ═══════════════════════════════════════════════════════════

def _risk_var_gate(cfg) -> tuple[bool, str]:
    """VaR 闸门: 检查当前 VaR 是否超过阈值。

    Returns:
        (passed, reason)
        当 cfg.var_enabled=False 或数据不足时, passed=True (不阻挡)。
    """
    if not getattr(cfg, 'var_enabled', False):
        return True, ""
    var_data = _live_state_get("risk", {}, clone=True).get("var", {})
    var_pct = var_data.get("var_pct", 0) or 0
    threshold_pct = getattr(cfg, 'var_cvar_threshold', 0.02) * 100  # 0.02 → 2%
    if var_pct > threshold_pct:
        return (
            False,
            f"var_gate: VaR={var_pct:.1f}% > {threshold_pct:.1f}%",
        )
    return True, ""


def _risk_kelly_volume(
    cfg, direction: int, current_price: float, sl_price: float,
    bridge_meta: dict, acct: dict,
) -> float:
    """根据 Kelly 分数计算 API 原生开仓量。

    返回值使用 cTrader API volume unit；XAUUSD 常见最小开仓量约为 100 API units。
    """
    _min_vol = float(bridge_meta.get('api_min_volume') or 1.0)
    _step_vol = float(bridge_meta.get('api_step_volume') or 1.0)

    def _to_step(v: float) -> float:
        if _step_vol <= 0:
            return max(_min_vol, v)
        return max(_min_vol, round(v / _step_vol) * _step_vol)

    default_vol = _to_step(_min_vol)
    if not getattr(cfg, 'kelly_enabled', False):
        return default_vol

    kelly_data = _live_state_get("risk", {}, clone=True).get("kelly", {})
    kelly_f = kelly_data.get("kelly_fraction", 0) or 0
    if kelly_f <= 0:
        return default_vol

    equity = float(acct.get("equity", 0) or 0)
    if equity <= 0:
        return default_vol

    # Kelly 乘数 (半凯利/四分之一凯利)
    kelly_mult = getattr(cfg, 'kelly_fraction', 0.5)
    f_star = kelly_f * kelly_mult

    # 每笔风险敞口 = equity × risk_pct
    risk_pct = getattr(cfg, 'kelly_risk_per_trade_pct', 0.01)
    risk_capital = equity * risk_pct

    # SL 距离 (价格单位)
    sl_dist = abs(current_price - sl_price)
    sl_dist = max(sl_dist, current_price * 0.001)  # 至少 0.1% 价格波动

    # XAUUSD 合约乘数: 100 oz
    contract_mult = 100.0
    raw_api_volume = f_star * risk_capital / (sl_dist * contract_mult) if sl_dist > 0 else 0

    # 资金占比上限 (API volume)
    max_pct = getattr(cfg, 'kelly_max_pct', 0.25)
    max_api_volume_calc = equity * max_pct / (sl_dist * contract_mult) if sl_dist > 0 else default_vol * 10 / 100.0

    vol_api = max(_min_vol, min(max_api_volume_calc * 100.0, raw_api_volume * 100.0, default_vol * 5))
    return _to_step(vol_api)


def _protection_prices_from_reference(
    direction: int,
    reference_price: float,
    sl_dist: float,
    tp_dist: float,
    digits: int = 2,
) -> tuple[float, float]:
    """Compute SL/TP from the freshest executable reference price."""
    ref = float(reference_price or 0.0)
    sl_delta = abs(float(sl_dist or 0.0))
    tp_delta = abs(float(tp_dist or 0.0))
    if direction == 1:
        sl_price = ref - sl_delta
        tp_price = ref + tp_delta
    else:
        sl_price = ref + sl_delta
        tp_price = ref - tp_delta
    return round(float(sl_price), int(digits)), round(float(tp_price), int(digits))


def _position_api_volume(pos: Any) -> float:
    """Extract the canonical API volume from a position payload.

    The live stack should use the broker-returned volume field directly and
    avoid falling back to legacy unit aliases when doing risk and sizing
    math.
    """
    if pos is None:
        return 0.0
    if hasattr(pos, 'get'):
        for key in ('volume', 'api_volume'):
            try:
                value = pos.get(key)
            except Exception:
                value = None
            if value is not None:
                return float(value)
        return 0.0
    for key in ('volume', 'api_volume'):
        value = getattr(pos, key, None)
        if value is not None:
            return float(value)
    return 0.0


def _tracked_total_api_volume(positions: list[Any]) -> float:
    total_api_volume = 0.0
    for item in positions or []:
        if hasattr(item, 'get'):
            pid = item.get('position_id') or item.get('ticket')
        else:
            pid = getattr(item, 'position_id', None) or getattr(item, 'ticket', None)
        if pid is not None and int(pid) in _pos_open_api_volume:
            total_api_volume += float(_pos_open_api_volume[int(pid)])
            continue
        total_api_volume += _position_api_volume(item)
    return float(total_api_volume)


def _max_abs_entry_score_for_positions(positions: list[Any]) -> float:
    max_entry = 0.0
    for item in positions or []:
        if hasattr(item, 'get'):
            pid = item.get("position_id") or item.get("ticket")
        else:
            pid = getattr(item, "position_id", None) or getattr(item, "ticket", None)
        if pid is None:
            continue
        entry_score = _pos_entry_scores.get(int(pid))
        if entry_score is not None and abs(entry_score) > abs(max_entry):
            max_entry = float(entry_score)
    return abs(float(max_entry))


def _build_open_trade_risk_context(
    *,
    cfg,
    bridge,
    acct: dict,
    positions: list[Any],
    requested_api_volume: float,
    signal_score: float,
) -> dict:
    risk_snapshot = _live_state_get("risk", {}, clone=True) or {}
    loop_running = bool(_live_state_get("loop_running", True))
    bridge_connected = bool(getattr(bridge, "is_connected", False))
    account_updated_at = float(_live_state_get("account_updated_at", 0.0) or 0.0)
    positions_updated_at = float(_live_state_get("positions_updated_at", 0.0) or 0.0)
    now = time.time()
    account_cache_age_seconds = max(0.0, now - account_updated_at) if account_updated_at > 0 else 0.0
    positions_cache_age_seconds = max(0.0, now - positions_updated_at) if positions_updated_at > 0 else 0.0
    sync_snapshot = {}
    data_lag_seconds = 0.0
    system_health_snapshot = {}
    try:
        from data.live_sync.health import SyncHealth

        sync_health = SyncHealth.shared()
        sync_snapshot = sync_health.snapshot()
        data_lag_seconds = float(
            sync_health.last_bar_age_seconds(str(getattr(cfg, "timeframe", "M5") or "M5")) or 0.0
        )
    except Exception:
        sync_snapshot = {}
    try:
        from monitor.system_health import shared as _system_health_shared

        report = _system_health_shared().get_last_report()
        if report is not None:
            system_health_snapshot = {
                "overall": str(getattr(report, "overall", "") or ""),
                "overall_score": float(getattr(report, "overall_score", 0.0) or 0.0),
                "component_status": {
                    str(name): str(getattr(component, "status", "") or "")
                    for name, component in (getattr(report, "components", {}) or {}).items()
                },
                "critical_components": [
                    str(name)
                    for name, component in (getattr(report, "components", {}) or {}).items()
                    if str(getattr(component, "status", "") or "") == "critical"
                ],
                "degraded_components": [
                    str(name)
                    for name, component in (getattr(report, "components", {}) or {}).items()
                    if str(getattr(component, "status", "") or "") == "degraded"
                ],
            }
    except Exception:
        system_health_snapshot = {}
    timeframe = str(getattr(cfg, "timeframe", "M5") or "M5")
    temporal_context = _temporal_context_for_trade(
        decision_ts=now,
        timeframe=timeframe,
        session_last_trade_ts=float(_live_state_get("session_last_trade_ts", 0.0) or 0.0),
        loop_started_at=float(_live_state_get("loop_started_at", 0.0) or 0.0),
    )

    return {
        "account": acct or {},
        "session": {
            "pnl": float(_live_state_get("session_pnl", 0.0) or 0.0),
            "start_balance": float(_live_state_get("session_start_balance", 0.0) or 0.0),
            "trades": int(_live_state_get("session_trades", 0) or 0),
            "consecutive_losses": int(_live_state_get("session_consecutive_loss", 0) or 0),
            "drawdown_pct": float(_live_state_get("session_max_drawdown_pct", 0.0) or 0.0),
            "circuit_breaker": bool(_live_state_get("circuit_breaker", False)),
        },
        "risk_snapshot": risk_snapshot,
        "var": {
            "enabled": bool(getattr(cfg, "var_enabled", False)),
            "threshold_pct": float(getattr(cfg, "var_cvar_threshold", 0.02) or 0.02) * 100.0,
        },
        "open_position_count": len(positions or []),
        "max_position_count": int(getattr(cfg, "max_position_count", 3) or 0),
        "total_api_volume": _tracked_total_api_volume(positions or []),
        "requested_api_volume": float(requested_api_volume or 0.0),
        "max_position_api_volume": float(getattr(cfg, "max_position_api_volume", 1000.0) or 0.0),
        "pyramid_enabled": bool(getattr(cfg, "pyramid_enabled", True)),
        "max_abs_entry_score": _max_abs_entry_score_for_positions(positions or []),
        "signal_score": float(signal_score or 0.0),
        "loop_running": loop_running,
        "bridge_connected": bridge_connected,
        "data_lag_seconds": data_lag_seconds,
        "runtime_health": {
            "account_cache_age_seconds": account_cache_age_seconds,
            "positions_cache_age_seconds": positions_cache_age_seconds,
            "sync_health": sync_snapshot,
            "system_health": system_health_snapshot,
        },
        "loss_cooldown_after_losses": int(getattr(cfg, "risk_loss_cooldown_after_losses", 0) or 0),
        "loss_cooldown_bars": int(getattr(cfg, "risk_loss_cooldown_bars", 0) or 0),
        "block_on_disk_critical": bool(getattr(cfg, "risk_block_on_disk_critical", True)),
        "require_l2_depth": bool(getattr(cfg, "risk_require_l2_depth", False)),
        "temporal_context": temporal_context,
    }


def _risk_state_with_verdict(verdict) -> dict:
    state = _live_state_get("risk", {}, clone=True) or {}
    try:
        state["policy_verdict"] = verdict.to_dict()
    except Exception:
        state["policy_verdict"] = {"allowed": False, "reason": "verdict_serialization_failed"}
    return state


def _position_open_price(pos: Any) -> float:
    """Extract broker-reported open/entry price from a position payload."""
    if pos is None:
        return 0.0
    if isinstance(pos, dict):
        candidates = (
            pos.get("open_price"),
            pos.get("entry_price"),
            pos.get("price"),
        )
    else:
        candidates = (
            getattr(pos, "open_price", None),
            getattr(pos, "entry_price", None),
            getattr(pos, "price", None),
        )
    for value in candidates:
        try:
            price = float(value or 0.0)
        except Exception:
            continue
        if price > 0:
            return price
    return 0.0


def _position_open_timestamp(pos: Any) -> float:
    if pos is None:
        return 0.0
    if isinstance(pos, dict):
        candidates = (
            pos.get("open_time"),
            pos.get("open_timestamp"),
            pos.get("open_ts"),
        )
    else:
        candidates = (
            getattr(pos, "open_time", None),
            getattr(pos, "open_timestamp", None),
            getattr(pos, "open_ts", None),
        )
    for value in candidates:
        try:
            ts = float(value or 0.0)
        except Exception:
            continue
        if ts <= 0:
            continue
        if ts > 10_000_000_000:
            ts /= 1000.0
        if ts > 0:
            return ts
    return 0.0


def _classify_trading_session(hour_utc: int) -> str:
    if 0 <= hour_utc < 7:
        return "asia"
    if 7 <= hour_utc < 13:
        return "europe"
    if 13 <= hour_utc < 21:
        return "us"
    return "rollover"


def _timeframe_seconds(timeframe: str) -> int:
    mapping = {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
    }
    return mapping.get(str(timeframe or "").upper(), 0)


def _temporal_context_for_trade(
    *,
    decision_ts: float,
    timeframe: str,
    session_last_trade_ts: float = 0.0,
    loop_started_at: float = 0.0,
) -> dict:
    ts = float(decision_ts or time.time())
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    tf_seconds = _timeframe_seconds(timeframe)
    last_trade_gap = max(0.0, ts - session_last_trade_ts) if session_last_trade_ts > 0 else 0.0
    loop_uptime = max(0.0, ts - loop_started_at) if loop_started_at > 0 else 0.0
    return {
        "decision_ts": ts,
        "timeframe": str(timeframe or ""),
        "timeframe_seconds": tf_seconds,
        "hour_utc": int(dt.hour),
        "minute_utc": int(dt.minute),
        "weekday_utc": int(dt.weekday()),
        "session_label": _classify_trading_session(int(dt.hour)),
        "is_weekend_utc": bool(dt.weekday() >= 5),
        "seconds_since_last_trade": round(last_trade_gap, 3),
        "bars_since_last_trade": round(last_trade_gap / tf_seconds, 3) if tf_seconds > 0 and last_trade_gap > 0 else 0.0,
        "loop_uptime_seconds": round(loop_uptime, 3),
    }


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
    holding_seconds = max(0.0, now - entry_ts) if entry_ts > 0 else 0.0
    max_holding_bars = int(getattr(cfg, "risk_max_holding_bars", 0) or 0)
    timeframe_seconds = int(temporal_context.get("timeframe_seconds", 0) or 0)
    max_holding_seconds = float(max_holding_bars * timeframe_seconds) if max_holding_bars > 0 and timeframe_seconds > 0 else 0.0
    return {
        "position_id": str(position_id),
        "close_reason": close_reason,
        "mode": mode,
        "broker": broker,
        "symbol": symbol,
        "entry_ts": entry_ts,
        "entry_ts_source": str(open_meta.get("source") or ("broker_position" if position is not None else "")),
        "holding_seconds": holding_seconds,
        "timeframe_seconds": timeframe_seconds,
        "max_holding_bars": max_holding_bars,
        "max_holding_seconds": max_holding_seconds,
        "temporal_context": temporal_context,
    }


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
    holding_seconds = float(close_context.get("holding_seconds", 0.0) or 0.0)
    max_holding_seconds = float(close_context.get("max_holding_seconds", 0.0) or 0.0)
    timeout_enabled = bool(max_holding_seconds > 0)
    timeout_ratio = (holding_seconds / max_holding_seconds) if timeout_enabled and max_holding_seconds > 0 else 0.0
    if not timeout_enabled:
        timeout_status = "disabled"
    elif holding_seconds >= max_holding_seconds:
        timeout_status = "expired"
    elif timeout_ratio >= 0.8:
        timeout_status = "watch"
    else:
        timeout_status = "normal"
    remaining_seconds = max(0.0, max_holding_seconds - holding_seconds) if timeout_enabled else 0.0
    return {
        "holding_seconds": round(holding_seconds, 3),
        "holding_minutes": round(holding_seconds / 60.0, 2) if holding_seconds > 0 else 0.0,
        "timeout_enabled": timeout_enabled,
        "max_holding_bars": int(close_context.get("max_holding_bars", 0) or 0),
        "max_holding_seconds": round(max_holding_seconds, 3) if max_holding_seconds > 0 else 0.0,
        "holding_timeout_exceeded": bool(close_context.get("max_holding_seconds", 0.0) and holding_seconds >= max_holding_seconds),
        "holding_timeout_ratio": round(timeout_ratio, 4) if timeout_enabled else 0.0,
        "holding_timeout_status": timeout_status,
        "holding_timeout_remaining_seconds": round(remaining_seconds, 3) if timeout_enabled else 0.0,
    }


def _position_unrealized_pnl(position: Any) -> float:
    if isinstance(position, dict):
        candidates = (
            position.get("profit"),
            position.get("pnl"),
            position.get("unrealized_pnl"),
        )
    else:
        candidates = (
            getattr(position, "profit", None),
            getattr(position, "pnl", None),
            getattr(position, "unrealized_pnl", None),
        )
    for value in candidates:
        try:
            return float(value or 0.0)
        except Exception:
            continue
    return 0.0


def _load_recovery_position_row(position_id: int) -> dict[str, Any]:
    if position_id <= 0:
        return {}
    conn = _get_state_conn()
    try:
        row = conn.execute(
            """
            SELECT *
            FROM recovery_position_state
            WHERE position_id=?
            LIMIT 1
            """,
            (int(position_id),),
        ).fetchone()
        if row is None:
            return {}
        item = dict(row)
        try:
            item["recovery_meta"] = json.loads(item.get("recovery_meta_json") or "{}")
        except Exception:
            item["recovery_meta"] = {}
        return item
    finally:
        conn.close()


def _current_regime_hint() -> str:
    composite = _live_state_get("last_composite", clone=True) or {}
    if isinstance(composite, dict):
        for key in ("regime_id", "regime", "regime_state"):
            value = composite.get(key)
            if value:
                return str(value)
    return ""


def _position_path_metrics_for_position(
    position: Any,
    *,
    cfg=None,
    now_ts: float | None = None,
    persist: bool = False,
    broker: str = "",
    strategy_name: str = "",
) -> dict[str, Any]:
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

    holding = _holding_summary_for_position(position, cfg=cfg, now_ts=now_ts)
    recovery_row = _load_recovery_position_row(pid)
    recovery_meta = recovery_row.get("recovery_meta") if recovery_row else {}
    path_state = normalize_path_state((recovery_meta or {}).get("position_path"))
    entry_ctx = _lookup_open_decision_context(pid)
    entry_regime = str((recovery_meta or {}).get("entry_regime") or "")
    current_regime = _current_regime_hint()
    current_pnl = _position_unrealized_pnl(position)
    next_state, metrics = update_position_path_metrics(
        previous_state=path_state,
        current_pnl=current_pnl,
        now_ts=float(now_ts or time.time()),
        holding_seconds=float(holding.get("holding_seconds", 0.0) or 0.0),
        max_holding_seconds=float(holding.get("max_holding_seconds", 0.0) or 0.0),
        entry_regime=entry_regime,
        current_regime=current_regime,
    )

    result = {
        **metrics,
        "time_in_profit": round(metrics["time_in_profit_seconds"], 6),
        "entry_regime": entry_regime,
        "current_regime": current_regime,
        "entry_ts_source": str(entry_ctx.get("source") or ""),
    }

    if persist:
        next_meta = dict(recovery_meta or {})
        next_meta["position_path"] = next_state
        if entry_regime:
            next_meta["entry_regime"] = entry_regime
        if current_regime:
            next_meta["current_regime"] = current_regime
        _upsert_recovery_position_state(
            position,
            broker=broker or str(recovery_row.get("broker") or "ctrader"),
            strategy_name=strategy_name or str(recovery_row.get("strategy_name") or _loop_strategy_name or "factor_v4"),
            status=str(recovery_row.get("status") or "open"),
            context_integrity=str(recovery_row.get("context_integrity") or _RECOVERY_CONTEXT_FULL),
            meta=next_meta,
        )
    return result


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
    holding_timeout_ratio = float(position.get("holding_timeout_ratio", 0.0) or 0.0)
    market_space_context = {
        "distance_to_sl": round(
            abs(float(position.get("current_price", position.get("price_current", 0.0)) or 0.0) - float(position.get("sl", 0.0) or 0.0)),
            6,
        ) if float(position.get("sl", 0.0) or 0.0) > 0 else 0.0,
        "distance_to_tp": round(
            abs(float(position.get("tp", 0.0) or 0.0) - float(position.get("current_price", position.get("price_current", 0.0)) or 0.0)),
            6,
        ) if float(position.get("tp", 0.0) or 0.0) > 0 else 0.0,
        "atr_multiple_from_entry": 0.0,
        "range_location": 0.0,
        "structure_bias": "",
    }
    entry_ctx = {
        "entry_decision_id": _lookup_entry_decision_id(int(position.get("position_id") or position.get("ticket") or 0)),
        "entry_score": 0.0,
        "entry_reason": "",
        "factor_set_version": "",
        "policy_version": "",
        "expected_holding_profile": "",
        "entry_regime": position_metrics.get("entry_regime", ""),
        "entry_regime_confidence": 0.0,
    }
    risk_context = {
        "risk_snapshot": _live_state_get("risk", {}, clone=True) or {},
        "policy_state": {},
        "max_holding_bars": int(getattr(cfg, "risk_max_holding_bars", 0) or 0) if cfg else 0,
        "max_holding_seconds": float(position.get("max_holding_seconds", 0.0) or 0.0),
        "open_position_count": len(positions or []),
        "total_api_volume": _tracked_total_api_volume(positions or []),
        "holding_timeout_ratio": holding_timeout_ratio,
        **position_metrics,
    }
    return {
        "position": {
            "position_id": position.get("position_id") or position.get("ticket"),
            "trade_id": str(position.get("position_id") or position.get("ticket") or ""),
            "symbol": str(position.get("symbol") or "XAUUSD+"),
            "direction": int(position.get("direction", 0) or 0),
            "entry_price": float(position.get("entry_price", position.get("open_price", position.get("price_open", 0.0))) or 0.0),
            "current_price": float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
            "volume": float(position.get("volume", position.get("api_volume", 0.0)) or 0.0),
            "opened_at": float(position.get("open_time", 0.0) or 0.0),
            "unrealized_pnl": float(position.get("profit", position.get("pnl", 0.0)) or 0.0),
            "realized_pnl": 0.0,
            "stop_loss": float(position.get("sl", 0.0) or 0.0),
            "take_profit": float(position.get("tp", 0.0) or 0.0),
            "type": str(position.get("type") or ""),
            "sl": float(position.get("sl", 0.0) or 0.0),
            "tp": float(position.get("tp", 0.0) or 0.0),
        },
        "market": {
            "bid": float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
            "ask": float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
            "mid": float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
            "spread": 0.0,
            "timeframe": str(temporal_context.get("temporal_context", {}).get("timeframe", "") or temporal_context.get("timeframe", "")),
            "timeframe_seconds": int(temporal_context.get("timeframe_seconds", 0) or 0),
            "regime_state": position_metrics.get("current_regime", ""),
            "volatility_state": "",
        },
        "risk": risk_context,
        "temporal_context": {
            **(temporal_context.get("temporal_context") or {}),
            "holding_seconds": temporal_context.get("holding_seconds", 0.0),
            "holding_minutes": round(float(temporal_context.get("holding_seconds", 0.0) or 0.0) / 60.0, 3),
            "holding_bars": round(
                float(temporal_context.get("holding_seconds", 0.0) or 0.0) / max(int(temporal_context.get("timeframe_seconds", 0) or 1), 1),
                3,
            ) if int(temporal_context.get("timeframe_seconds", 0) or 0) > 0 else 0.0,
        },
        "market_space_context": market_space_context,
        "entry_context": entry_ctx,
        "runtime": {
            "loop_running": bool(_live_state_get("loop_running", True)),
            "bridge_connected": True,
            "data_quality_state": "",
            "runtime_health": {},
            "account": acct or {},
        },
    }


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
        meta = dict((row or {}).get("recovery_meta") or {})
        meta["latest_supervisor"] = verdict
        _upsert_recovery_position_state(
            position,
            broker=broker or str((row or {}).get("broker") or "ctrader"),
            strategy_name=strategy_name or str((row or {}).get("strategy_name") or _loop_strategy_name or "factor_v4"),
            status=str((row or {}).get("status") or "open"),
            context_integrity=str((row or {}).get("context_integrity") or _RECOVERY_CONTEXT_FULL),
            meta=meta,
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
    enriched: list[dict] = []
    for raw in _coerce_live_positions(pos_list):
        item = dict(raw)
        item.update(_holding_summary_for_position(item, cfg=cfg, now_ts=now_ts))
        item.update(
            _position_path_metrics_for_position(
                item,
                cfg=cfg,
                now_ts=now_ts,
                persist=persist,
                broker=broker,
                strategy_name=strategy_name,
            )
        )
        item["supervisor"] = _evaluate_position_supervisor_for_position(
            item,
            cfg=cfg,
            now_ts=now_ts,
            positions=pos_list,
            persist=persist,
            broker=broker,
            strategy_name=strategy_name,
        )
        item["supervisor_action"] = item["supervisor"].get("action")
        item["supervisor_label"] = item["supervisor"].get("action_label")
        item["supervisor_reason"] = item["supervisor"].get("summary_reason")
        item["supervisor_summary"] = item["supervisor"].get("human_summary")
        enriched.append(item)
    return enriched


def _supervisor_risk_context(
    position: dict[str, Any],
    verdict: dict[str, Any],
    *,
    cfg=None,
    mode: str = "live",
) -> dict[str, Any]:
    close_context = _build_close_position_risk_context(
        position_id=int(position.get("position_id") or position.get("ticket") or 0),
        close_reason=str((verdict.get("recommended_controls") or {}).get("close_reason") or verdict.get("summary_reason") or ""),
        mode=mode,
        broker="ctrader",
        symbol=str(position.get("symbol") or "XAUUSD+"),
        position=position,
        cfg=cfg,
    )
    close_context.update(
        {
            "supervisor_action": verdict.get("action"),
            "supervisor_confidence": verdict.get("confidence"),
            "supervisor_reason": verdict.get("summary_reason"),
            "supervisor_evidence": verdict.get("evidence") or {},
            "supervisor_decision_ts": verdict.get("decision_ts"),
            "recommended_controls": verdict.get("recommended_controls") or {},
            "position_id": str(position.get("position_id") or position.get("ticket") or ""),
        }
    )
    return close_context


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
    meta = dict((row or {}).get("recovery_meta") or {})
    meta["latest_supervisor"] = verdict
    if action_applied:
        meta["last_supervisor_applied_action"] = action_applied
        meta["last_supervisor_applied_ts"] = time.time()
        meta["last_supervisor_reason"] = verdict.get("summary_reason")
    _upsert_recovery_position_state(
        position,
        broker=broker,
        strategy_name=strategy_name or str((row or {}).get("strategy_name") or _loop_strategy_name or "factor_v4"),
        status=str((row or {}).get("status") or "open"),
        context_integrity=str((row or {}).get("context_integrity") or _RECOVERY_CONTEXT_FULL),
        meta=meta,
    )


def _supervisor_recently_applied(position_id: int, action: str, cooldown_seconds: float = 300.0) -> bool:
    row = _load_recovery_position_row(position_id)
    meta = dict((row or {}).get("recovery_meta") or {})
    if str(meta.get("last_supervisor_applied_action") or "") != str(action or ""):
        return False
    last_ts = float(meta.get("last_supervisor_applied_ts", 0.0) or 0.0)
    return last_ts > 0 and (time.time() - last_ts) < cooldown_seconds


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
            event_type=event_type,
            symbol=str(position.get("symbol") or "XAUUSD+"),
            timeframe=str(getattr(cfg, "timeframe", "") or ""),
            trade_id=str(position.get("position_id") or position.get("ticket") or ""),
            position_id=str(position.get("position_id") or position.get("ticket") or ""),
            decision_ts=float(verdict.get("decision_ts") or time.time()),
            portfolio_state={
                "balance": (acct or {}).get("balance", 0.0),
                "equity": (acct or {}).get("equity", 0.0),
                "session_pnl": _live_state_get("session_pnl", 0.0),
            },
            risk_state=_risk_state_with_verdict_dict(risk_verdict or {}),
            action_score=float(verdict.get("confidence", 0.0) or 0.0),
            action_reason=str(verdict.get("summary_reason") or event_type),
            action_json={
                "tick": tick,
                "supervisor_verdict": verdict,
                "risk_verdict": risk_verdict or {},
            },
        )
    except Exception as exc:
        logger.debug("[live] supervisor ledger failed for pos %s: %s", position.get("position_id"), exc)
        return ""


def _run_position_supervision(
    bridge,
    pos: list,
    *,
    cfg,
    acct: dict,
    tick: int,
    log,
) -> None:
    if not pos or bridge is None:
        return
    for raw in pos or []:
        position = dict(raw)
        pid = int(position.get("position_id") or position.get("ticket") or 0)
        if pid <= 0:
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
            continue
        if _supervisor_recently_applied(pid, action):
            continue

        risk_action = {
            "tighten": "tighten_position",
            "reduce": "reduce_position",
            "close": "close_position",
        }.get(action)
        if not risk_action:
            continue
        risk_context = _supervisor_risk_context(position, verdict, cfg=cfg)
        risk_verdict = _RISK_POLICY.evaluate(risk_action, risk_context).to_dict()
        _log_supervisor_decision(
            position=position,
            verdict=verdict,
            risk_verdict=risk_verdict,
            acct=acct,
            cfg=cfg,
            event_type=f"supervisor_{action}",
            tick=tick,
        )
        if not risk_verdict.get("allowed", False):
            _remember_supervisor_state(position, verdict, broker="ctrader", strategy_name=str(_loop_strategy_name or "factor_v4"))
            continue

        controls = verdict.get("recommended_controls") or {}
        try:
            if action == "tighten":
                target_sl = float(controls.get("target_stop_loss", 0.0) or 0.0)
                current_sl = float(position.get("sl", 0.0) or 0.0)
                current_tp = float(position.get("tp", 0.0) or 0.0)
                if target_sl > 0 and abs(target_sl - current_sl) >= 0.01:
                    amend_res = bridge.amend_position_sltp(pid, sl=target_sl, tp=current_tp)
                    if getattr(amend_res, "success", False):
                        _track_local_sl_tp(pid, sl=target_sl, tp=current_tp)
                        if _LEDGER:
                            _LEDGER.log_position_event(
                                position_id=str(pid),
                                trade_id=str(pid),
                                symbol=str(position.get("symbol") or "XAUUSD+"),
                                event_type="tightened",
                                net_volume=float(position.get("volume", 0.0) or 0.0),
                                avg_price=float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
                                details={
                                    "supervisor_action": action,
                                    "supervisor_reason": verdict.get("summary_reason"),
                                    "risk_verdict_reason": risk_verdict.get("reason"),
                                    "applied_controls": controls,
                                },
                            )
                        _remember_supervisor_state(position, verdict, action_applied=action, broker="ctrader", strategy_name=str(_loop_strategy_name or "factor_v4"))
                        log(f"tick {tick}: supervisor tighten pos={pid} sl->{target_sl:.2f}")
            elif action == "reduce":
                current_volume = float(position.get("volume", position.get("api_volume", 0.0)) or 0.0)
                reduce_fraction = float(controls.get("reduce_fraction", 0.0) or 0.0)
                reduce_volume = max(0.0, round(current_volume * reduce_fraction))
                if reduce_volume > 0 and current_volume - reduce_volume >= 1.0:
                    result = bridge.close_position(pid, volume=reduce_volume)
                    if getattr(result, "success", False):
                        if _LEDGER:
                            _LEDGER.log_position_event(
                                position_id=str(pid),
                                trade_id=str(pid),
                                symbol=str(position.get("symbol") or "XAUUSD+"),
                                event_type="reduced",
                                net_volume=max(0.0, current_volume - reduce_volume),
                                avg_price=float(position.get("current_price", position.get("price_current", 0.0)) or 0.0),
                                details={
                                    "supervisor_action": action,
                                    "supervisor_reason": verdict.get("summary_reason"),
                                    "risk_verdict_reason": risk_verdict.get("reason"),
                                    "applied_controls": {**controls, "reduce_volume": reduce_volume},
                                },
                            )
                        _remember_supervisor_state(position, verdict, action_applied=action, broker="ctrader", strategy_name=str(_loop_strategy_name or "factor_v4"))
                        log(f"tick {tick}: supervisor reduce pos={pid} vol={reduce_volume:.0f}")
            elif action == "close":
                _remember_close_reason(pid, str(controls.get("close_reason") or verdict.get("summary_reason") or "supervisor_close"))
                _remember_close_verdict(
                    pid,
                    type(
                        "SupervisorVerdictProxy",
                        (),
                        {
                            "to_dict": lambda _self: risk_verdict,
                        },
                    )(),
                )
                result = bridge.close_position(pid)
                if getattr(result, "success", False):
                    _remember_supervisor_state(position, verdict, action_applied=action, broker="ctrader", strategy_name=str(_loop_strategy_name or "factor_v4"))
                    log(f"tick {tick}: supervisor close sent pos={pid} reason={verdict.get('summary_reason')}")
        except Exception as exc:
            logger.debug("[live] supervisor action %s failed for pos %s: %s", action, pid, exc)


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
    """Persist param tune state to state.db + JSON backup."""
    import json, time as _time
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent.parent / "data" / "param_tune_state.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_PARAM_TUNE_STATE, indent=2, default=str))
    except Exception as e:
        logger.warning("Failed to save param tune state: %s", e)

    try:
        from backend.core.db import get_state_conn
        conn = get_state_conn()
        try:
            for key, val in _PARAM_TUNE_STATE.items():
                conn.execute(
                    "INSERT OR REPLACE INTO param_tune (key, value_json, updated_at) VALUES (?, ?, ?)",
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
_live_state: dict = {
    "broker": "ctrader",    # 唯一执行/数据通道
    "loop_running": False,
    "loop_strategy": None,
    "loop_started_at": None,
    "account": None,         # {balance, equity, currency, ...}
    "account_updated_at": None,
    "positions": [],        # [position, ...]
    "positions_updated_at": None,
    "spot_price": None,      # cTrader spot event
    "session_pnl": 0.0,
    "session_trades": 0,
    "session_winning": 0,
    "session_losing": 0,
    "session_consecutive_loss": 0,
    "session_max_drawdown_pct": 0.0,
    "session_peak_equity": 0.0,
    "session_start_balance": 0.0,
    "session_last_trade_ts": 0.0,
    "circuit_breaker": False,
    "circuit_reason": "",
    "trade_equity_history": [],
    "risk": {
        "var": {},
        "kelly": {},
        "stress": {},
        "concentration": {},
    },
}

# ★ 保护 _live_state 的读-改-写操作 (多线程: HTTP handler + live loop + scheduler)
_LIVE_STATE_LOCK = threading.Lock()


def _live_state_get(key: str, default=None, *, clone: bool = False):
    with _LIVE_STATE_LOCK:
        value = _live_state.get(key, default)
    if clone and isinstance(value, (dict, list, set)):
        return copy.deepcopy(value)
    return value


def _live_state_set(key: str, value) -> None:
    with _LIVE_STATE_LOCK:
        _live_state[key] = value


def _live_state_update(**kwargs) -> None:
    with _LIVE_STATE_LOCK:
        _live_state.update(kwargs)


def _get_state_conn():
    from backend.core.db import get_state_conn

    return get_state_conn()


def _ensure_runtime_kv_schema(conn) -> None:
    from backend.core.db import STATE_DB_DDL

    conn.executescript(STATE_DB_DDL)


def _runtime_kv_get(key: str, default=None):
    conn = _get_state_conn()
    try:
        _ensure_runtime_kv_schema(conn)
        row = conn.execute(
            "SELECT value_json FROM runtime_kv WHERE key=?",
            (key,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return default
    try:
        return json.loads(row["value_json"])
    except Exception:
        return default


def _runtime_kv_set(key: str, value) -> None:
    conn = _get_state_conn()
    try:
        _ensure_runtime_kv_schema(conn)
        conn.execute(
            """
            INSERT INTO runtime_kv(key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json=excluded.value_json,
                updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False, default=str), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _lookup_entry_decision_id(position_id: int) -> str:
    conn = _get_state_conn()
    try:
        row = conn.execute(
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
    conn = _get_state_conn()
    try:
        row = conn.execute(
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
        recovery = conn.execute(
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

    conn = _get_state_conn()
    try:
        row = conn.execute(
            "SELECT * FROM recovery_position_state WHERE position_id=?",
            (position_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return ""

    real_pnl = real_pnl or {}
    open_price = float(row["open_price"] or real_pnl.get("entry_price") or close_price or 0.0)
    volume = float(row["volume"] or 0.0)
    direction = int(row["direction"] or 0)
    symbol = str(row["symbol"] or "XAUUSD+")
    strategy_name = str(row["strategy_name"] or _loop_strategy_name or "factor_v4")
    first_seen_at = float(row["first_seen_at"] or 0.0)
    open_ts = first_seen_at if first_seen_at > 0 else max(0.0, float(close_ts or time.time()) - 1.0)

    try:
        decision_id = _LEDGER.log_decision(
            event_type="open",
            symbol=symbol,
            timeframe="",
            trade_id=str(position_id),
            position_id=str(position_id),
            decision_ts=open_ts,
            portfolio_state={},
            risk_state=_live_state_get("risk", {}, clone=True) or {},
            action_score=0.0,
            action_reason="live_close_open_repair",
            action_json={
                "position_id": int(position_id),
                "broker": broker,
                "strategy_name": strategy_name,
                "price": open_price,
                "volume": volume,
                "direction": direction,
                "close_reason": close_reason,
                "repair_source": "recovery_position_state",
                "context_integrity": str(row["context_integrity"] or _RECOVERY_CONTEXT_PARTIAL),
                "real_pnl": real_pnl,
            },
        )
        _LEDGER.log_position_event(
            position_id=str(position_id),
            trade_id=str(position_id),
            symbol=symbol,
            event_type="opened",
            net_volume=volume,
            avg_price=open_price,
            details={
                "repair_source": "recovery_position_state",
                "close_reason": close_reason,
                "direction": direction,
            },
            event_ts=open_ts,
        )
        _upsert_recovery_position_state(
            {
                "position_id": position_id,
                "symbol": symbol,
                "direction": direction,
                "open_price": open_price,
                "volume": volume,
                "entry_decision_id": decision_id,
            },
            broker=broker,
            strategy_name=strategy_name,
            status=str(row["status"] or "open"),
            context_integrity=str(row["context_integrity"] or _RECOVERY_CONTEXT_PARTIAL),
            meta={"open_repaired_before_close": True, "open_repair_decision_id": decision_id},
        )
        logger.info("[live] repaired missing open ledger before close pos=%s decision=%s", position_id, decision_id)
        return decision_id
    except Exception as exc:
        logger.debug("[live] open ledger repair before close failed for pos %s: %s", position_id, exc)
        return ""


def _lookup_recovery_context_integrity(position_id: int, default: str = _RECOVERY_CONTEXT_FULL) -> str:
    conn = _get_state_conn()
    try:
        row = conn.execute(
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


def _remember_close_reason(position_id: int, reason: str) -> None:
    if position_id <= 0 or not reason:
        return
    _pending_close_reasons[int(position_id)] = reason


def _consume_close_reason(position_id: int, default: str = "broker_close") -> str:
    return _pending_close_reasons.pop(int(position_id), default)


def _remember_close_verdict(position_id: int, verdict) -> None:
    if position_id <= 0 or verdict is None:
        return
    try:
        _pending_close_verdicts[int(position_id)] = verdict.to_dict()
    except Exception:
        _pending_close_verdicts[int(position_id)] = {
            "allowed": False,
            "reason": "verdict_serialization_failed",
        }


def _consume_close_verdict(position_id: int, close_reason: str) -> dict:
    pending = _pending_close_verdicts.pop(int(position_id), None)
    if pending:
        return pending
    close_context = _build_close_position_risk_context(
        position_id=int(position_id),
        close_reason=close_reason,
        mode="live",
    )
    return _RISK_POLICY.evaluate(
        "close_position",
        close_context,
    ).to_dict()


def _risk_state_with_verdict_dict(verdict: dict) -> dict:
    state = _live_state_get("risk", {}, clone=True) or {}
    state["policy_verdict"] = verdict or {"allowed": False, "reason": "missing_verdict"}
    return state


def _normalize_position_snapshot(raw: Any) -> dict:
    if isinstance(raw, dict):
        data = dict(raw)
    else:
        data = {
            "position_id": getattr(raw, "position_id", 0) or getattr(raw, "ticket", 0),
            "ticket": getattr(raw, "ticket", 0) or getattr(raw, "position_id", 0),
            "symbol": getattr(raw, "symbol", ""),
            "type": "buy" if int(getattr(raw, "direction", 0) or 0) >= 0 else "sell",
            "direction": int(getattr(raw, "direction", 0) or 0),
            "open_price": float(getattr(raw, "open_price", 0.0) or getattr(raw, "entry_price", 0.0) or 0.0),
            "entry_price": float(getattr(raw, "entry_price", 0.0) or getattr(raw, "open_price", 0.0) or 0.0),
            "volume": float(getattr(raw, "volume", 0.0) or getattr(raw, "api_volume", 0.0) or 0.0),
        }
    direction = int(data.get("direction") or 0)
    if direction == 0:
        ptype = str(data.get("type") or "").lower()
        direction = 1 if ptype == "buy" else -1 if ptype == "sell" else 0
    position_id = int(data.get("position_id") or data.get("ticket") or 0)
    return {
        "position_id": position_id,
        "symbol": str(data.get("symbol") or ""),
        "direction": direction,
        "open_price": float(data.get("open_price") or data.get("entry_price") or 0.0),
        "volume": float(data.get("api_volume") or data.get("volume") or 0.0),
        "type": str(data.get("type") or ("buy" if direction >= 0 else "sell")),
        "raw": data,
    }


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
    conn = _get_state_conn()
    try:
        prev = conn.execute(
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
        conn.execute(
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
                volume=excluded.volume,
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
    conn = _get_state_conn()
    try:
        rows = conn.execute(
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
    conn = _get_state_conn()
    try:
        row = conn.execute(
            "SELECT recovery_meta_json FROM recovery_position_state WHERE position_id=?",
            (position_id,),
        ).fetchone()
        merged_meta = {}
        if row and row["recovery_meta_json"]:
            try:
                merged_meta = json.loads(row["recovery_meta_json"])
            except Exception:
                merged_meta = {}
        if meta:
            merged_meta.update(meta)
        conn.execute(
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
                float(closed_at),
                close_reason,
                float(close_pnl),
                json.dumps(merged_meta, ensure_ascii=False, default=str),
                int(position_id),
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
    total_pnl = float((real_pnl or {}).get("net", position_state.get("close_pnl", 0.0)) or 0.0)
    close_price = float((real_pnl or {}).get("exec_price", position_state.get("open_price", 0.0)) or 0.0)
    close_ts = float((real_pnl or {}).get("exec_timestamp", time.time()) or time.time())
    context_integrity = str(position_state.get("context_integrity") or _RECOVERY_CONTEXT_PARTIAL)

    _record_session_trade(total_pnl)
    _mark_recovery_position_closed(
        position_id,
        close_reason="restart_replay",
        close_pnl=total_pnl,
        closed_at=close_ts,
        meta={"replayed_at": time.time(), "strategy_name": strategy_name},
    )

    exit_decision_id = ""
    if _LEDGER:
        try:
            exit_decision_id = _LEDGER.log_decision(
                event_type="close",
                symbol=str(position_state.get("symbol") or "XAUUSD+"),
                timeframe="",
                trade_id=str(position_id),
                position_id=str(position_id),
                decision_ts=close_ts,
                portfolio_state={},
                risk_state=_live_state_get("risk", {}, clone=True) or {},
                action_score=float(total_pnl),
                action_reason="restart_replay_close",
                action_json={
                    "position_id": int(position_id),
                    "replayed": True,
                    "close_reason": "restart_replay",
                    "real_pnl": real_pnl or {},
                },
            )
            _LEDGER.log_position_event(
                position_id=str(position_id),
                trade_id=str(position_id),
                symbol=str(position_state.get("symbol") or "XAUUSD+"),
                event_type="closed",
                avg_price=close_price,
                realized_pnl=float(total_pnl),
                details={
                    "replayed": True,
                    "close_reason": "restart_replay",
                    "real_pnl": real_pnl or {},
                },
                event_ts=close_ts,
            )
        except Exception as exc:
            logger.debug("[live] replay close ledger failed for pos %s: %s", position_id, exc)

    if _TRADE_REVIEWER and _EXPERIENCE_BUILDER and _POLICY_SUGGESTER:
        try:
            review = _TRADE_REVIEWER.review_closed_trade(
                position_id=str(position_id),
                pnl=float(total_pnl),
                close_price=close_price,
                close_ts=close_ts,
                contributions={},
                exit_decision_id=exit_decision_id,
                real_pnl=real_pnl,
                close_reason="restart_replay",
                context_integrity=context_integrity,
            )
            if review.get("accepted", True):
                experience = _EXPERIENCE_BUILDER.build_from_review(review)
                _POLICY_SUGGESTER.suggest_from_experience(experience)
        except Exception as exc:
            logger.debug("[live] replay close learning failed for pos %s: %s", position_id, exc)


def _read_positions_for_recovery(bridge) -> list[Any]:
    cached_positions = _live_state_get("positions", clone=True) or []
    if isinstance(cached_positions, dict):
        cached_positions = cached_positions.get("positions", []) or []
    if cached_positions:
        return list(cached_positions)
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
        suffix = f" while {len(active_rows)} persisted positions remain" if active_rows else ""
        log(f"recovery bootstrap deferred: broker returned 0 positions{suffix}")
        return False
    missing_ids = {int(row["position_id"]) for row in active_rows if int(row["position_id"]) not in current_ids}

    if missing_ids:
        from execution.deal_sync import sync_close_deals_batch

        lookback_from = int(
            max(
                0,
                min(float(row.get("last_seen_at") or time.time()) for row in active_rows if int(row["position_id"]) in missing_ids)
                - _RECOVERY_REPLAY_LOOKBACK_SEC,
            )
        )
        conn = _get_state_conn()
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
        session_consecutive_loss=0,
        session_max_drawdown_pct=0.0,
        session_start_balance=start_balance,
        session_last_trade_ts=0.0,
    )


def _evaluate_daily_drawdown() -> dict:
    session_pnl = float(_live_state_get("session_pnl", 0.0) or 0.0)
    start_balance = float(_live_state_get("session_start_balance", 0.0) or 0.0)
    if start_balance <= 0:
        return {"tripped": False, "dd_pct": 0.0, "reason": "", "session_pnl": session_pnl, "start_balance": 0.0}
    dd_pct = abs(session_pnl) / start_balance * 100 if start_balance > 0 else 0.0
    prev_dd = float(_live_state_get("session_max_drawdown_pct", 0.0) or 0.0)
    updates = {"session_max_drawdown_pct": max(prev_dd, dd_pct)}
    tripped = session_pnl < 0 and dd_pct >= 5.0
    reason = f"daily drawdown {dd_pct:.1f}%" if tripped else ""
    if tripped:
        updates["circuit_breaker"] = True
        updates["circuit_reason"] = reason
    _live_state_update(**updates)
    return {
        "tripped": tripped,
        "dd_pct": dd_pct,
        "reason": reason,
        "session_pnl": session_pnl,
        "start_balance": start_balance,
    }


def _record_session_trade(total_pnl: float) -> dict:
    with _LIVE_STATE_LOCK:
        trades = int(_live_state.get("session_trades", 0)) + 1
        winning = int(_live_state.get("session_winning", 0))
        losing = int(_live_state.get("session_losing", 0))
        consecutive_loss = int(_live_state.get("session_consecutive_loss", 0))
        session_pnl = float(_live_state.get("session_pnl", 0.0)) + float(total_pnl)
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
            session_consecutive_loss=consecutive_loss,
            session_pnl=session_pnl,
            session_last_trade_ts=time.time(),
        )
    return {
        "session_trades": trades,
        "session_winning": winning,
        "session_losing": losing,
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
    _reset_session_state_for_new_day()



def _mark_loop_stopped_for_display() -> None:
    _live_state_update(
        loop_running=False,
        loop_strategy=None,
    )


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


def _cache_get_or_refresh(cache: dict, ttl: float, fetcher):
    """读缓存, 过期则调 fetcher 刷新. 带锁防并发刷新. 出错时返旧缓存不抛."""
    now = _time.time()
    cached = cache.get("_data")
    if cached and (now - cached[0]) < ttl:
        return cached[1]
    # 缓存过期: 加锁防重复刷新. 拿到锁后 double-check.
    with _CACHE_LOCK:
        cached = cache.get("_data")
        if cached and (_time.time() - cached[0]) < ttl:
            return cached[1]
        try:
            data = fetcher()
            cache["_data"] = (_time.time(), data)
            return data
        except Exception:
            if cached:
                return cached[1]
            raise


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
    kw = dict(
        client_id=os.getenv("CTRADER_CLIENT_ID", ""),
        client_secret=os.getenv("CTRADER_CLIENT_SECRET", ""),
        access_token=os.getenv("CTRADER_ACCESS_TOKEN", ""),
        account_id=int(os.getenv("CTRADER_ACCOUNT_ID", "0")),
    )
    kw.update(overrides)
    return CTraderBridge(**kw), None


# ── cTrader 连接管理 ──────────────────────────────────────────────
# Twisted reactor 是全局单例, 不能 stop/restart. 每次 create+connect+destroy
# bridge 会导致 reactor 状态污染 (旧 protocol 残留).
# 方案: 进程级长连接 bridge, 所有 cTrader API 复用同一个连接.
# audit 2026-06-10: connect() 之前是同步阻塞 (reactor.startService 等回包 +
# 3 次 _send 每次 10s, 总 5-50s), 切 cTrader broker 占满 FastAPI 线程池 40 线程
# 之一, 全部其它 API 排队. 改造: _get_ctrader() 非阻塞 — 首次启动后台线程做
# 真 connect, 立刻返 (bridge, None, warming_up=True); 后续调用查 is_connected
# 属性(瞬时), 连好了返 warming_up=False, 没好返 warming_up=True.
_ctrader_bridge = None  # type: "CTraderBridge | None"
_ctrader_lock = threading.Lock()
_ctrader_connect_thread: threading.Thread | None = None
_ctrader_last_error: str | None = None


def _kickoff_ctrader_connect():
    """在后台线程跑 _ctrader_bridge.connect(). 不会阻塞调用方.
    必须已持有 _ctrader_lock 锁. 假定 _ctrader_bridge 已实例化."""
    global _ctrader_last_error
    bridge = _ctrader_bridge

    def _bg():
        global _ctrader_last_error
        try:
            ok = bridge.connect()
            if not ok:
                _ctrader_last_error = "cTrader connect failed (check credentials / network)"
                logger.warning(f"[ctrader] background connect failed: {_ctrader_last_error}")
            else:
                _ctrader_last_error = None
                logger.info("[ctrader] background connect OK")
        except Exception as e:
            _ctrader_last_error = f"{type(e).__name__}: {e}"[:300]
            logger.warning(f"[ctrader] background connect exception: {_ctrader_last_error}")

    t = threading.Thread(target=_bg, daemon=True, name="ctrader-bg-connect")
    t.start()
    return t


def _get_ctrader():
    """返回进程级长连接 CTraderBridge (非阻塞版, audit 2026-06-10).

    Returns:
        (bridge, error_msg | None, warming_up: bool)
        warming_up=True 表示后台 connect 还没好 — 调用方应返 warming_up 缓存,
        不要阻塞等连接 (e.g. `{"ok": True, "warming_up": True}`).
        warming_up=False + bridge 不为 None → 可直接用.
        error_msg 不为 None → 启动失败 (无 token / 库未装), 重试也没用.
    """
    global _ctrader_bridge, _ctrader_connect_thread
    try:
        from execution._env import load_env
        load_env()
    except Exception as _e:
        logger.debug("load_env failed (non-critical): %s", _e)
    try:
        from execution.ctrader_bridge import CTraderBridge
    except ImportError as e:
        return None, f"ctrader-open-api not installed: {e}", False

    with _ctrader_lock:
        # 复用已有连接 — 用 is_connected 属性 (瞬时), 不用 ping() (阻塞 5s)
        if _ctrader_bridge is not None:
            if _ctrader_bridge.is_connected:
                return _ctrader_bridge, None, False
            # 旧实例断开且没在 reconnect — 后台起一次
            if _ctrader_connect_thread is None or not _ctrader_connect_thread.is_alive():
                _ctrader_connect_thread = _kickoff_ctrader_connect()
            return _ctrader_bridge, None, True  # warming up

        # 首次: 创建实例 + 后台启动 connect
        try:
            _ctrader_bridge = CTraderBridge(
                client_id=os.getenv("CTRADER_CLIENT_ID", ""),
                client_secret=os.getenv("CTRADER_CLIENT_SECRET", ""),
                access_token=os.getenv("CTRADER_ACCESS_TOKEN", ""),
                account_id=int(os.getenv("CTRADER_ACCOUNT_ID", "0")),
                send_orders=True,  # cTrader 是唯一执行通道, 外层 _should_send_orders 控制闸
            )
        except Exception as e:
            _ctrader_bridge = None
            return None, f"{type(e).__name__}: {e}"[:300], False

        if not _ctrader_bridge.has_token():
            _ctrader_bridge = None
            return None, "no cTrader credentials in .env (CTRADER_CLIENT_ID/SECRET/ACCESS_TOKEN/ACCOUNT_ID)", False

        # ★ 关键改动: 立刻返 warming_up, 后台线程做真 connect
        _ctrader_connect_thread = _kickoff_ctrader_connect()
        return _ctrader_bridge, None, True  # warming up


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
    return {
        "ctrader": {"status": ctrader_status, "error": ctrader_error},
        "loop": loop_status(),
        "readiness": get_live_readiness("ctrader"),
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
    positions_ready = positions_updated_at > 0 and len(positions) > 0
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
    elif not positions_ready:
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
        return _enrich_positions_with_path_metrics(pos_list, cfg=cfg, now_ts=time.time(), persist=False, broker=broker)

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
    """每日参数自动优化: 轻量网格扫描, 最优参数自动写入 RuntimeConfig。"""
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

    from config.runtime_config import patch as rc_patch
    rc_patch(best.params)
    logger.info(
        f"[param_tune] applied: rsi={best.params.get('strategy_rsi_period')} "
        f"sl={best.params.get('strategy_sl_atr')} tp={best.params.get('strategy_tp_atr')} "
        f"PnL={best.net_pnl:.1f} WR={best.win_rate:.0f}% Sharpe={best.sharpe:.2f}"
    )
    # 记录运行时间
    _PARAM_TUNE_STATE["last_run_ts"] = time.time()
    _save_param_tune_state()

    try:
        from monitor.evolution_story import EvolutionStory
        EvolutionStory.shared().append(
            event_type="param_tune_complete",
            payload={"best_params": best.params, "pnl": round(best.net_pnl, 2),
                  "sharpe": round(best.sharpe, 2), "n_combos": len(combos)}
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

        from config.runtime_config import patch as _rc_patch
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
                f_names = [n for n in fv_dict if n in cfg.factor_portfolio_weights]
                if len(f_names) >= 3:
                    factor_mat = _np.column_stack([
                        fv_dict[n][:len(fwd_ret)] for n in f_names
                    ])
                    awe.compute_blend_baseline(factor_mat, fwd_ret[:len(fwd_ret)], f_names)
                    use_blend = True
            except Exception as _e2:
                logger.debug("[awe_adapt] blend_baseline compute failed: %s", _e2)

        patches = awe.adapt(attr, cfg.factor_portfolio_weights,
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
                    factor_configs=cfg.factor_signal_config,
                    current_weights=cfg.factor_portfolio_weights,
                )
                merged = DecisionPolicy.to_weights(decisions)
                _rc_patch({"factor_portfolio_weights": merged})
                logger.info("[awe_adapt] weights pushed via DecisionPolicy ({} factors)", len(merged))
            except Exception as _e2:
                logger.warning("[awe_adapt] DecisionPolicy weight push failed: %s", _e2)
        else:
            logger.debug("[awe_adapt] no weight changes needed")
    except Exception as e:
        logger.warning("[awe_adapt] failed: {}", e)




# ═══════════════════════════════════════════════════════════
# Phase 2: ML 预测管道
# ═══════════════════════════════════════════════════════════


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


def _scheduled_ml_retrain():
    """每周日凌晨 5 点: ML 因子自动重训。

    1. 加载最近 30,000 bars
    2. 训练 XGBoost 方向预测器
    3. 若通过 OOS 验证 → 注册为因子
    4. 记录到 evolution_story
    """
    try:
        from alpha.ml.direction_predictor import train_direction_predictor
        result = train_direction_predictor(
            symbol="XAUUSD+", timeframe="M5", n_bars=30000,
        )
        if result:
            from monitor.evolution_story.report import EvolutionStory
            story = EvolutionStory.shared() if hasattr(EvolutionStory, "shared") else None
            if story:
                story.append(
                    event_type="ml_retrain",
                    payload=result,
                )
            logger.info("[ml_retrain] done: status={}, oos_acc={:.4f}",
                        result.get("status"), result.get("oos_acc", 0))
        else:
            logger.info("[ml_retrain] no model trained (insufficient data)")
    except Exception as e:
        logger.warning("[ml_retrain] failed: {}", e)


def _scheduled_ml_drift_check():
    """每 6 小时: ML 因子概念漂移检测。

    检查 xgb_dir 因子的滚动准确率,
    若连续低于基线 95% → 触发自动重训。
    """
    try:
        from alpha.ml.drift_detector import get_detector
        from alpha.ml.direction_predictor import FACTOR_NAME

        detector = get_detector(FACTOR_NAME)
        report = detector.check(FACTOR_NAME)
        if report.drift_detected:
            logger.warning(
                "[ml_drift] %s drift detected: acc=%.4f score=%.2f needs_retrain=%s",
                FACTOR_NAME, report.rolling_accuracy,
                report.drift_score, report.needs_retrain,
            )
            if report.needs_retrain:
                logger.info("[ml_drift] triggering auto-retrain for %s", FACTOR_NAME)
                _scheduled_ml_retrain()
                detector.reset()
        else:
            logger.debug("[ml_drift] %s OK: acc=%.4f n=%d",
                         FACTOR_NAME, report.rolling_accuracy,
                         report.n_observations)
    except Exception as e:
        logger.warning("[ml_drift] failed: %s", e)



def _start_live_scheduler():
    """注册并启动自进化 Scheduler (11 job). 幂等: 已运行时跳过."""
    from backend.runtime.scheduler import InProcessScheduler
    from backend.runtime.evolution_kernel import EvolutionKernel
    sched = InProcessScheduler()
    if getattr(sched, "_started", False):
        return

    # ★ 初始化 EvolutionKernel (注册中枢 + quality gate + governor)
    kernel = EvolutionKernel.shared()
    kernel.set_pipeline(_factor_pipeline)
    kernel.start()  # registers evolution_hourly + awe_adapt + system_health

    # ── 数据同步 (每 5 分钟) ──
    from data.live_sync.health import SyncHealth
    def _data_sync():
        """先检查 bars + ticks 新鲜度, 有缺口才回补, 不缺就跳过。"""
        t0 = time.time()
        health = SyncHealth.shared()
        try:
            from config.runtime_config import shared as _rcc
            cfg = _rcc()
            symbols = list(cfg.enabled_symbols) if hasattr(cfg, 'enabled_symbols') else ["XAUUSD+"]
            now = time.time()
            import duckdb as _duckdb
            _db_path = str(Path(__file__).resolve().parent.parent.parent / "data" / "ctrader_data.duckdb")
            _dc = _duckdb.connect(_db_path)

            # 1. 检查 bar 新鲜度: 各周期最新 bar 时间 vs 预期阈值
            bar_thresholds = {"M1": 600, "M5": 900, "M15": 1800, "M30": 3600, "H1": 7200, "D1": 172800}
            stale_tfs = []
            fresh_tfs = []
            observed_bar_ts_by_tf: dict[str, float] = {}
            for tf, max_age in bar_thresholds.items():
                try:
                    row = _dc.execute(
                        "SELECT MAX(time) FROM bars WHERE symbol=? AND timeframe=?",
                        [symbols[0], tf],
                    ).fetchone()
                    row_ts = float(row[0]) if row and row[0] else 0.0
                    if row_ts > 0:
                        observed_bar_ts_by_tf[tf] = row_ts
                    if row_ts > 0 and (now - row_ts) < max_age:
                        fresh_tfs.append(tf)
                    else:
                        stale_tfs.append(tf)
                except Exception:
                    stale_tfs.append(tf)

            # 2. 检查 tick 新鲜度
            tick_gap = 600.0  # 10 分钟阈值
            try:
                tick_row = _dc.execute(
                    "SELECT MAX(time) FROM ticks WHERE symbol=?", [symbols[0]],
                ).fetchone()
                tick_latest = float(tick_row[0]) if tick_row and tick_row[0] else 0
                tick_stale = tick_latest == 0 or (now - tick_latest) > tick_gap
                tick_age = (now - tick_latest) if tick_latest > 0 else float("inf")
            except Exception:
                tick_stale = True
                tick_age = float("inf")
            _dc.close()

            # 3. 日志: 数据健康摘要
            bar_status = f"{len(fresh_tfs)}/{len(bar_thresholds)} fresh"
            tick_status = f"{'' if tick_stale else '✓'}tick age={tick_age/60:.0f}m"
            if stale_tfs:
                logger.info("[data_sync] stale: bars={} tick_age={:.0f}m → pulling", stale_tfs, tick_age/60)
            else:
                # 一切新鲜: 跳过数据拉取, 只记录健康状态
                if not tick_stale:
                    logger.debug("[data_sync] all fresh ({}), skip pull", bar_status)
                    health.record_success(last_bar_ts_by_tf=observed_bar_ts_by_tf or None)
                    return
                logger.info("[data_sync] bars ok, ticks stale (age={:.0f}m) → pulling", tick_age/60)

            # 4. 回补 bars (用主 bridge 直接拉, 不再开第二连接)
            total_bars = 0
            sync_tfs = stale_tfs if stale_tfs else list(bar_thresholds.keys())
            if sync_tfs:
                bridge, err, warming = _get_ctrader()
                if err:
                    logger.warning("[data_sync] cTrader bridge unavailable: {}, skip bar pull", err)
                elif warming:
                    logger.info("[data_sync] cTrader bridge still warming up, skip bar pull")
                elif not bridge.is_connected:
                    logger.info("[data_sync] cTrader bridge not connected, skip bar pull")
                else:
                    for sym in symbols:
                        for tf in sync_tfs:
                            try:
                                df = bridge.fetch_bars(tf, n_bars=200)
                                if df is None or df.empty:
                                    continue
                                bars = []
                                for idx, row in df.iterrows():
                                    ts = int(idx.timestamp())
                                    bars.append({
                                        "time": ts,
                                        "open": float(row["open"]),
                                        "high": float(row["high"]),
                                        "low": float(row["low"]),
                                        "close": float(row["close"]),
                                        "volume": int(row["volume"]),
                                        "spread": 0,
                                    })
                                from data.store import DataStore
                                DataStore().insert_bars(bars, sym, tf)
                                total_bars += len(bars)
                                if bars:
                                    observed_bar_ts_by_tf[tf] = float(bars[-1]["time"])
                                logger.info("[data_sync] pulled {} {} bars: {} bars", sym, tf, len(bars))
                            except Exception as e:
                                logger.warning("[data_sync] {} {} pull failed: {}", sym, tf, e)

            # 5. 记录健康状态
            elapsed = time.time() - t0
            health.record_success(last_bar_ts_by_tf=observed_bar_ts_by_tf or None)
            if total_bars > 0 or tick_stale:
                logger.info("[data_sync] done ({:.1f}s): +{} bars, tick_gap={:.0f}m", elapsed, total_bars, tick_age/60)
        except Exception as e:
            logger.warning("[data_sync] failed: {}", e)
            try:
                health.record_failure(str(e)[:200])
            except Exception as _e2:
                logger.debug("[data_sync] health.record_failure failed: %s", _e2)
    sched.add_job("data_sync", "*/5 * * * *", _data_sync)
    # 每小时: Dukascopy tick 增量 (替换 Hermes cron, 项目全自主)
    def _scheduled_dukascopy_tick():
        """运行 Dukascopy tick 增量拉取脚本."""
        try:
            import subprocess, sys
            script = Path(__file__).resolve().parent.parent.parent / "scripts" / "debug" / "_pull_dukascopy_incremental.py"
            if not script.exists():
                logger.warning("[dukascopy_tick] script not found: {}")
                return
            result = subprocess.run(
                [sys.executable or "python", str(script)],
                capture_output=True, text=True, timeout=180,
            )
            out = (result.stdout or "").strip()
            if result.returncode == 0:
                logger.info("[dukascopy_tick] {}", out.split(chr(10))[-1] if out else "done")
            else:
                logger.warning("[dukascopy_tick] failed (rc={}): {}", result.returncode, (result.stderr or "")[:200])
        except Exception as e:
            logger.warning("[dukascopy_tick] error: {}", e)
    sched.add_job("dukascopy_tick", "0 * * * *", _scheduled_dukascopy_tick)
    # 启动后立即补跑一次 tick 增量 (后台线程, 不阻塞启动)
    threading.Thread(
        target=_scheduled_dukascopy_tick,
        daemon=True,
        name="dukascopy-tick-catchup",
    ).start()
    # 每天 8:00: 经济日历拉取
    def _scheduled_events_sync():
        import sys
        try:
            import subprocess
            script = Path(__file__).resolve().parent.parent.parent / "scripts" / "fetch_events_calendar.py"
            if not script.exists():
                logger.warning("[events_sync] script not found")
                return
            result = subprocess.run(
                [sys.executable or "python", str(script), "--weeks", "2"],
                capture_output=True, text=True, timeout=60,
            )
            out = (result.stdout or "").strip()
            if result.returncode == 0:
                logger.info("[events_sync] ok")
            else:
                logger.warning("[events_sync] failed (rc={}): {}", result.returncode, (result.stderr or "")[:200])
        except Exception as e:
            logger.warning("[events_sync] error: {}", e)
    sched.add_job("events_sync", "0 8 * * *", _scheduled_events_sync)
    # 启动后立即补跑一次 (后台线程)
    threading.Thread(
        target=_scheduled_events_sync,
        daemon=True,
        name="events-sync-catchup",
    ).start()
    # 每周六 6:00: COT 持仓刷新 (CFTC 周五发布)
    def _scheduled_cot_sync():
        import sys
        try:
            import subprocess
            script = Path(__file__).resolve().parent.parent.parent / "scripts" / "refresh_external_data.py"
            result = subprocess.run(
                [sys.executable or "python", str(script), "--source", "cot", "--force"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                logger.info("[cot_sync] ok")
            else:
                logger.warning("[cot_sync] failed (rc={}): {}", result.returncode, (result.stderr or "")[:200])
        except Exception as e:
            logger.warning("[cot_sync] error: {}", e)
    sched.add_job("cot_sync", "0 6 * * 6", _scheduled_cot_sync)
    # 每季度首日 4:00: ETF 持仓刷新 (SEC 10-Q filing)
    def _scheduled_etf_sync():
        import sys
        try:
            import subprocess
            script = Path(__file__).resolve().parent.parent.parent / "scripts" / "refresh_external_data.py"
            result = subprocess.run(
                [sys.executable or "python", str(script), "--source", "etf", "--force"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode == 0:
                logger.info("[etf_sync] ok")
            else:
                logger.warning("[etf_sync] failed (rc={}): {}", result.returncode, (result.stderr or "")[:200])
        except Exception as e:
            logger.warning("[etf_sync] error: {}", e)
    sched.add_job("etf_sync", "0 4 1 */3 *", _scheduled_etf_sync)
    # ★ awe_adapt / evolution_hourly / system_health 已由 EvolutionKernel 注册
    # Phase 2: ML 因子自动重训 (每周日凌晨 5:00)
    sched.add_job("ml_retrain", "0 5 * * 0", _scheduled_ml_retrain)
    # Phase 3: 特征工程 (每天凌晨 3:00)
    sched.add_job("feature_eng", "0 3 * * *", _scheduled_feature_engineering)
    # Phase 2: ML 因子漂移检测 (每 6 小时)
    sched.add_job("ml_drift_check", "0 */6 * * *", _scheduled_ml_drift_check)
    # ★ system_health 已由 EvolutionKernel 注册
    sched.start()
    logger.info("[live] InProcessScheduler started with 10 jobs")

    # ── 后台: 首次启动数据补充 (用主 bridge, 不开第二连接) ──
    def _initial_ctrader_data_pull():
        """启动后立即从 cTrader 拉最近数据写入 DB.
        不清除现有 MT5 历史数据 (保留 203K 宝贵 bars).
        DuckDB UNIQUE constraint 自动处理同时间戳的 bar."""
        try:
            bridge, err, warming = _get_ctrader()
            if err:
                logger.warning("[init] cTrader bridge unavailable: {}, skip initial pull", err)
                return
            if warming:
                # bridge 还在后台连接中, 等最多 30s
                t0 = time.time()
                while time.time() - t0 < 30:
                    if bridge.is_connected:
                        break
                    time.sleep(1)
                if not bridge.is_connected:
                    logger.warning("[init] cTrader bridge not connected after 30s, skip initial pull")
                    return
            # 拉最近 5000 根 M5 bars (cTrader 有 ~90天数据)
            from data.store import DataStore
            store = DataStore()
            for tf in ["M5", "M15", "H1"]:
                try:
                    df = bridge.fetch_bars(tf, n_bars=5000)
                    if df is None or df.empty:
                        logger.warning("[init] {} pull returned empty", tf)
                        continue
                    bars = []
                    for idx, row in df.iterrows():
                        ts = int(idx.timestamp())
                        bars.append({
                            "time": ts, "open": float(row["open"]),
                            "high": float(row["high"]), "low": float(row["low"]),
                            "close": float(row["close"]), "volume": int(row["volume"]),
                            "spread": 0,
                        })
                    store.insert_bars(bars, "XAUUSD+", tf)
                    logger.info("[init] {}: +{} bars ({} → {})", tf, len(bars),
                               time.strftime('%m-%d %H:%M', time.gmtime(bars[0]["time"])),
                               time.strftime('%m-%d %H:%M', time.gmtime(bars[-1]["time"])))
                except Exception as e:
                    logger.warning("[init] {} pull failed: {}", tf, e)
            logger.info("[init] ✅ cTrader 初始数据补充完成")
        except Exception as _e:
            logger.warning("[init] initial pull failed: {}", _e)

    import threading as _th
    _th.Thread(target=_initial_ctrader_data_pull, daemon=True).start()
    # 开机补跑: 先拉数据, 再跑其他检查 (后台线程, 不阻塞 start_loop HTTP 响应)
    def _catch_up_all_jobs():
        import traceback as _tb
        # 使用 sched.run_job_now() 确保执行计入 run_count
        # 冷启动补跑: 确保重启后数据立即刷新, 不错过低频任务的调度窗口
        job_names = [
            "data_sync",           # 每 5 分钟 — bars/ticks 补缺
            "dukascopy_tick",      # 每小时 — Dukascopy tick 历史
            "events_sync",         # 每日 08:00 — 经济事件日历
            "cot_sync",            # 每周六 — COT 持仓报告
            "etf_sync",            # 每季度 — ETF 持仓
            "evolution_hourly",    # 每小时 — 进化闭环
            "awe_adapt",           # 每 30 分钟 — 权重自适应
            "ml_retrain",           # 每周日 — ML 重训
            "feature_eng",         # 每日 03:00 — 特征工程
            "ml_drift_check",      # 每 6 小时 — 概念漂移检测
        ]
        for name in job_names:
            try:
                logger.info("[catch-up] running {} ...", name)
                sched.run_job_now(name)
                logger.info("[catch-up] {} done", name)
            except Exception as e:
                logger.warning("[catch-up] {} failed: {}\n{}", name, e, _tb.format_exc()[-200:])
    threading.Thread(
        target=_catch_up_all_jobs,
        name="scheduler_catch_up",
        daemon=True,
    ).start()


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
    # 显式停止 → 立即返回 stopped, 不等后台清理线程
    if _live_state_get("loop_running") is False:
        return {
            "running": False, "pid": None, "broker": _live_state_get("broker") or _loop_broker,
            "started_at": _live_state_get("loop_started_at"),
            "strategy_name": _live_state_get("loop_strategy") or _loop_strategy_name,
        }
    # 优先共享缓存 (audit 2026-06-08)
    if _live_state_get("loop_running") and _live_state_get("broker"):
        return {
            "running": True,
            "pid": None,
            "broker": _live_state_get("broker"),
            "started_at": _live_state_get("loop_started_at"),
            "strategy_name": _live_state_get("loop_strategy"),
        }
    with _loop_state_lock:
        if _loop_thread is not None and _loop_thread.is_alive():
            return {
                "running": True,
                "pid": _loop_thread.ident,
                "broker": _loop_broker,
                "started_at": _loop_started_at,
                "strategy_name": _loop_strategy_name,
            }
        return {
            "running": False, "pid": None, "broker": None,
            "started_at": None, "strategy_name": _loop_strategy_name,
        }


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
    from backend.core.db import DUCKDB_BARS
    db_path = str(DUCKDB_BARS)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            import duckdb
            conn = duckdb.connect(db_path)
            try:
                df = conn.execute(
                    "SELECT time, open, high, low, close, volume "
                    "FROM bars WHERE symbol=? AND timeframe=? "
                    "ORDER BY time DESC LIMIT ?",
                    [symbol, timeframe, n_bars]
                ).df()
            finally:
                conn.close()
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
    log(f"warmed up: {len(df)} bars (source={df_source}), last close={df['close'].iloc[-1]:.2f}")
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
        gate = ExecutionGate({
            "signal_threshold": _rcfg.factor_signal_threshold,
            "cooldown_bars": _rcfg.strategy_cooldown_bars,
        })
        from alpha.attribution_engine import AttributionEngine
        from alpha.adaptive_weight_engine import AdaptiveWeightEngine
        from alpha.ic_tracker import ICTracker
        attr = AttributionEngine()
        ictracker = ICTracker(window=5000)
        awe = AdaptiveWeightEngine({
            "awe_sensitivity": _rcfg.awe_sensitivity,
            "awe_anchor_pull": _rcfg.awe_anchor_pull,
            "awe_max_single_change": _rcfg.awe_max_single_change,
            "awe_weight_min": _rcfg.awe_weight_min,
            "awe_weight_max": _rcfg.awe_weight_max,
            "awe_min_trades": _rcfg.awe_min_trades,
            "awe_ic_floor": _rcfg.awe_ic_floor,
            "awe_health_floor": _rcfg.awe_health_floor,
            "awe_disable_min_trades": _rcfg.awe_disable_min_trades,
            "awe_max_type_weight_pct": _rcfg.awe_max_type_weight_pct,
        }, ictracker=ictracker)
        awe.initialize(_rcfg.factor_portfolio_weights, ictracker=ictracker)
        _factor_pipeline = {
            "engine": engine, "normalizer": normalizer,
            "compositor": compositor, "gate": gate,
            "attribution": attr, "awe": awe, "ic_tracker": ictracker,
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
                    pipelines = [_factor_pipeline] + list((_factor_pipelines or {}).values())
                    seen = set()
                    for pipe in pipelines:
                        if not pipe or id(pipe) in seen:
                            continue
                        seen.add(id(pipe))
                        pipe_engine = pipe.get("engine")
                        pipe_normalizer = pipe.get("normalizer")
                        pipe_compositor = pipe.get("compositor")
                        if pipe_engine and hasattr(pipe_engine, "set_factor_runtime_config"):
                            pipe_engine.set_factor_runtime_config(cfg.factor_signal_config)
                        if pipe_normalizer and hasattr(pipe_normalizer, "update_configs"):
                            pipe_normalizer.update_configs(cfg.factor_signal_config)
                        if pipe_compositor and hasattr(pipe_compositor, "reload_configs"):
                            pipe_compositor.reload_configs(merged_cfg)
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
            symbols = list(cfg2.enabled_symbols) if hasattr(cfg2, 'enabled_symbols') else ["XAUUSD+"]
            for sym in symbols:
                if sym == "XAUUSD+":
                    # 已有主管道
                    _factor_pipelines[sym] = _factor_pipeline
                    continue
                # 为额外品种创建独立管道 (共用归因/AWE/IC tracker)
                _sym_engine = StreamingFactorEngine(max_buffer=200, factor_runtime_config=cfg2.factor_signal_config)
                _sym_normalizer = SignalNormalizer(cfg2.factor_signal_config)
                _sym_compositor = PortfolioCompositor(
                    _merge_portfolio_configs(
                        cfg2.factor_signal_config,
                        cfg2.factor_portfolio_weights,
                        cfg2.factor_tactical_alpha,
                        cfg2.factor_signal_threshold,
                    )
                )
                _sym_gate = ExecutionGate({
                    "signal_threshold": cfg2.factor_signal_threshold,
                    "cooldown_bars": cfg2.strategy_cooldown_bars,
                })
                _factor_pipelines[sym] = {
                    "engine": _sym_engine, "normalizer": _sym_normalizer,
                    "compositor": _sym_compositor, "gate": _sym_gate,
                    "attribution": attr, "awe": awe, "ic_tracker": ictracker,
                }
            if len(symbols) > 1:
                log(f"Multi-symbol pipelines initialized: {symbols}")
        except Exception as e:
            log(f"Multi-symbol pipeline init skipped: {e}")
            _factor_pipelines = {"XAUUSD+": _factor_pipeline} if _factor_pipeline else {}
        # Phase 6: 初始化跨品种协方差
        global _cross_asset_covar
        try:
            from risk.cross_asset import CrossAssetCovariance
            symbols = list(_rcfg.enabled_symbols) if hasattr(_rcfg, 'enabled_symbols') else ["XAUUSD+"]
            if len(symbols) > 1 and _rcfg.cross_asset_covariance_enabled:
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
            for i in range(len(df)):
                bar = {
                    "open": float(df["open"].iloc[i]),
                    "high": float(df["high"].iloc[i]),
                    "low": float(df["low"].iloc[i]),
                    "close": float(df["close"].iloc[i]),
                    "volume": float(df["volume"].iloc[i]) if "volume" in df.columns else 0.0,
                    "time": float(df.index[i].timestamp()) if hasattr(df.index[i], "timestamp") else 0.0,
                    "timeframe": TF,
                    "complete": True,
                }
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
                    votes = {}
                    for name, sig in signals.items():
                        raw_val = last_fv.get(name)
                        s_val = sig if isinstance(sig, (int, float)) else 0.0
                        r_val = raw_val if isinstance(raw_val, (int, float)) else None
                        votes[name] = {
                            "signal": round(s_val, 4),
                            "raw": round(r_val, 4) if r_val is not None else None,
                            "direction": 1 if s_val > 0 else -1 if s_val < 0 else 0,
                        }
                    _set_factor_snapshot(
                        votes,
                        {
                            "direction": composite.direction,
                            "score": round(composite.score, 4),
                            "tactical_score": round(composite.tactical_score, 4),
                            "macro_score": round(composite.macro_score, 4),
                            "n_active": composite.n_active_factors,
                            "n_abstain": composite.n_abstain_factors,
                            "gate_passed": gate_result.passed,
                            "gate_reason": gate_result.reason,
                            "ts": time.time(),
                        },
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

    def _ensure_ctrader_market_subscriptions(wait_timeout_sec: float = 0.0) -> bool:
        try:
            spot_bridge, spot_err, spot_warming = _get_ctrader()
            require_l2_depth = bool(getattr(_rcfg, "risk_require_l2_depth", False))
            if spot_err:
                log(f"subscribe_spots skipped: {spot_err}")
                return False
            if spot_bridge is None:
                return False
            if spot_warming or not spot_bridge.is_connected:
                if wait_timeout_sec <= 0:
                    return False
                wait_err = _wait_ctrader_ready(spot_bridge, timeout_sec=wait_timeout_sec)
                if wait_err:
                    log(f"subscribe_spots skipped: {wait_err}")
                    return False
            if not getattr(spot_bridge, "_spot_subscribed", False):
                if not spot_bridge.subscribe_spots():
                    log("subscribe_spots failed (non-fatal): subscribe request rejected")
                    return False
                if require_l2_depth:
                    if not getattr(spot_bridge, "_depth_subscribed", False):
                        spot_bridge.subscribe_depth()
                    log("subscribed to spot and depth events for real-time price")
                else:
                    log("subscribed to spot events for real-time price (depth disabled)")
                return True
            if require_l2_depth and not getattr(spot_bridge, "_depth_subscribed", False):
                if spot_bridge.subscribe_depth():
                    log("subscribed depth events for real-time price")
            return True
        except Exception as e:
            log(f"subscribe_spots failed (non-fatal): {e}")
            return False

    # 订阅 cTrader 实时报价 (audit 2026-06-08)
    # audit 2026-06-10: warmup 走 local_db 路径时 bridge 变量未定义,
    # 之前直接调 bridge.subscribe_spots() 抛 NameError 被 except 吞,
    # log 误报 "failed (non-fatal)". 修: 从 _get_ctrader() 拿真 bridge, 短等 ready.
    if broker == "ctrader":
        _ensure_ctrader_market_subscriptions(wait_timeout_sec=20.0)

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
        except Exception as _e2:
            log(f"tick {tick}: session reset failed (non-fatal): {_e2}")

        # ── 主循环体: 账户刷新 + 数据读取 + 交易 ──
        try:
            bridge, err, warming = _get_ctrader()
            if err:
                log(f"tick {tick}: {err}; reconnect next tick")
                stop_flag.wait(60)
                continue
            # bridge 不可用时仍跑因子管道（用本地 DB），只跳过发单
            bridge_ready = bridge is not None and not warming and bridge.is_connected
            _set_loop_diagnostic(
                tick,
                "ready" if bridge_ready else ("warming" if warming else "disconnected"),
                bridge_ready=bridge_ready,
            )
            if not bridge_ready:
                log(f"tick {tick}: cTrader warming/disconnected, running pipeline dry")

            # 刷新账户缓存 (bridge 可用时才做)
            if bridge_ready:
                _ensure_ctrader_market_subscriptions(wait_timeout_sec=0.0)
                kickoff_account_refresh(bridge, broker, interval_sec=3.0)
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

            # 从本地 DataStore 读最新 bars (cTraderPuller 定时写入)
            df_new = _warmup_from_local_db("XAUUSD+", TF, 5)
            if df_new is None or len(df_new) == 0:
                log(f"tick {tick}: local DB has no bars (waiting for CTraderPuller)")
            else:
                # v9: 用 cTrader spot 覆盖最新 close, 但验证合理性 (spot 在 bar close ±20% 内)
                spot = bridge.get_spot_price() if hasattr(bridge, "get_spot_price") else 0
                last_close = float(df_new.iloc[-1]["close"])
                if spot and spot > 0 and last_close > 0 and abs(spot - last_close) / last_close < 0.20:
                    df_new.loc[df_new.index[-1], "close"] = spot
                    df_new.loc[df_new.index[-1], "high"] = max(df_new.iloc[-1]["high"], spot)
                    df_new.loc[df_new.index[-1], "low"] = min(df_new.iloc[-1]["low"], spot)
                elif spot and spot > 0:
                    log(f"tick {tick}: spot={spot:.2f} too far from bar close={last_close:.2f}, using DataStore price")

                # 熔断检查 + 策略运算
                cb_tripped = _live_state_get("circuit_breaker", False)
                if cb_tripped:
                    log(f"tick {tick}: circuit breaker tripped, skip trading")
                else:
                    dd_state = _evaluate_daily_drawdown()
                    if dd_state["tripped"]:
                        log(f"tick {tick}: CIRCUIT BREAKER: daily drawdown {dd_state['dd_pct']:.1f}%")
                    else:
                        last_bar = df_new.iloc[-1]
                        _process_tick(bridge, None, df_new, last_bar, broker, tick, log)
                        # ★ 交易执行后立即检查停止信号，避免下个 tick 才响应
                        if stop_flag.is_set():
                            log(f"tick {tick}: stop requested during processing, exiting")
                            break
        except Exception as e:
            log(f"tick {tick} error: {type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")

        # ── 风险模块自动计算 (每 tick, 不阻塞主循环) ──
        try:
            # 1. 追加当前 equity 到历史序列
            acct = _live_state_get("account", {}, clone=True) or {}
            equity = float(acct.get("equity") or 0.0)
            eq_hist = _live_state_get("trade_equity_history", [], clone=True) or []
            if equity > 0:
                eq_hist = _append_trade_equity(equity)

            # 2. VaR — 用 VaRCalculator(confidence=0.95)
            from backend.risk.var import VaRCalculator as _VaRCalc
            _var_calc = _VaRCalc(confidence=0.95)
            if len(eq_hist) >= 10:
                _set_risk_metric("var", _var_calc.calculate(eq_hist))
            else:
                _set_risk_metric("var", _var_calc.get_status(eq_hist))

            # 3. Kelly — 从 session_winning/session_losing 计算
            from backend.risk.kelly import KellyCriterion as _KellyCalc
            _kelly_calc = _KellyCalc()
            sw = int(_live_state_get("session_winning", 0))
            sl = int(_live_state_get("session_losing", 0))
            total = sw + sl
            if total > 0:
                win_rate = sw / total
                # 没有单笔盈亏明细, 用 session_pnl 估算平均盈亏
                session_pnl = float(_live_state_get("session_pnl", 0.0))
                if sw > 0 and sl > 0 and session_pnl != 0:
                    # 简单估算: 假设盈亏各半, avg_win = avg_loss 的粗略分割
                    avg_win = (session_pnl / total) * (1 + win_rate)
                    avg_loss = abs((session_pnl / total) * (1 - win_rate)) if win_rate < 1 else 0.01
                    avg_loss = max(avg_loss, 0.01)  # 避免除零
                else:
                    avg_win = 0.0
                    avg_loss = 0.01
                _set_risk_metric("kelly", _kelly_calc.calculate(win_rate, avg_win, avg_loss))
            else:
                _set_risk_metric("kelly", _kelly_calc.get_status())

            # 4. Stress — 用 trade_equity_history 跑
            from backend.risk.stress_test import StressTest as _StressTest
            _stress = _StressTest()
            if len(eq_hist) >= 10:
                _set_risk_metric("stress", _stress.run(eq_hist))
            else:
                _set_risk_metric("stress", _stress.get_status())

            # 5. Concentration — 空参数 (no factor weights yet)
            from backend.risk.concentration import ConcentrationChecker as _ConcCheck
            _conc = _ConcCheck()
            _set_risk_metric("concentration", _conc.check())

        except Exception as risk_e:
            log(f"tick {tick}: risk calculation error (non-fatal): {risk_e}")

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
    all_names = set(signal_config) | set(weight_config)
    for name in all_names:
        sc = signal_config.get(name, {})
        wc = weight_config.get(name, 1.0)
        weight = wc if isinstance(wc, (int, float)) else wc.get("weight", 1.0)
        merged[name] = {
            "weight": weight,
            "tags": sc.get("tags", []),
            "mode": sc.get("mode", "rank_mapping"),
            "enabled": sc.get("enabled", True),
            "source": sc.get("source", "builtin"),
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
    # 连接预检: 断开时不调用, 避免 10s timeout 堆积
    if hasattr(bridge, 'is_connected') and not bridge.is_connected:
        return
    try:
        raw = bridge.account_info()
    except Exception as e:
        logger.warning(f"[{broker}] background account_info failed: {e}")
        return
    if not raw:
        return
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
    _live_state_update(account=acct, account_updated_at=time.time())
    try:
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
            persist=True,
            broker=broker,
            strategy_name=str(_loop_strategy_name or "factor_v4"),
        )
        _live_state_update(positions=enriched, positions_updated_at=time.time())


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
# 审计日志 (统一使用 DecisionLogStore → state.db)
# ═══════════════════════════════════════════════════════════
import json as _json


def _should_send_orders(broker: str) -> bool:
    """True = 真发单; False = dry-run (记 log, 不下单)."""
    if broker == "ctrader":
        from config.runtime_config import shared as cfg
        return cfg().ctrader_send_orders
    return False


# 模块级,供 _read_state_snapshot 读
_latest_price: float | None = None


def get_latest_price() -> float | None:
    """返回最新价. 优先共享缓存 (live loop 写), 其次 bridge spot, 最后 bar close."""
    spot = _live_state_get("spot_price")
    if spot and spot > 0:
        return spot
    global _latest_price
    try:
        # audit 2026-06-10: 3-tuple; warming_up 时返旧价不阻塞
        bridge, err, warming = _get_ctrader()
        if bridge is None or err or warming or not bridge.is_connected:
            return _latest_price
        spot = bridge.get_spot_price()
        if spot is not None and spot > 0:
            return spot
    except Exception as _e2:
        logger.debug("[live] get_latest_price spot query failed: %s", _e2)
    return _latest_price


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
            # symbol 路径: 走 get_positions + filter by symbol_id + close 一个个.
            positions = bridge.get_positions()
            if symbol:
                # symbol 这里可能是 symbol 名 (XAUUSD) 或 id (int), 简单按 name 匹配 fallback
                target_positions = [p for p in positions if str(p.get("symbol_id")) == symbol or p.get("symbol") == symbol]
            else:
                target_positions = positions
            closed = 0
            for p in target_positions:
                # 优先用 position_id; 旧 dict 形式也兼容
                pid = p.get("position_id") or p.get("ticket")
                if pid is None:
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
                    continue
                _remember_close_reason(int(pid), "emergency_close")
                _remember_close_verdict(int(pid), close_verdict)
                result = bridge.close_position(pid)
                if getattr(result, "success", False):
                    closed += 1
            return {"ok": True, "broker": "ctrader", "symbol": symbol or "ALL", "closed": closed}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-300:]}
    else:
        return {"ok": False, "error": f"unknown broker: {broker}"}


# ═══════════════════════════════════════════════════════════
# Factor Takeover v4: 因子管道 _process_tick
# ═══════════════════════════════════════════════════════════
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
) -> str:
    """Persist open context after a market fill, even if SL/TP amend fails."""
    entry_decision_id = ""
    try:
        from alpha.attribution_engine import TradeAttribution

        total_signal_abs = sum(
            abs(s) for s in composite.factor_signals.values()
            if s is not None
        )
        trade_attr = TradeAttribution(
            position_id=pid,
            open_ts=time.time(),
            open_price=current_price,
            direction=composite.direction,
            factor_signals=dict(composite.factor_signals),
            factor_values=dict(composite.factor_values),
            active_weights=dict(composite.active_weights),
            composite_score=composite.score,
            tactical_score=composite.tactical_score,
            macro_score=composite.macro_score,
            tags_breakdown=dict(composite.tags_breakdown),
            total_signal_abs=total_signal_abs,
            api_volume=float(actual_api_volume),
        )
        if attr_engine is not None:
            attr_engine.record_open(pid, trade_attr)
        _pos_open_prices[pid] = current_price
        _pos_open_api_volume[pid] = float(actual_api_volume)
    except Exception as attr_err:
        logger.debug("[live] attribution open persist failed for pos %s: %s", pid, attr_err)

    if _LEDGER:
        try:
            entry_decision_id = _LEDGER.log_composite_decision(
                event_type="open",
                composite=composite,
                gate_result=gate_result,
                symbol="XAUUSD+",
                timeframe=str(getattr(cfg, "timeframe", "") or ""),
                decision_ts=time.time(),
                trade_id=str(pid),
                position_id=str(pid),
                portfolio_state={
                    "balance": acct.get("balance", 0),
                    "equity": acct.get("equity", 0),
                    "n_positions": len(pos),
                    "session_pnl": _live_state_get("session_pnl", 0),
                },
                risk_state=(
                    _risk_state_with_verdict(risk_verdict)
                    if risk_verdict is not None
                    else (_live_state_get("risk", {}, clone=True) or {})
                ),
                action_reason="executed",
                action_json={
                    "position_id": pid,
                    "volume": actual_api_volume,
                    "requested_volume": requested_volume,
                    "price": round(current_price, 2),
                    "fill_price": round(fill_price, 2),
                    "sl": round(sl_price, 2),
                    "tp": round(tp_price, 2),
                    "tick": tick,
                    **(
                        {"risk_verdict": risk_verdict.to_dict()}
                        if risk_verdict is not None
                        else {}
                    ),
                },
            )
            _pos_entry_decisions[int(pid)] = entry_decision_id
            _LEDGER.log_order_event(
                event_type="submitted",
                decision_id=entry_decision_id,
                trade_id=str(pid),
                order_id=str(pid),
                broker_order_id=str(pid),
                price=float(current_price),
                volume=float(actual_api_volume),
                status="submitted",
                details={"tick": tick, "direction": composite.direction},
            )
            _LEDGER.log_order_event(
                event_type="filled",
                decision_id=entry_decision_id,
                trade_id=str(pid),
                order_id=str(pid),
                broker_order_id=str(pid),
                price=float(fill_price),
                volume=float(actual_api_volume),
                status="filled",
                details={"tick": tick, "direction": composite.direction},
            )
            _LEDGER.log_position_event(
                position_id=str(pid),
                trade_id=str(pid),
                symbol="XAUUSD+",
                event_type="opened",
                net_volume=float(actual_api_volume),
                avg_price=float(fill_price),
                details={
                    "tick": tick,
                    "direction": composite.direction,
                    "sl": round(sl_price, 2),
                    "tp": round(tp_price, 2),
                },
                event_ts=time.time(),
            )
        except Exception as ledger_err:
            logger.debug("[live] ledger open persist failed for pos %s: %s", pid, ledger_err)

    try:
        _upsert_recovery_position_state(
            {
                "position_id": pid,
                "symbol": "XAUUSD+",
                "direction": composite.direction,
                "open_price": float(fill_price or current_price),
                "volume": float(actual_api_volume),
                "entry_decision_id": entry_decision_id or _lookup_entry_decision_id(int(pid)),
            },
            broker=broker,
            strategy_name=str(_loop_strategy_name or "factor_v4"),
            status="open",
            context_integrity=_RECOVERY_CONTEXT_FULL,
            meta={
                "tick": tick,
                "sl": round(sl_price, 2),
                "tp": round(tp_price, 2),
            },
        )
    except Exception as recovery_err:
        logger.debug("[live] recovery open persist failed for pos %s: %s", pid, recovery_err)

    return entry_decision_id


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
    bar = {
        "open": float(last_bar["open"]),
        "high": float(last_bar["high"]),
        "low": float(last_bar["low"]),
        "close": float(last_bar["close"]),
        "volume": float(last_bar["volume"]) if "volume" in last_bar.index else 0.0,
        "time": float(df_new.index[-1].timestamp()) if hasattr(df_new.index[-1], "timestamp") else 0.0,
        "timeframe": _tf,
        "complete": True,
    }

    # 2. 流式因子计算 → 归一化 → 组合 → 闸门
    engine.refresh_factor_list()
    factor_values = engine.append_bar(bar)
    if not factor_values or not engine.is_warm:
        log(f"tick {tick}: factor engine not ready (is_warm={engine.is_warm})")
        gate.tick()
        return

    signals = normalizer.normalize(factor_values)
    composite = compositor.compose(signals, factor_values, timestamp=bar.get("time", time.time()))
    gate_result = gate.filter(composite, factor_values, bar)
    gate.tick()
    # ★ 保存因子投票快照到 _live_state, 前端「因子投票」面板读取
    try:
        votes = {}
        for name, sig in signals.items():
            raw_val = factor_values.get(name)
            s_val = sig if isinstance(sig, (int, float)) else 0.0
            r_val = raw_val if isinstance(raw_val, (int, float)) else None
            votes[name] = {
                "signal": round(s_val, 4),
                "raw": round(r_val, 4) if r_val is not None else None,
                "direction": 1 if s_val > 0 else -1 if s_val < 0 else 0,
            }
        _set_factor_snapshot(
            votes,
            {
                "direction": composite.direction,
                "score": round(composite.score, 4),
                "tactical_score": round(composite.tactical_score, 4),
                "macro_score": round(composite.macro_score, 4),
                "n_active": composite.n_active_factors,
                "n_abstain": composite.n_abstain_factors,
                "gate_passed": gate_result.passed,
                "gate_reason": gate_result.reason,
                "ts": time.time(),
            },
        )
    except Exception as _e:
        log(f"tick {tick}: factor votes save failed (non-fatal): {_e}")
    # ── 决策审计: signal ──
    if _DECISION_LOG:
        bar_ts = bar.get("time", 0)
        if bar_ts:
            bar_date = time.strftime("%Y-%m-%d", time.gmtime(bar_ts))
            _DECISION_LOG.log(
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
    send = cfg is not None and cfg.ctrader_send_orders and not cfg.factor_dry_run

    signal_str = ""
    if composite.direction != 0:
        direction_name = {1: "LONG", -1: "SHORT"}.get(composite.direction, "?")
        signal_str = (
            f" signal={direction_name} score={composite.score:.4f}"
            f" tactical={composite.tactical_score:.4f}"
            f" macro={composite.macro_score:.4f}"
            f" n={composite.n_active_factors}"
            f" gate={gate_result.reason}"
        )

    # ── 读 account/positions 缓存 ──
    acct = _live_state_get("account", {}, clone=True) or {}
    pos = _live_state_get("positions", [], clone=True) or []
    if isinstance(pos, dict):
        pos = pos.get("positions", []) or []
    # ★ P0 fix: 统一转 dict — 支持 dataclass / protobuf / 任意非 dict
    if pos and not isinstance(pos[0], dict):
        from backend.ws.endpoints import _position_to_dict
        pos = [_position_to_dict(p) for p in pos]
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
    current_pids: set[int] = set()
    for p in pos:
        pid = p.get("position_id") or p.get("ticket")
        if pid is not None:
            current_pids.add(int(pid))
    closed_pids: set[int] = set()
    attr_engine = pipeline.get("attribution")
    positions_snapshot_ready = bool(_live_state_get("positions_updated_at", 0.0))
    if not _prev_position_ids:
        _prev_position_ids = current_pids.copy()
    elif not current_pids and not positions_snapshot_ready:
        closed_pids = set()
        current_pids = _prev_position_ids.copy()
        log(f"tick {tick}: positions cache not ready, defer close detection")
    else:
        closed_pids = _prev_position_ids - current_pids

    # ── 获取真实 PnL (从 cTrader deals) ──
    _real_pnls: dict[int, dict] = {}
    if closed_pids and bridge is not None:
        try:
            from execution.deal_sync import sync_close_deals_batch
            from backend.core.db import get_state_conn
            _sconn = get_state_conn()
            try:
                _real_pnls = sync_close_deals_batch(bridge, _sconn, closed_pids)
            finally:
                _sconn.close()
        except Exception as _ds_err:
            log(f"tick {tick}: deal_sync error: {_ds_err}")

    for cpid in closed_pids:
        try:
            real_pnl = _real_pnls.get(cpid)
            close_reason = _consume_close_reason(int(cpid), "broker_close")
            close_verdict = _consume_close_verdict(int(cpid), close_reason)
            close_ts = float((real_pnl or {}).get("exec_timestamp") or time.time())
            mc = attr_engine.record_close(cpid,
                                          close_price=current_price,
                                          close_ts=close_ts,
                                          real_pnl=real_pnl)
            # 平仓事件真实发生, 无论是否有因子归因都要计数
            # ★ 优先使用 cTrader 真实 PnL (修复 Bug D: 边际贡献和不可用于熔断器)
            if real_pnl and real_pnl.get("net") is not None:
                total_pnl = real_pnl["net"]
            elif mc:
                total_pnl = sum(mc.values())
            else:
                total_pnl = 0.0
                try:
                    open_price = _pos_open_prices.get(cpid, current_price)
                    dir_sign = 1  # default long
                    for p in pos:
                        ptype = (p.get("type")
                                 if isinstance(p, dict)
                                 else getattr(p, "type", None))
                        if ptype == "sell":
                            dir_sign = -1
                    cpid_vol = _pos_open_api_volume.get(int(cpid), 0.0)
                    if cpid_vol <= 0:
                        cpid_vol = 0.01 * 100.0  # fallback: 0.01 lot = 1 API unit
                    # PnL = 价差 × 方向 × API volume × (oz/API unit)
                    # 1 API unit = 0.01 lot = 1 oz, 所以 PnL = 价差 × 方向 × API volume
                    total_pnl = (current_price - open_price) * dir_sign * cpid_vol
                except Exception as _e2:
                    log(f"tick {tick}: attribution close pos={cpid} PnL fallback error: {_e2}")
            _record_session_trade(total_pnl)
            log(f"tick {tick}: attribution close pos={cpid} pnl={total_pnl:.2f} "
                f"factors={len(mc)}")
            _pos_open_api_volume.pop(int(cpid), None)
            # ── 决策审计: close ──
            if _DECISION_LOG:
                bar_ts = bar.get("time", 0)
                bar_date = time.strftime("%Y-%m-%d", time.gmtime(bar_ts)) if bar_ts else ""
                _DECISION_LOG.log(
                    run_id=_DECISION_LOG_RUN_ID,
                    ts=bar_ts or time.time(),
                    bar_date=bar_date,
                    decision_type="close",
                    strategy="factor_v4",
                    direction=0,
                    confidence=round(total_pnl, 2),
                    decision="closed",
                    meta=_json.dumps({
                        "position_id": cpid,
                        "pnl": round(total_pnl, 2),
                        "price": round(current_price, 2),
                        "tick": tick,
                    }, ensure_ascii=False),
                )
            exit_decision_id = ""
            context_integrity = _lookup_recovery_context_integrity(int(cpid), _RECOVERY_CONTEXT_FULL)
            if _LEDGER:
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
                    exit_decision_id = _LEDGER.log_decision(
                        event_type="close",
                        symbol="XAUUSD+",
                        timeframe=str(getattr(cfg, "timeframe", "") or ""),
                        decision_ts=close_ts,
                        trade_id=str(cpid),
                        position_id=str(cpid),
                        portfolio_state={
                            "balance": acct.get("balance", 0),
                            "equity": acct.get("equity", 0),
                            "session_pnl": _live_state_get("session_pnl", 0),
                        },
                        risk_state=_risk_state_with_verdict_dict(close_verdict),
                        action_score=float(total_pnl),
                        action_reason=close_reason,
                        action_json={
                            "position_id": cpid,
                            "pnl": round(total_pnl, 2),
                            "price": round(current_price, 2),
                            "tick": tick,
                            "close_reason": close_reason,
                            "risk_verdict": close_verdict,
                            "factor_contributions": mc,
                            "real_pnl": real_pnl or {},
                        },
                    )
                    _LEDGER.log_position_event(
                        position_id=str(cpid),
                        trade_id=str(cpid),
                        symbol="XAUUSD+",
                        event_type="closed",
                        avg_price=current_price,
                        realized_pnl=float(total_pnl),
                        details={
                            "tick": tick,
                            "real_pnl": real_pnl or {},
                            "factor_contributions": mc,
                            "close_reason": close_reason,
                            "risk_verdict": close_verdict,
                        },
                        event_ts=close_ts,
                    )
                except Exception as _ledger_err:
                    logger.debug("[live] ledger close failed: %s", _ledger_err)
            if _TRADE_REVIEWER and _EXPERIENCE_BUILDER and _POLICY_SUGGESTER:
                try:
                    review = _TRADE_REVIEWER.review_closed_trade(
                        position_id=str(cpid),
                        pnl=float(total_pnl),
                        close_price=float(current_price),
                        close_ts=close_ts,
                        contributions=mc,
                        exit_decision_id=exit_decision_id,
                        real_pnl=real_pnl,
                        close_reason=close_reason,
                        context_integrity=context_integrity,
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
                except Exception as _learn_err:
                    logger.debug("[live] post-trade learning failed for pos %s: %s", cpid, _learn_err)
            try:
                _mark_recovery_position_closed(
                    int(cpid),
                    close_reason=close_reason,
                    close_pnl=float(total_pnl),
                    closed_at=close_ts,
                    meta={"real_pnl": real_pnl or {}, "factor_contributions": mc or {}},
                )
            except Exception as _recovery_close_err:
                logger.debug("[live] recovery close persist failed for pos %s: %s", cpid, _recovery_close_err)
            # 清理追踪止损状态
            _trailing_state.pop(cpid, None)
            # 清理金字塔规则状态
            _pos_entry_scores.pop(cpid, None)
            _pos_entry_decisions.pop(int(cpid), None)
        except Exception as exc:
            log(f"tick {tick}: attribution close pos={cpid} error: {exc}")
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
    if bridge is not None and hasattr(bridge, "get_spot_price"):
        try:
            spot = bridge.get_spot_price()
            if (spot and spot > 0 and current_price > 0
                    and abs(spot - current_price) / current_price < 0.20):
                current_price = spot
        except Exception as _e2:
            logger.debug("[live] spot price guard failed for tick %s: %s", tick, _e2)

    # ── 执行 ──
    atr_val = factor_values.get("atr_ratio", 0)
    atr_price = atr_val * current_price if atr_val and atr_val > 0 else 0
    sl_price = 0.0
    tp_price = 0.0
    if composite.direction != 0 and gate_result.passed and send:
        direction_name = {1: "LONG", -1: "SHORT"}.get(composite.direction, "?")
        sl_dist = atr_price * cfg.strategy_sl_atr if atr_price > 0 else current_price * 0.02
        tp_dist = atr_price * cfg.strategy_tp_atr if atr_price > 0 else current_price * 0.03
        # 从 bridge metadata 取小数位 → 舍入 SL/TP 防 cTrader 拒绝
        _meta = getattr(bridge, '_symbol_meta', None) or {}
        if not _meta.get('api_min_volume') and bridge is not None and hasattr(bridge, '_resolve_symbol_id'):
            try:
                bridge._resolve_symbol_id()
                _meta = getattr(bridge, '_symbol_meta', None) or {}
            except Exception:
                pass
        _digits = _meta.get('digits', 2)
        sl_price, tp_price = _protection_prices_from_reference(
            composite.direction, current_price, sl_dist, tp_dist, _digits,
        )

        # ── 风控: Kelly 仓位 ──
        acct_clean = _live_state_get("account", {}, clone=True) or {}
        volume = _risk_kelly_volume(cfg, composite.direction, current_price,
                                    sl_price, _meta, acct_clean)
        log(f"tick {tick}: v4 {direction_name} req_api_volume={volume:.0f} "
            f"(Kelly enabled={getattr(cfg, 'kelly_enabled', False)})")

        # ── Phase B: 统一风控裁决 ──
        risk_context = _build_open_trade_risk_context(
            cfg=cfg,
            bridge=bridge,
            acct=acct_clean,
            positions=pos,
            requested_api_volume=volume,
            signal_score=float(composite.score or 0.0),
        )
        risk_verdict = _RISK_POLICY.evaluate("open_trade", risk_context)
        order_blocked = not risk_verdict.allowed
        block_reason = risk_verdict.reason

        if order_blocked:
            log(f"tick {tick}: v4 {direction_name} SKIP ({block_reason})")
            gate_result = type('GateResult', (), {
                'passed': False, 'reason': block_reason,
            })()
            if _LEDGER:
                try:
                    _LEDGER.log_composite_decision(
                        event_type="skip",
                        composite=composite,
                        gate_result=gate_result,
                        symbol="XAUUSD+",
                        timeframe=str(getattr(cfg, "timeframe", "") or ""),
                        decision_ts=bar.get("time", time.time()),
                        portfolio_state={
                            "balance": acct.get("balance", 0),
                            "equity": acct.get("equity", 0),
                            "n_positions": len(pos),
                        },
                        risk_state=_risk_state_with_verdict(risk_verdict),
                        action_reason=block_reason,
                        action_json={
                            "tick": tick,
                            "skip_stage": "risk_policy",
                            "risk_verdict": risk_verdict.to_dict(),
                        },
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
                    fill_price = float(getattr(result, 'price', current_price) or current_price)
                    if fill_price > 0:
                        sl_price, tp_price = _protection_prices_from_reference(
                            composite.direction, fill_price, sl_dist, tp_dist, _digits,
                        )
                    pid = getattr(result, "position_id", 0) or 0
                    if pid <= 0 and pos:
                        p0 = pos[0]
                        if hasattr(p0, 'get'):
                            pid = int(p0.get("position_id") or p0.get("ticket") or 0)
                        else:
                            pid = int(getattr(p0, 'position_id', 0) or getattr(p0, 'ticket', 0) or 0)
                    if pid > 0:
                        refreshed_positions = bridge.get_positions(getattr(bridge, 'symbol', '') or '')
                        actual_api_volume = _resolve_position_api_volume(
                            pid,
                            refreshed_positions,
                            volume,
                        )
                        for _ref_pos in refreshed_positions or []:
                            _ref_pid = (
                                _ref_pos.get("position_id") or _ref_pos.get("ticket")
                                if hasattr(_ref_pos, "get")
                                else getattr(_ref_pos, "position_id", None) or getattr(_ref_pos, "ticket", None)
                            )
                            if _ref_pid is not None and int(_ref_pid) == int(pid):
                                protection_ref = _position_open_price(_ref_pos)
                                if protection_ref > 0:
                                    sl_price, tp_price = _protection_prices_from_reference(
                                        composite.direction, protection_ref, sl_dist, tp_dist, _digits,
                                    )
                                break
                        try:
                            amend_res = bridge.amend_position_sltp(
                                position_id=pid, sl=sl_price, tp=tp_price,
                            )
                            if getattr(amend_res, "success", False):
                                _track_local_sl_tp(pid, sl=sl_price, tp=tp_price)
                                _pos_entry_scores[pid] = composite.score
                                log(f"tick {tick}: v4 {direction_name} ORDER+AMEND OK "
                                    f"api_volume={actual_api_volume:.0f} pos={pid} score={composite.score:.4f}")
                                # ── 执行质量记录 ──
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
                                # ── 记录开仓归因 ──
                                try:
                                    from alpha.attribution_engine import TradeAttribution
                                    total_signal_abs = sum(
                                        abs(s) for s in composite.factor_signals.values()
                                        if s is not None
                                    )
                                    trade_attr = TradeAttribution(
                                        position_id=pid,
                                        open_ts=time.time(),
                                        open_price=current_price,
                                        direction=composite.direction,
                                        factor_signals=dict(composite.factor_signals),
                                        factor_values=dict(composite.factor_values),
                                        active_weights=dict(composite.active_weights),
                                        composite_score=composite.score,
                                        tactical_score=composite.tactical_score,
                                        macro_score=composite.macro_score,
                                        tags_breakdown=dict(composite.tags_breakdown),
                                        total_signal_abs=total_signal_abs,
                                        api_volume=float(actual_api_volume),
                                    )
                                    attr_engine.record_open(pid, trade_attr)
                                    _pos_open_prices[pid] = current_price
                                    _pos_open_api_volume[pid] = float(actual_api_volume)
                                    log(f"tick {tick}: attribution recorded open pos={pid}")
                                    entry_decision_id = ""
                                    if _LEDGER:
                                        try:
                                            entry_decision_id = _LEDGER.log_composite_decision(
                                                event_type="open",
                                                composite=composite,
                                                gate_result=gate_result,
                                                symbol="XAUUSD+",
                                                timeframe=str(getattr(cfg, "timeframe", "") or ""),
                                                decision_ts=bar.get("time", time.time()),
                                                trade_id=str(pid),
                                                position_id=str(pid),
                                                portfolio_state={
                                                    "balance": acct.get("balance", 0),
                                                    "equity": acct.get("equity", 0),
                                                    "n_positions": len(pos),
                                                    "session_pnl": _live_state_get("session_pnl", 0),
                                                },
                                                risk_state=_risk_state_with_verdict(risk_verdict),
                                                action_reason="executed",
                                                action_json={
                                                    "position_id": pid,
                                                    "volume": actual_api_volume,
                                                    "requested_volume": volume,
                                                    "price": round(current_price, 2),
                                                    "sl": round(sl_price, 2),
                                                    "tp": round(tp_price, 2),
                                                    "tick": tick,
                                                    "risk_verdict": risk_verdict.to_dict(),
                                                },
                                            )
                                            _pos_entry_decisions[int(pid)] = entry_decision_id
                                            _LEDGER.log_order_event(
                                                event_type="submitted",
                                                decision_id=entry_decision_id,
                                                trade_id=str(pid),
                                                order_id=str(pid),
                                                broker_order_id=str(pid),
                                                price=float(current_price),
                                                volume=float(actual_api_volume),
                                                status="submitted",
                                                details={"tick": tick, "direction": composite.direction},
                                            )
                                            _LEDGER.log_order_event(
                                                event_type="filled",
                                                decision_id=entry_decision_id,
                                                trade_id=str(pid),
                                                order_id=str(pid),
                                                broker_order_id=str(pid),
                                                price=float(fill_price),
                                                volume=float(actual_api_volume),
                                                status="filled",
                                                details={"tick": tick, "direction": composite.direction},
                                            )
                                            _LEDGER.log_position_event(
                                                position_id=str(pid),
                                                trade_id=str(pid),
                                                symbol="XAUUSD+",
                                                event_type="opened",
                                                net_volume=float(actual_api_volume),
                                                avg_price=float(fill_price),
                                                details={
                                                    "tick": tick,
                                                    "direction": composite.direction,
                                                    "sl": round(sl_price, 2),
                                                    "tp": round(tp_price, 2),
                                                },
                                                event_ts=time.time(),
                                            )
                                        except Exception as _ledger_err:
                                            logger.debug("[live] ledger open failed for pos %s: %s", pid, _ledger_err)
                                    try:
                                        _upsert_recovery_position_state(
                                            {
                                                "position_id": pid,
                                                "symbol": "XAUUSD+",
                                                "direction": composite.direction,
                                                "open_price": float(fill_price or current_price),
                                                "volume": float(actual_api_volume),
                                                "entry_decision_id": entry_decision_id or _lookup_entry_decision_id(int(pid)),
                                            },
                                            broker=broker,
                                            strategy_name=str(_loop_strategy_name or "factor_v4"),
                                            status="open",
                                            context_integrity=_RECOVERY_CONTEXT_FULL,
                                            meta={
                                                "tick": tick,
                                                "sl": round(sl_price, 2),
                                                "tp": round(tp_price, 2),
                                            },
                                        )
                                    except Exception as _recovery_open_err:
                                        logger.debug("[live] recovery open persist failed for pos %s: %s", pid, _recovery_open_err)
                                    # ── 决策审计: open ──
                                    if _DECISION_LOG:
                                        bar_ts = bar.get("time", 0)
                                        bar_date = time.strftime(
                                            "%Y-%m-%d", time.gmtime(bar_ts)
                                        ) if bar_ts else ""
                                        _DECISION_LOG.log(
                                            run_id=_DECISION_LOG_RUN_ID,
                                            ts=bar_ts or time.time(),
                                            bar_date=bar_date,
                                            decision_type="open",
                                            strategy="factor_v4",
                                            direction=composite.direction,
                                            confidence=composite.score,
                                            decision="executed",
                                            meta=_json.dumps({
                                                "position_id": pid,
                                                "volume": actual_api_volume,
                                                "requested_volume": volume,
                                                "price": round(current_price, 2),
                                                "sl": round(sl_price, 2),
                                                "tp": round(tp_price, 2),
                                                "tick": tick,
                                            }, ensure_ascii=False),
                                        )
                                except Exception as attr_err:
                                    log(f"tick {tick}: attribution record_open error: {attr_err}")
                            else:
                                log(f"tick {tick}: v4 {direction_name} AMEND FAILED "
                                    f"pos={pid}: {getattr(amend_res, 'comment', '?')}")
                                _record_filled_position_open_context(
                                    attr_engine=attr_engine,
                                    broker=broker,
                                    cfg=cfg,
                                    bar=bar,
                                    tick=tick,
                                    pid=pid,
                                    actual_api_volume=actual_api_volume,
                                    requested_volume=volume,
                                    fill_price=fill_price,
                                    current_price=current_price,
                                    sl_price=sl_price,
                                    tp_price=tp_price,
                                    acct=acct,
                                    pos=pos,
                                    composite=composite,
                                    gate_result=gate_result,
                                    risk_verdict=risk_verdict,
                                )
                                if _LEDGER:
                                    try:
                                        amend_decision_id = _LEDGER.log_composite_decision(
                                            event_type="amend_failed",
                                            composite=composite,
                                            gate_result=gate_result,
                                            symbol="XAUUSD+",
                                            timeframe=str(getattr(cfg, "timeframe", "") or ""),
                                            decision_ts=bar.get("time", time.time()),
                                            trade_id=str(pid),
                                            position_id=str(pid),
                                            portfolio_state={
                                                "balance": acct.get("balance", 0),
                                                "equity": acct.get("equity", 0),
                                                "n_positions": len(pos),
                                            },
                                            risk_state=_live_state_get("risk", {}, clone=True) or {},
                                            action_reason=str(getattr(amend_res, "comment", "amend_failed") or "amend_failed"),
                                            action_json={
                                                "tick": tick,
                                                "skip_stage": "amend_sltp",
                                                "position_id": pid,
                                                "requested_volume": volume,
                                                "fill_price": fill_price,
                                                "sl": sl_price,
                                                "tp": tp_price,
                                            },
                                        )
                                        _LEDGER.log_order_event(
                                            event_type="amend_failed",
                                            decision_id=amend_decision_id,
                                            trade_id=str(pid),
                                            order_id=str(pid),
                                            broker_order_id=str(pid),
                                            price=float(fill_price),
                                            volume=float(actual_api_volume),
                                            status="failed",
                                            details={
                                                "tick": tick,
                                                "direction": composite.direction,
                                                "comment": str(getattr(amend_res, "comment", "") or ""),
                                            },
                                        )
                                    except Exception as _ledger_err:
                                        logger.debug("[live] ledger amend failed event failed for pos %s: %s", pid, _ledger_err)
                        except Exception as e:
                            log(f"tick {tick}: v4 {direction_name} amend exception: {e}")
                            _record_filled_position_open_context(
                                attr_engine=attr_engine,
                                broker=broker,
                                cfg=cfg,
                                bar=bar,
                                tick=tick,
                                pid=pid,
                                actual_api_volume=actual_api_volume,
                                requested_volume=volume,
                                fill_price=fill_price,
                                current_price=current_price,
                                sl_price=sl_price,
                                tp_price=tp_price,
                                acct=acct,
                                pos=pos,
                                composite=composite,
                                gate_result=gate_result,
                                risk_verdict=risk_verdict,
                            )
                            if _LEDGER:
                                try:
                                    amend_decision_id = _LEDGER.log_composite_decision(
                                        event_type="amend_failed",
                                        composite=composite,
                                        gate_result=gate_result,
                                        symbol="XAUUSD+",
                                        timeframe=str(getattr(cfg, "timeframe", "") or ""),
                                        decision_ts=bar.get("time", time.time()),
                                        trade_id=str(pid),
                                        position_id=str(pid),
                                        portfolio_state={
                                            "balance": acct.get("balance", 0),
                                            "equity": acct.get("equity", 0),
                                            "n_positions": len(pos),
                                        },
                                        risk_state=_live_state_get("risk", {}, clone=True) or {},
                                        action_reason=f"amend_exception:{type(e).__name__}",
                                        action_json={
                                            "tick": tick,
                                            "skip_stage": "amend_sltp",
                                            "position_id": pid,
                                            "requested_volume": volume,
                                            "fill_price": fill_price,
                                            "sl": sl_price,
                                            "tp": tp_price,
                                            "error": str(e)[:300],
                                        },
                                    )
                                    _LEDGER.log_order_event(
                                        event_type="amend_failed",
                                        decision_id=amend_decision_id,
                                        trade_id=str(pid),
                                        order_id=str(pid),
                                        broker_order_id=str(pid),
                                        price=float(fill_price),
                                        volume=float(actual_api_volume),
                                        status="failed",
                                        details={
                                            "tick": tick,
                                            "direction": composite.direction,
                                            "error": str(e)[:300],
                                        },
                                    )
                                except Exception as _ledger_err:
                                    logger.debug("[live] ledger amend exception event failed for pos %s: %s", pid, _ledger_err)
                    else:
                        log(f"tick {tick}: v4 {direction_name} ORDER OK (no position_id) "
                            f"vol={volume}")
                elif result is not None and not getattr(result, "success", False):
                    log(f"tick {tick}: v4 {direction_name} ORDER FAILED: "
                        f"{getattr(result, 'error_code', '?')} {getattr(result, 'comment', '')}")
                    if _LEDGER:
                        try:
                            reason = (
                                f"{getattr(result, 'error_code', '?')} "
                                f"{getattr(result, 'comment', '')}"
                            ).strip()
                            failed_decision_id = _LEDGER.log_composite_decision(
                                event_type="order_failed",
                                composite=composite,
                                gate_result=gate_result,
                                symbol="XAUUSD+",
                                timeframe=str(getattr(cfg, "timeframe", "") or ""),
                                decision_ts=bar.get("time", time.time()),
                                portfolio_state={
                                    "balance": acct.get("balance", 0),
                                    "equity": acct.get("equity", 0),
                                    "n_positions": len(pos),
                                },
                                risk_state=_live_state_get("risk", {}, clone=True) or {},
                                action_reason=reason or "order_failed",
                                action_json={
                                    "tick": tick,
                                    "skip_stage": "broker_order_failed",
                                    "requested_volume": volume,
                                    "price": round(current_price, 2),
                                    "sl": round(sl_price, 2),
                                    "tp": round(tp_price, 2),
                                    "error_code": str(getattr(result, "error_code", "") or ""),
                                    "comment": str(getattr(result, "comment", "") or ""),
                                },
                            )
                            _LEDGER.log_order_event(
                                event_type="order_failed",
                                decision_id=failed_decision_id,
                                price=float(current_price),
                                volume=float(volume),
                                status="failed",
                                details={
                                    "tick": tick,
                                    "direction": composite.direction,
                                    "error_code": str(getattr(result, "error_code", "") or ""),
                                    "comment": str(getattr(result, "comment", "") or ""),
                                },
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

    # ── AWE 自适应追踪止损 ──
    if pos and bridge is not None and atr_price > 0:
        _update_trailing_stops(
            bridge, pos, current_price, pipeline, atr_price,
            tick, log,
        )
    if pos and bridge is not None and cfg is not None:
        _run_position_supervision(
            bridge,
            pos,
            cfg=cfg,
            acct=acct,
            tick=tick,
            log=log,
        )
    if pos and bridge is not None and cfg is not None:
        _enforce_holding_timeout(
            bridge,
            pos,
            cfg=cfg,
            tick=tick,
            log=log,
        )

    # ── 更新上一 tick 持仓 ID, 供下次平仓检测 ──
    _prev_position_ids = current_pids

    global _latest_price
    _latest_price = current_price


# ── AWE 自适应追踪止损 ──────────────────────────────────────

def _update_trailing_stops(
    bridge, pos: list, current_price: float, pipeline: dict,
    atr_price: float, tick: int, log,
) -> None:
    """每 tick 检查追踪止损, 需要时 amend SL.

    追踪松紧度由 AWE composite_conviction() 动态决定:
        ≥0.7 → 紧追踪 (1.5×ATR), 快速锁利
        0.4~0.7 → 中等 (2.0×ATR)
        <0.4 → 松追踪 (3.0×ATR), 只保本
    """
    global _trailing_state
    awe = pipeline.get("awe")
    if awe is None:
        return

    try:
        conviction = awe.composite_conviction()
    except Exception:
        return

    if conviction >= 0.7:
        trail_atr = 1.5
        activate_atr = 1.0
    elif conviction >= 0.4:
        trail_atr = 2.0
        activate_atr = 1.5
    else:
        trail_atr = 3.0
        activate_atr = 2.0

    for p in pos:
        pid = p.get("position_id") or p.get("ticket")
        if pid is None:
            continue
        pid = int(pid)
        direction = p.get("direction", 0)
        entry = float(p.get("entry_price", 0) or p.get("open_price", 0))
        if entry <= 0 or direction == 0:
            continue

        state = _trailing_state.setdefault(pid, {
            "best_price": entry,
            "activated": False,
            "entry_price": entry,
            "direction": direction,
        })

        # 更新最优价
        if direction == 1:  # LONG
            if current_price > state["best_price"]:
                state["best_price"] = current_price
            price_move = current_price - entry
        else:  # SHORT
            if current_price < state["best_price"]:
                state["best_price"] = current_price
            price_move = entry - current_price

        # 激活检查
        if not state["activated"] and price_move >= atr_price * activate_atr:
            state["activated"] = True
            log(f"tick {tick}: trail activated pos={pid} "
                f"move={price_move:.2f} conviction={conviction:.2f}")

        if not state["activated"]:
            continue

        # 计算目标 SL
        if direction == 1:
            target_sl = state["best_price"] - atr_price * trail_atr
            current_sl = float(p.get("sl", 0))
            current_tp = float(p.get("tp", 0) or p.get("takeProfit", 0))
            if target_sl > current_sl + 0.01:
                try:
                    bridge.amend_position_sltp(
                        position_id=pid, sl=round(target_sl, 2),
                        tp=round(current_tp, 2) if current_tp > 0 else 0.0,
                    )
                    log(f"tick {tick}: trail LONG pos={pid} sl={target_sl:.2f} "
                        f"best={state['best_price']:.2f} conv={conviction:.2f}")
                except Exception as _e2:
                    logger.debug("[live] trail LONG amend failed for pos %s: %s", pid, _e2)
        else:
            target_sl = state["best_price"] + atr_price * trail_atr
            current_sl = float(p.get("sl", 0))
            current_tp = float(p.get("tp", 0) or p.get("takeProfit", 0))
            if current_sl == 0 or target_sl < current_sl - 0.01:
                try:
                    bridge.amend_position_sltp(
                        position_id=pid, sl=round(target_sl, 2),
                        tp=round(current_tp, 2) if current_tp > 0 else 0.0,
                    )
                    log(f"tick {tick}: trail SHORT pos={pid} sl={target_sl:.2f} "
                        f"best={state['best_price']:.2f} conv={conviction:.2f}")
                except Exception as _e2:
                    logger.debug("[live] trail SHORT amend failed for pos %s: %s", pid, _e2)


def _enforce_holding_timeout(
    bridge,
    pos: list,
    *,
    cfg,
    tick: int,
    log,
) -> None:
    max_holding_bars = int(getattr(cfg, "risk_max_holding_bars", 0) or 0)
    if max_holding_bars <= 0:
        return

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
        if max_holding_seconds <= 0 or holding_seconds < max_holding_seconds:
            continue

        close_verdict = _RISK_POLICY.evaluate("close_position", close_context)
        if not close_verdict.allowed:
            logger.warning("[live] holding timeout close blocked pos=%s reason=%s", pid, close_verdict.reason)
            continue
        _remember_close_reason(pid, "holding_timeout")
        _remember_close_verdict(pid, close_verdict)
        try:
            result = bridge.close_position(pid)
        except Exception as exc:
            logger.warning("[live] holding timeout close exception pos=%s: %s", pid, exc)
            continue
        if getattr(result, "success", False):
            log(
                f"tick {tick}: holding timeout close sent pos={pid} "
                f"held={holding_seconds:.0f}s limit={max_holding_seconds:.0f}s"
            )


def _write_live_trade_log_factor(
    tick: int, price: float, acct: dict, pos: list,
    composite, gate_result, state: dict,
) -> None:
    """因子管道版结构化审计日志 (写入 DecisionLogStore → state.db)。"""
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
            _DECISION_LOG.log(
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
