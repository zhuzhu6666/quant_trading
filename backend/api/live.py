"""Live trading API.

Endpoints (all JWT-protected except /api/auth/login; /api/health):
  GET  /api/live/status           — broker status + loop status
  GET  /api/live/loop-status      — current in-process live loop status
  GET  /api/live/account          — real broker account info (balance/equity/margin)
  GET  /api/live/positions        — real open positions
  POST /api/live/start            — start in-process live loop via start_loop thread model
  POST /api/live/stop             — stop in-process live loop
  POST /api/live/emergency-close  — flatten all positions; requires X-Confirm: emergency

当前实盘主链口径：
backend/services/live_service.py -> risk/policy_service.py -> execution/ctrader_bridge.py -> execution/deal_sync.py -> backend/ledger/service.py

/start 和 /stop 仅负责通过 start_loop/stop_loop 控制该主链中的线程化循环，不是 subprocess 模型。
"""
from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel
import re

from backend.core.auth import RequireRecentStepUp, RequireRiskReductionUser, RequireUser
from backend.services.mutation_audit import confirm_header_valid, record_api_mutation
from backend.services.live_service import (
    _should_send_orders,
    emergency_close,
    get_account,
    get_positions,
    get_status,
    loop_status,
    start_loop,
    stop_loop,
)
from backend.services.live_safety_state import append_safety_outbox
from backend.services.api_fact_views import (
    account_fact_payload,
    live_status_fact_payload,
    loop_fact_payload,
    positions_fact_payload,
    realized_fact_payload,
    session_fact_payload,
    strategy_fact_payload,
)
from backend.services.realized_pnl import get_realized_pnl_series

router = APIRouter(prefix="/api/live", tags=["live"])


class StartRequest(BaseModel):
    broker: str = "ctrader"  # 唯一执行通道
    strategy_name: str = "v1_minimal_ma_cross"  # audit 2026-06-08: v1 loop 没真装 strategy, 仅记录名


class EmergencyCloseRequest(BaseModel):
    broker: str  # "ctrader"
    symbol: str | None = None


def _fact_runtime_observation() -> dict:
    """Read only timestamps/provenance needed by the API fact boundary."""
    from backend.services.live_service import _live_state_get

    diag = _live_state_get("_diag", {}, clone=True) or {}
    return {
        "diagnostic_ts": diag.get("ts"),
        "session_source": _live_state_get("session_state_source", "none"),
        # Session freshness is independent from broker position/account
        # freshness.  A successful position poll must not make a failed or
        # cached session-risk restore appear current.
        "session_observed_at": _live_state_get("session_observed_at", 0.0),
    }


@router.get("/status")
def status(_user: RequireUser) -> dict:
    observation = _fact_runtime_observation()
    return live_status_fact_payload(
        get_status(),
        diagnostic_ts=observation.get("diagnostic_ts"),
    )


@router.get("/loop-status")
def get_loop_status(_user: RequireUser) -> dict:
    observation = _fact_runtime_observation()
    return loop_fact_payload(
        loop_status(),
        diagnostic_ts=observation.get("diagnostic_ts"),
    )


@router.get("/account")
def get_account_endpoint(_user: RequireUser, broker: str = Query("ctrader")) -> dict:
    return account_fact_payload(get_account(broker))


@router.get("/positions")
def get_positions_endpoint(
    _user: RequireUser,
    broker: str = Query("ctrader"),
    symbol: str | None = Query(None),
) -> dict:
    return positions_fact_payload(get_positions(broker, symbol))


@router.get("/realized-pnl-series")
def realized_pnl_series_endpoint(
    _user: RequireUser,
    scope: str = Query("today", pattern="^(today|24h|7d|30d|all)$"),
    from_ts: float | None = Query(None),
    to_ts: float | None = Query(None),
    tz: str = Query("Asia/Shanghai"),
) -> dict:
    result = get_realized_pnl_series(scope=scope, from_ts=from_ts, to_ts=to_ts, tz=tz)
    try:
        currency = str((get_account("ctrader") or {}).get("currency") or "").strip()
        if currency:
            result["currency"] = currency
    except Exception:
        pass
    return realized_fact_payload(result)


@router.post("/start")
def start(
    _user: RequireRecentStepUp,
    req: StartRequest,
    x_confirm: str | None = Header(default=None),
) -> dict:
    """Start the in-process live loop thread via start_loop."""
    before = loop_status()
    if not confirm_header_valid(x_confirm, "start-live"):
        record_api_mutation(
            user=_user,
            endpoint="/api/live/start",
            action="start_live_loop",
            status="blocked",
            before=before,
            after=before,
            result={"broker": req.broker, "strategy_name": req.strategy_name},
            reason="missing_x_confirm",
            required_confirm="start-live",
            confirm_ok=False,
        )
        raise HTTPException(
            status_code=403,
            detail={"error": "missing_x_confirm", "msg": "send X-Confirm: start-live header"},
        )
    result = start_loop(req.broker, strategy_name=req.strategy_name)
    record_api_mutation(
        user=_user,
        endpoint="/api/live/start",
        action="start_live_loop",
        status="applied" if result.get("ok") else "blocked",
        before=before,
        after=loop_status(),
        result=result,
        required_confirm="start-live",
        confirm_ok=True,
    )
    return result


@router.post("/stop")
def stop(_user: RequireRiskReductionUser) -> dict:
    """Stop the in-process live loop thread."""
    return stop_loop()


@router.post("/emergency-close")
def emergency(
    _user: RequireRiskReductionUser,
    req: EmergencyCloseRequest,
    x_confirm: str | None = Header(default=None),
) -> dict:
    if x_confirm != "emergency":
        record_api_mutation(
            user=_user,
            endpoint="/api/live/emergency-close",
            action="emergency_close",
            status="blocked",
            result={"broker": req.broker, "symbol": req.symbol, "reason": "missing_x_confirm"},
            reason="missing_x_confirm",
            required_confirm="emergency",
            confirm_ok=False,
        )
        raise HTTPException(
            status_code=403,
            detail={"error": "missing_x_confirm", "msg": "send X-Confirm: emergency header"},
        )
    result = emergency_close(req.broker, req.symbol)
    try:
        record_api_mutation(
            user=_user,
            endpoint="/api/live/emergency-close",
            action="emergency_close",
            status="applied" if result.get("ok", True) else "blocked",
            result=result,
            required_confirm="emergency",
            confirm_ok=True,
        )
    except Exception as exc:
        # Mutation audit is governance evidence, not broker authority.  A
        # completed/partial emergency result must still reach the operator.
        try:
            append_safety_outbox(
                event_type="emergency_api_mutation_audit_failed",
                correlation_id=str(result.get("emergency_id") or ""),
                payload={
                    "endpoint": "/api/live/emergency-close",
                    "result": result,
                },
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:
            pass
    return result


@router.get("/strategy-status")
def strategy_status_endpoint(_user: RequireUser) -> dict:
    """当前因子管道状态、持仓、最近信号及闸门判定。"""
    from pathlib import Path as _Path
    from backend.services.live_service import (
        _factor_pipeline,
        _live_state,
        get_live_readiness,
        loop_status as _ls,
    )

    loop = _ls()
    acct = _live_state.get("account") or {}
    readiness = get_live_readiness("ctrader")
    positions = readiness.get("positions") or []
    cb = _live_state.get("circuit_breaker", False)
    cb_reason = _live_state.get("circuit_reason", "")

    pos_list = positions
    n_pos = len(pos_list)
    pos_dir = "FLAT"
    pos_entry = 0.0
    if pos_list:
        p = pos_list[0]
        pos_dir = "LONG" if p.get("type") == "buy" else "SHORT"
        pos_entry = float(p.get("price_open", 0))

    # ── v4 因子管道状态 ──
    pipeline = _factor_pipeline or {}
    engine = pipeline.get("engine")
    attr = pipeline.get("attribution")
    awe = pipeline.get("awe")

    v4_status: dict = {
        "pipeline_active": bool(pipeline),
        "engine_warm": engine.is_warm if engine else False,
        "buffer_size": engine.buffer_size if engine else 0,
        "n_attribution_trades": sum(s.n_trades for s in attr.get_all_factor_stats().values()) if attr else 0,
        "awe_conviction": round(awe.composite_conviction(), 3) if awe else 0.5,
    }

    # 最近信号: 从 live_loop.log 读取 v4 格式
    recent_signals: list[dict] = []
    logs_dir = _Path(__file__).resolve().parent.parent.parent / "logs"
    for log_name in ["live_loop.log"]:
        log_path = logs_dir / log_name
        if not log_path.exists():
            continue
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(size - 8000, 0))
                raw = f.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()
            if size > 8000:
                lines = lines[1:]
            for line in lines[-30:]:
                current = re.search(
                    r"signal=(LONG|SHORT)\s+score=([\d.\-]+)\s+"
                    r"tactical=([\d.\-]+)\s+macro=([\d.\-]+)\s+"
                    r"available=(\d+)\s+scoring=(\d+)\s+"
                    r"contributing=(\d+)\s+gate=(\S+)",
                    line,
                )
                legacy = None if current else re.search(
                    r"signal=(LONG|SHORT)\s+score=([\d.\-]+)\s+"
                    r"tactical=([\d.\-]+)\s+macro=([\d.\-]+)\s+"
                    r"n=(\d+)\s+gate=(\S+)",
                    line,
                )
                m = current or legacy
                if m:
                    available = int(m.group(5))
                    scoring = int(m.group(6)) if current else available
                    contributing = int(m.group(7)) if current else scoring
                    recent_signals.append({
                        "direction": m.group(1),
                        "score": float(m.group(2)),
                        "tactical_score": float(m.group(3)),
                        "macro_score": float(m.group(4)),
                        # Keep the old field for compatibility; consumers
                        # should use the explicit semantic counters below.
                        "n_active_factors": available,
                        "n_available_factors": available,
                        "n_scoring_factors": scoring,
                        "n_contributing_factors": contributing,
                        "gate_reason": m.group(8) if current else m.group(6),
                    })
        except Exception:
            pass

    # ── 执行链路: 从日志里提取最近的「尝试 / 本地拦截 / cTrader 拒单 / 成功」 ──
    execution_events: list[dict] = []
    execution_summary = {
        "attempts": 0,
        "wire_sends": 0,
        "local_skips": 0,
        "successes": 0,
        "failures": 0,
        "last_stage": "unknown",
        "last_reason": "",
        "last_tick": None,
    }
    seen_exec_keys: set[tuple] = set()
    try:
        logs_dir = _Path(__file__).resolve().parent.parent.parent / "logs"
        log_names = ["live_loop.log", "backend.log"]
        for log_name in log_names:
            log_path = logs_dir / log_name
            if not log_path.exists():
                continue
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                # backend.log 增长快, 多读一些确保 wire_send 事件不丢失
                read_size = 800000 if "backend" in log_name else 80000
                f.seek(max(size - read_size, 0))
                raw = f.read().decode("utf-8", errors="replace")
            lines = raw.splitlines()[-5000:]
            for line in lines:
                # 提取时间戳: HH:MM:SS (live_loop) 或 YYYY-MM-DD HH:MM:SS (backend)
                _ts = None
                _m_time = re.match(r"(\d{2}:\d{2}:\d{2}) ", line)
                if _m_time:
                    _ts = _m_time.group(1)
                else:
                    _m_time = re.match(r"\d{4}-\d{2}-\d{2} (\d{2}:\d{2}:\d{2})", line)
                    if _m_time:
                        _ts = _m_time.group(1)
                m = re.search(r"market_order: account=(\d+) symbolId=(\d+) side=(\d+) volume=(\d+) api_units \(= ([\d.]+) api\)", line)
                if m:
                    key = ("wire_send", m.group(1), m.group(2), m.group(3), m.group(4))
                    if key in seen_exec_keys:
                        continue
                    seen_exec_keys.add(key)
                    execution_events.append({
                        "tick": None, "time": _ts,
                        "direction": "BUY" if m.group(3) == "1" else "SELL",
                        "stage": "wire_send",
                        "reason": f"实际发送 {m.group(5)} API量 (account={m.group(1)})",
                    })
                    execution_summary["wire_sends"] += 1
                    execution_summary["last_stage"] = "wire_send"
                    execution_summary["last_reason"] = execution_events[-1]["reason"]
                    continue
                m = re.search(r"market_order send error: (.+?)\. Reconciling positions", line)
                if m:
                    key = ("ctrader_send_err", m.group(1))
                    if key in seen_exec_keys:
                        continue
                    seen_exec_keys.add(key)
                    reason_text = m.group(1)
                    execution_events.append({
                        "tick": None, "time": _ts,
                        "direction": "—",
                        "stage": "ctrader_send_err",
                        "reason": reason_text,
                    })
                    execution_summary["failures"] += 1
                    execution_summary["last_stage"] = "ctrader_send_err"
                    execution_summary["last_reason"] = reason_text
                    continue
                m = re.search(r"tick (\d+): v4 (LONG|SHORT) req_api_volume=([\d.]+) \(Kelly enabled=(True|False)\)", line)
                if m:
                    key = ("attempt", m.group(1), m.group(2), m.group(3), m.group(4))
                    if key in seen_exec_keys:
                        continue
                    seen_exec_keys.add(key)
                    execution_events.append({
                        "tick": int(m.group(1)), "time": _ts,
                        "direction": m.group(2),
                        "stage": "attempt",
                        "reason": f"策略准备下单，Kelly enabled={m.group(4)}",
                    })
                    execution_summary["attempts"] += 1
                    execution_summary["last_stage"] = "attempt"
                    execution_summary["last_reason"] = execution_events[-1]["reason"]
                    execution_summary["last_tick"] = int(m.group(1))
                    continue
                m = re.search(r"tick (\d+): v4 (LONG|SHORT) SKIP \((.+)\)", line)
                if m:
                    key = ("local_skip", m.group(1), m.group(2), m.group(3))
                    if key in seen_exec_keys:
                        continue
                    seen_exec_keys.add(key)
                    reason_text = m.group(3)
                    execution_events.append({
                        "tick": int(m.group(1)), "time": _ts,
                        "direction": m.group(2),
                        "stage": "local_skip",
                        "reason": reason_text,
                    })
                    execution_summary["local_skips"] += 1
                    execution_summary["last_stage"] = "local_skip"
                    execution_summary["last_reason"] = reason_text
                    execution_summary["last_tick"] = int(m.group(1))
                    continue
                m = re.search(r"tick (\d+): v4 (LONG|SHORT) ORDER\+AMEND OK (?:vol|api_volume)=([\d.]+) pos=(\d+) score=([-\d.]+)", line)
                if m:
                    key = ("success", m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
                    if key in seen_exec_keys:
                        continue
                    seen_exec_keys.add(key)
                    execution_events.append({
                        "tick": int(m.group(1)), "time": _ts,
                        "direction": m.group(2),
                        "stage": "success",
                        "reason": f"cTrader 已成交并完成止盈止损，pos={m.group(4)}",
                    })
                    execution_summary["successes"] += 1
                    execution_summary["last_stage"] = "success"
                    execution_summary["last_reason"] = execution_events[-1]["reason"]
                    execution_summary["last_tick"] = int(m.group(1))
                    continue
                m = re.search(r"tick (\d+): v4 (LONG|SHORT) ORDER FAILED: ([A-Z_]+) (.+)", line)
                if m:
                    key = ("ctrader_reject", m.group(1), m.group(2), m.group(3), m.group(4))
                    if key in seen_exec_keys:
                        continue
                    seen_exec_keys.add(key)
                    reason_text = f"{m.group(3)} {m.group(4)}".strip()
                    execution_events.append({
                        "tick": int(m.group(1)), "time": _ts,
                        "direction": m.group(2),
                        "stage": "ctrader_reject",
                        "reason": reason_text,
                    })
                    execution_summary["failures"] += 1
                    execution_summary["last_stage"] = "ctrader_reject"
                    execution_summary["last_reason"] = reason_text
                    execution_summary["last_tick"] = int(m.group(1))
                    continue
        execution_events = execution_events[-8:]
        # 按 tick 号选最新带 tick 的事件作为 last_stage (wire_send 没有 tick 不覆盖)
        _best_tick = execution_summary["last_tick"]
        if _best_tick is not None and _best_tick >= 0:
            # 从 events 里找到最后一条有 tick 的事件
            for ev in reversed(execution_events):
                if ev.get("tick") is not None:
                    execution_summary["last_stage"] = ev["stage"]
                    execution_summary["last_reason"] = ev["reason"]
                    break
        elif execution_events:
            # 完全没有 tick 事件才用 wire_send
            execution_summary["last_stage"] = execution_events[-1]["stage"]
            execution_summary["last_reason"] = execution_events[-1]["reason"]
    except Exception:
        pass

    send_orders = _should_send_orders("ctrader")
    execution_mode = "LIVE" if send_orders else "DRY-RUN"

    # 原因
    reason = ""
    if cb:
        reason = f"熔断激活: {cb_reason}" if cb_reason else "熔断激活"
    elif not loop.get("running"):
        reason = "实盘未启动"
    elif not v4_status.get("pipeline_active"):
        reason = "因子管道未初始化"
    elif not v4_status.get("engine_warm"):
        reason = f"因子引擎预热中 ({v4_status.get('buffer_size', 0)}/50 bars)"
    elif pos_dir != "FLAT":
        reason = f"已有 {pos_dir} 持仓 @ {pos_entry:.2f}"
    elif recent_signals:
        last = recent_signals[-1]
        if last.get("gate_reason", "").startswith("passed"):
            reason = f"最近信号: {last['direction']} score={last['score']:.3f} (闸门通过)"
        else:
            reason = f"最近信号: {last['direction']} score={last['score']:.3f} (闸门: {last.get('gate_reason', '?')})"
    elif not send_orders:
        reason = "DRY-RUN 模式 (不发实单)"
    else:
        reason = "等待因子信号"

    # ── 因子投票快照 (每个已提交决策 K 线更新, 前端「因子投票」面板) ──
    factor_votes = _live_state.get("last_factor_votes") or {}
    last_composite = _live_state.get("last_composite") or {}
    # 诊断信息
    diag = _live_state.get("_diag") or {}

    # 只暴露真正参与交易的因子: 过滤掉 DSL / PCA 这类内部自动生成项。
    # 以 config.runtime_config 为准, 保持和交易策略的实际权重表一致。
    try:
        from config.runtime_config import shared as _rc_shared
        cfg = _rc_shared()
        valid_names = set((cfg.factor_portfolio_weights or {}).keys()) | set((cfg.factor_signal_config or {}).keys())
        if valid_names:
            factor_votes = {k: v for k, v in factor_votes.items() if k in valid_names}
    except Exception:
        # 配置读取失败时不阻塞页面, 但仍保留原始投票快照。
        pass

    payload = {
        "running": loop.get("running", False),
        "broker": loop.get("broker") or "ctrader",
        "strategy": "factor_pipeline_v4",
        "mode": execution_mode,
        "execution_mode": execution_mode,
        "send_orders": send_orders,
        "dry_run": not send_orders,
        "readiness": readiness,
        "position": {"dir": pos_dir, "entry": round(pos_entry, 2), "count": n_pos},
        "circuit_breaker": cb,
        "circuit_reason": cb_reason,
        "reason": reason,
        "recent_signals": recent_signals[-5:],
        "v4_status": v4_status,
        "factor_votes": factor_votes,
        "last_composite": last_composite,
        "_diag": diag,
        "execution_events": execution_events,
        "execution_summary": execution_summary,
    }
    return strategy_fact_payload(payload, diagnostic_ts=diag.get("ts"))


@router.get("/session-stats")
def session_stats_endpoint(_user: RequireUser) -> dict:
    """今日会话统计: 盈亏/交易笔数/胜率/回撤. HTTP 后备, 不依赖 WS."""
    from backend.services.live_service import _live_state
    payload = {
        "pnl_today": float(_live_state.get("session_pnl", 0)),
        "trades": int(_live_state.get("session_trades", 0)),
        "wins": int(_live_state.get("session_winning", 0)),
        "losses": int(_live_state.get("session_losing", 0)),
        "drawdown_pct": float(_live_state.get("session_max_drawdown_pct", 0)),
        "consecutive_loss": int(_live_state.get("session_consecutive_loss", 0)),
    }
    observation = _fact_runtime_observation()
    return session_fact_payload(
        payload,
        source=str(observation.get("session_source") or "none"),
        observed_at=observation.get("session_observed_at"),
    )
