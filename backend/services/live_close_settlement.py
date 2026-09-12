"""Live close-settlement family: runtime_kv plumbing, session state restore,
deferred-close latches, pending-close bookkeeping, close reason/verdict memory,
recovery sync/replay/retirement, and daily-drawdown evaluation.

Extracted from ``backend.services.live_service`` (2026-09-12 structural repair,
AST-precise move, public names).  The live loop keeps calling these through the
settlement public API; the only back-references are function-local lazy imports
for loop-owned helpers (reconcile publisher, risk-reduction aux recorder,
loss-streak book, bridge risk context).
"""
from __future__ import annotations
from typing import Any as _Any  # noqa: F401  (reserved for typing imports below)


_ls_module = None


def _live_service():
    """Lazy handle to the live loop module (import-order-safe back-reference).

    Only the loop-owned helpers live here: the reconcile publisher, the
    risk-reduction aux failure recorder, the loss-streak book, the bridge risk
    context, and the strategy-name accessor.  Resolved at call time so the
    import graph stays acyclic regardless of which module loads first.
    """
    global _ls_module
    if _ls_module is None:
        from backend.services import live_service as _module
        _ls_module = _module
    return _ls_module



from backend.core.retry import retry
from backend.services.canonical_v2_reader import iter_decision_rows, iter_supervisor_trace_rows, load_position_decision_index
from backend.services.live_execution_recovery import (
    PositionRecoveryRuntime,
    bootstrap_position_recovery as _runtime_bootstrap_position_recovery,
)
from backend.services.live_position_lifecycle import (
    build_recovered_open_ledger_payloads as _lifecycle_build_recovered_open_ledger_payloads,
    build_recovery_closed_update_payload as _lifecycle_build_recovery_closed_update_payload,
    build_recovery_meta_update_payload as _lifecycle_build_recovery_meta_update_payload,
    build_replayed_close_payloads as _lifecycle_build_replayed_close_payloads,
    build_risk_state_with_policy_verdict as _lifecycle_build_risk_state_with_policy_verdict,
    classify_close_source_from_evidence as _lifecycle_classify_close_source_from_evidence,
    consume_close_reason as _lifecycle_consume_close_reason,
    consume_close_verdict as _lifecycle_consume_close_verdict,
    filter_removed_live_position as _lifecycle_filter_removed_live_position,
    forget_pending_close_state as _lifecycle_forget_pending_close_state,
    latest_close_evidence as _lifecycle_latest_close_evidence,
    normalize_position_snapshot as _lifecycle_normalize_position_snapshot,
    normalize_protection_trace_row as _lifecycle_normalize_protection_trace_row,
    normalize_recovery_position_row as _lifecycle_normalize_recovery_position_row,
    normalize_supervisor_event_row as _lifecycle_normalize_supervisor_event_row,
    recovery_active_position_ids as _lifecycle_recovery_active_position_ids,
    recovery_missing_position_ids as _lifecycle_recovery_missing_position_ids,
    recovery_replay_lookback_from as _lifecycle_recovery_replay_lookback_from,
    remember_close_reason as _lifecycle_remember_close_reason,
    remember_close_verdict as _lifecycle_remember_close_verdict,
)
from backend.services.live_reconciliation import (
    LIVE_SAFETY_FRESHNESS_SEC as _LIVE_SAFETY_FRESHNESS_SEC,
    explicit_position_reconcile as _explicit_position_reconcile,
    reconcile_value as _reconcile_value,
)
from backend.services.live_recovery_close import (
    MissingPositionRetirementRuntime,
    RecoveredCloseReplayRuntime,
    replay_recovered_close as _runtime_replay_recovered_close,
    retire_broker_missing_position as _runtime_retire_missing_position,
)
from backend.services.live_recovery_position_store import RecoveryPositionStore, RecoveryPositionStoreRuntime
from backend.services.live_risk_reduction import (
    record_risk_reduction_aux_failure as _risk_reduction_record_aux_failure,
)
from backend.services.live_safety_state import activate_no_new_risk_latch, append_safety_outbox, no_new_risk_latch_status, release_no_new_risk_latch_cause
from backend.services.live_state_store import _LIVE_STATE_LOCK, _live_state, live_state_get
from backend.services.session_restore import (
    PartialCloseSessionFactRuntime,
    authoritative_close_pnl as _authoritative_close_pnl,
    build_authoritative_session_state as _session_build_authoritative_state,
    load_authoritative_session_deal_facts as _session_load_authoritative_deal_facts,
    resolve_session_restore as _session_resolve_restore,
    session_trade_window as _session_restore_trade_window,
    sync_partial_close_session_fact as _session_sync_partial_close_fact,
)
from config.runtime_config import bounded_demo_mode_active
from datetime import datetime, timezone
from functools import partial
from loguru import logger
from pathlib import Path
from risk.runtime_policy import RiskLimitSnapshot
from zoneinfo import ZoneInfo
from typing import Any
import json
import threading
import time
from backend.services.live_position_lifecycle import payload_get as _payload_get
from backend.services.live_state_store import (
    _LIVE_STATE_LOCK,
    _live_state,
    live_state_get,
    live_state_update,
)


_RUNTIME_KV_PENDING_PATH = Path("data/charts/runtime_kv.pending.jsonl")


_RUNTIME_KV_PENDING_LOCK = threading.Lock()


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


_RUNTIME_KV_LOOP_DESIRED = "live.loop.desired_state"


_RUNTIME_KV_SESSION_STATE_PREFIX = "live.session_state."


_RECOVERY_REPLAY_LOOKBACK_SEC = 7 * 24 * 3600


_RECOVERY_ZERO_CONFIRMATIONS_REQUIRED = 2


def record_risk_reduction_aux_failure(
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
        runtime=_live_service()._risk_reduction_runtime(),
    )


def recovery_position_store() -> RecoveryPositionStore:

    return RecoveryPositionStore(
        RecoveryPositionStoreRuntime(
            get_read_connection=get_state_read_conn,
            get_write_connection=get_state_pg_conn,
            execute=_state_execute,
            normalize_position=_normalize_position_snapshot,
            normalize_row=_lifecycle_normalize_recovery_position_row,
            lookup_entry_decision_id=lookup_entry_decision_id,
            build_meta_update_payload=_lifecycle_build_recovery_meta_update_payload,
            build_closed_update_payload=_lifecycle_build_recovery_closed_update_payload,
            now=time.time,
            local_open_volumes=_live_service()._pos_open_api_volume,
            full_context=_live_service()._RECOVERY_CONTEXT_FULL,
            partial_context=_live_service()._RECOVERY_CONTEXT_PARTIAL,
        )
    )


def load_recovery_position_row(position_id: int) -> dict[str, Any]:
    return recovery_position_store().load(position_id)


def merge_recovery_position_meta(position_id: int, meta: dict[str, Any] | None) -> None:
    recovery_position_store().merge_meta(position_id, meta)


_recovery_zero_confirmations: dict[str, int] = {}


def _track_pending_close_ids(pending_add: Any, pending_remove: Any) -> None:
    """Track realized closes waiting for their authoritative deal.

    Estimates must never advance the session-risk boundary, but risk
    surfaces must see pending ids instead of silently operating on stale
    pnl.  Entries are added on deal-wait defer and released on projection.
    """
    try:
        add_ids: set[int] = set()
        if pending_add is not None:
            candidates = (
                pending_add
                if isinstance(pending_add, (list, tuple, set, frozenset))
                else [pending_add]
            )
            for item in candidates:
                try:
                    pid = int(item or 0)
                except (TypeError, ValueError):
                    continue
                if pid > 0:
                    add_ids.add(pid)
        drop_ids: set[int] = set()
        if pending_remove:
            for item in pending_remove:
                try:
                    pid = int(item or 0)
                except (TypeError, ValueError):
                    continue
                if pid > 0:
                    drop_ids.add(pid)
        with _LIVE_STATE_LOCK:
            current = [
                int(item)
                for item in list(_live_state.get("session_pending_close_ids") or [])
                if int(item or 0) > 0
            ]
            merged = [pid for pid in current if pid not in drop_ids]
            for pid in sorted(add_ids):
                if pid not in merged:
                    merged.append(pid)
            _live_state["session_pending_close_ids"] = sorted(merged)[-50:]
            _live_state["session_pending_close_observed_at"] = time.time()
    except Exception:
        return


def get_state_pg_conn():
    from backend.core.db import get_state_pg_conn

    return get_state_pg_conn()


def get_state_read_conn():
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


def runtime_kv_get(key: str, default=None):
    conn = get_state_read_conn()
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
    from backend.services.runtime_kv_store import set_on_conn

    set_on_conn(
        conn,
        key,
        value,
        updated_at=updated_at,
        ensure=False,
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


def runtime_kv_set(key: str, value) -> None:
    conn = get_state_pg_conn()
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


_POSITION_DECISION_INDEX_PATH = (
    Path(__file__).resolve().parents[2] / "run_artifacts" / "canonical_v2_position_decision_index.json"
)


_POSITION_DECISION_INDEX_CACHE: dict[str, dict[str, Any]] | None | bool = False


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


def lookup_entry_decision_id(position_id: int) -> str:
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


def lookup_open_decision_context(position_id: int) -> dict:
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
    conn = get_state_read_conn()
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


def ensure_open_ledger_for_recovered_close(
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
    existing = lookup_entry_decision_id(position_id)
    if existing:
        return existing
    if not _live_service()._LEDGER:
        return ""

    conn = get_state_read_conn()
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
        risk_state=live_state_get("risk", {}, clone=True) or {},
        real_pnl=real_pnl or {},
        close_reason=close_reason,
        fallback_strategy_name=_live_service()._current_loop_strategy_name(),
        context_integrity_default=_live_service()._RECOVERY_CONTEXT_PARTIAL,
        fallback_now_ts=time.time(),
    )

    try:
        decision_id = _live_service()._LEDGER.log_decision(**payloads["decision_payload"])
        _live_service()._LEDGER.log_position_event(
            decision_id=decision_id,
            **payloads["position_event_payload"],
        )
        recovery_state_payload = dict(payloads["recovery_state_payload"])
        recovery_state_payload["entry_decision_id"] = decision_id
        recovery_state_meta = dict(payloads["recovery_state_meta"])
        recovery_state_meta["open_repair_decision_id"] = decision_id
        upsert_recovery_position_state(
            recovery_state_payload,
            **payloads["recovery_state_kwargs"],
            meta=recovery_state_meta,
        )
        logger.info("[live] repaired missing open ledger before close pos={} decision={}", position_id, decision_id)
        return decision_id
    except Exception as exc:
        logger.debug("[live] open ledger repair before close failed for pos {}: {}", position_id, exc)
        return ""


def lookup_recovery_context_integrity(position_id: int, default: str | None = None) -> str:
    if default is None:
        default = _live_service()._RECOVERY_CONTEXT_PARTIAL
    return recovery_position_store().context_integrity(
        position_id,
        default=default,
    )


def persist_loop_desired_state(
    enabled: bool,
    *,
    broker: str = "ctrader",
    strategy_name: str = "factor_v4",
    reason: str = "manual",
) -> None:
    runtime_kv_set(
        _RUNTIME_KV_LOOP_DESIRED,
        {
            "enabled": bool(enabled),
            "broker": broker,
            "strategy_name": strategy_name,
            "reason": reason,
            "updated_at": time.time(),
        },
    )


def read_loop_desired_state() -> dict:
    state = runtime_kv_get(_RUNTIME_KV_LOOP_DESIRED, {}) or {}
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
        "source": str(live_state_get("session_state_source", "runtime_incremental") or "runtime_incremental"),
        "status": str(live_state_get("session_state_status", "unknown") or "unknown"),
        "session_pnl": float(live_state_get("session_pnl", 0.0) or 0.0),
        "session_trades": int(live_state_get("session_trades", 0) or 0),
        "session_winning": int(live_state_get("session_winning", 0) or 0),
        "session_losing": int(live_state_get("session_losing", 0) or 0),
        "session_trade_pnls": list(live_state_get("session_trade_pnls", [], clone=True) or [])[-200:],
        "session_realized_pnl_legs": list(
            live_state_get("session_realized_pnl_legs", [], clone=True) or []
        )[-500:],
        "session_realized_legs": int(
            live_state_get("session_realized_legs", 0) or 0
        ),
        "session_recorded_position_ids": list(
            live_state_get("session_recorded_position_ids", [], clone=True) or []
        )[-1000:],
        "session_consecutive_loss": int(live_state_get("session_consecutive_loss", 0) or 0),
        "session_max_drawdown_pct": float(live_state_get("session_max_drawdown_pct", 0.0) or 0.0),
        "session_peak_equity": float(live_state_get("session_peak_equity", 0.0) or 0.0),
        "session_start_balance": float(live_state_get("session_start_balance", 0.0) or 0.0),
        "session_last_trade_ts": float(live_state_get("session_last_trade_ts", 0.0) or 0.0),
        "session_observed_at": float(live_state_get("session_observed_at", 0.0) or 0.0),
        "circuit_breaker": bool(live_state_get("circuit_breaker", False)),
        "circuit_reason": str(live_state_get("circuit_reason", "") or ""),
        "session_circuit_observation": dict(
            live_state_get("session_circuit_observation", {}, clone=True) or {}
        ),
        "trade_equity_history": list(live_state_get("trade_equity_history", [], clone=True) or [])[-500:],
        "updated_at": time.time(),
    }


def _persist_session_state(trade_date: str | None = None) -> None:
    try:
        snapshot = _session_state_snapshot(trade_date)
        runtime_kv_set(_session_state_key(snapshot["trade_date"]), snapshot)
    except Exception as exc:
        logger.debug("[live] session state persist failed: {}", exc)


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
        connection_factory=get_state_read_conn,
        execute=_state_execute,
        warning=logger.warning,
    )


def _calendar_day_trade_summary(timezone_name: str = "Asia/Shanghai") -> dict:
    """Read-only operator-day view; never drives the UTC risk circuit."""
    tz = ZoneInfo(timezone_name)
    trade_date = datetime.now(tz).strftime("%Y-%m-%d")
    open_position_ids = fresh_cached_broker_open_position_ids()
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
    account = live_state_get("account", {}, clone=True) or {}
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


def restore_session_state_for_day(
    trade_date: str | None = None,
    *,
    broker_open_position_ids: set[int] | None = None,
    confirmed_closed_position_ids: set[int] | None = None,
) -> bool:
    if not trade_date:
        trade_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw_state = runtime_kv_get(_session_state_key(trade_date), {}) or {}
    authoritative_facts = _load_authoritative_session_deal_facts(
        trade_date,
        broker_open_position_ids=broker_open_position_ids,
        confirmed_closed_position_ids=confirmed_closed_position_ids,
    )
    account = live_state_get("account", {}, clone=True) or {}
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
            "[live] authoritative session projection unavailable for {}: {}",
            trade_date,
            decision["authoritative_error"],
        )
    if not decision.get("authoritative") and decision.get("restored"):
        logger.warning(
            "[live] restoring cached session projection for {} because broker close facts are unavailable",
            trade_date,
        )
    live_state_update(**dict(decision.get("state") or {}))
    if decision.get("authoritative"):
        _persist_session_state(trade_date)
        evaluate_daily_drawdown()
    return bool(decision.get("restored"))


def defer_close_until_authoritative_deal(
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
            _live_service()._pos_open_api_volume.get(pid, 0.0) or 0.0
        ),
        **dict(recovery_evidence or {}),
    }
    try:
        merge_recovery_position_meta(
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
    live_state_update(
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


def release_session_close_deal_latch(position_id: int, real_pnl: dict[str, Any]) -> None:
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


def release_orphaned_recovery_session_latches(
    position_ids: list[int] | set[int] | tuple[int, ...],
    *,
    broker: str,
    broker_position_ids: set[int],
    reconcile_id: str,
    observed_at: float,
) -> None:
    """Release close-deal latches for recovery rows proven to be orphaned.

    This is deliberately distinct from ``release_session_close_deal_latch``:
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
        live_state_get("session_risk_blockers", [], clone=True) or []
    )
    filtered_blockers = [
        blocker for blocker in current_blockers if str(blocker) not in stale_blockers
    ]
    if filtered_blockers != current_blockers:
        live_state_update(session_risk_blockers=filtered_blockers)


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
        # FIX 2026-09-01: filter synthetic/test positions (902/903) that have leaked into durable latch
        # Real cTrader positions are >100k; synthetic IDs <1000 must not block recovery bootstrap
        if position_id < 1000:
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
        "open_price": float(_live_service()._pos_open_prices.get(pid, 0.0) or 0.0),
        "volume": float(
            _live_service()._pos_open_api_volume.get(pid, 0.0)
            or evidence.get("expected_position_volume", 0.0)
            or 0.0
        ),
        "close_pnl": 0.0,
        "context_integrity": _live_service()._RECOVERY_CONTEXT_PARTIAL,
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


def capture_partial_close_deal_cursor(position_id: int) -> dict[str, Any]:
    """Capture the durable close-deal cursor immediately before broker RPC."""

    pid = int(position_id or 0)
    captured_at = time.time()
    try:
        from execution.deal_sync import find_close_deal

        conn = get_state_pg_conn()
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
        record_risk_reduction_aux_failure(
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


def sync_partial_close_session_fact(
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
        get_state_connection=get_state_pg_conn,
        fetch_deals_since_result=fetch_deals_since_result,
        store_deals=store_deals,
        find_close_deal=find_close_deal,
        authoritative_close_pnl=_authoritative_close_pnl,
        defer_close=defer_close_until_authoritative_deal,
        record_aux_failure=record_risk_reduction_aux_failure,
        release_close_latch=release_session_close_deal_latch,
        update_live_state=live_state_update,
        no_new_risk_latch_status=no_new_risk_latch_status,
        open_api_volumes=_live_service()._pos_open_api_volume,
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


def fresh_cached_broker_open_position_ids(
    *,
    now_ts: float | None = None,
    stale_after_sec: float = _LIVE_SAFETY_FRESHNESS_SEC,
) -> set[int] | None:
    """Return broker-open position IDs only from a fresh position fact.

    ``None`` means the open-position set is unknown.  Passing an unknown set
    into deals-first restore as an empty set could incorrectly classify a
    partially closed, still-open broker position as a completed trade.
    """

    observed_at = float(live_state_get("positions_updated_at", 0.0) or 0.0)
    reconcile_id = str(live_state_get("positions_reconcile_id", "") or "")
    checked_at = float(time.time() if now_ts is None else now_ts)
    if (
        observed_at <= 0.0
        or not reconcile_id
        or checked_at < observed_at
        or checked_at - observed_at > max(0.0, float(stale_after_sec))
    ):
        return None
    positions = live_state_get("positions_reconciled", [], clone=True)
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


def remember_close_reason(position_id: int, reason: str) -> None:
    try:
        _lifecycle_remember_close_reason(
            pending_reasons=_live_service()._pending_close_reasons,
            merge_recovery_meta=merge_recovery_position_meta,
            position_id=position_id,
            reason=reason,
        )
    except Exception as exc:
        # The lifecycle helper stores the process-local reason before PG
        # projection.  Preserve that broker-adjacent fact and defer the audit.
        _live_service()._pending_close_reasons[int(position_id)] = str(reason or "")
        record_risk_reduction_aux_failure(
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


def consume_close_reason(position_id: int, default: str = "broker_close") -> str:
    return _lifecycle_consume_close_reason(
        pending_reasons=_live_service()._pending_close_reasons,
        load_recovery_row=load_recovery_position_row,
        position_id=position_id,
        default=default,
    )


def remember_close_verdict(position_id: int, verdict) -> None:
    try:
        _lifecycle_remember_close_verdict(
            pending_verdicts=_live_service()._pending_close_verdicts,
            merge_recovery_meta=merge_recovery_position_meta,
            position_id=position_id,
            verdict=verdict,
        )
    except Exception as exc:
        try:
            from backend.services.live_position_lifecycle import serialize_close_verdict

            _live_service()._pending_close_verdicts[int(position_id)] = serialize_close_verdict(verdict)
        except Exception:
            pass
        record_risk_reduction_aux_failure(
            "close_verdict_projection_failed",
            position_id=position_id,
            action="close_position",
            error=exc,
        )


def consume_close_verdict(position_id: int, close_reason: str) -> dict:
    return _lifecycle_consume_close_verdict(
        pending_verdicts=_live_service()._pending_close_verdicts,
        load_recovery_row=load_recovery_position_row,
        build_close_context=_live_service()._build_close_position_risk_context,
        risk_evaluate=_live_service()._RISK_POLICY.evaluate,
        position_id=int(position_id),
        close_reason=close_reason,
    )


def _latest_supervisor_event_before_close(position_id: int, close_ts: float, lookback_sec: float = 3600.0) -> dict[str, Any]:
    conn = get_state_read_conn()
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
    conn = get_state_read_conn()
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


def classify_close_source(position_id: int, close_reason: str, close_ts: float) -> dict[str, Any]:
    ledger_latest = _latest_supervisor_event_before_close(position_id, close_ts)
    trace_latest = _latest_protection_trace_before_close(position_id, close_ts)
    latest = _lifecycle_latest_close_evidence(ledger_latest, trace_latest)
    return _lifecycle_classify_close_source_from_evidence(
        close_reason=close_reason,
        evidence=latest,
    )


def risk_state_with_verdict_dict(verdict: dict) -> dict:
    state = live_state_get("risk", {}, clone=True) or {}
    return _lifecycle_build_risk_state_with_policy_verdict(
        state,
        verdict,
        serialized=True,
    )


def _normalize_position_snapshot(raw: Any) -> dict:
    return _lifecycle_normalize_position_snapshot(raw)


def upsert_recovery_position_state(
    raw_position: Any,
    *,
    broker: str,
    strategy_name: str,
    status: str = "open",
    context_integrity: str | None = None,
    meta: dict | None = None,
) -> None:
    recovery_position_store().upsert(
        raw_position,
        broker=broker,
        strategy_name=strategy_name,
        status=status,
        context_integrity=context_integrity,
        meta=meta,
    )


def list_active_recovery_positions(broker: str) -> list[dict]:
    return recovery_position_store().list_active(broker)


def active_recovery_position_ids_for_close_detection(broker: str) -> set[int]:
    """Keep durable open rows in close detection even if memory lost the ID."""

    try:
        return _lifecycle_recovery_active_position_ids(
            list_active_recovery_positions(broker)
        )
    except Exception as exc:
        logger.debug(
            "[live] durable recovery IDs unavailable for close detection: {}",
            exc,
        )
        return set()


def _recovery_last_seen_by_position(position_ids: set[int]) -> dict[int, float]:
    """Return the last broker-open observation used to reject stale partial deals."""
    return recovery_position_store().last_seen_by_position(position_ids)


def _recovery_remaining_volume_by_position(
    position_ids: set[int],
) -> dict[int, float]:
    """Return the last fresh broker-open volume for close completeness proof."""
    return recovery_position_store().remaining_volume_by_position(position_ids)


def sync_closed_position_deals_for_tick(
    bridge: Any,
    closed_pids: set[int],
    *,
    observed_close_cursor_out: dict[int, dict[str, Any]],
) -> dict[int, dict]:
    """Fetch authoritative deals for positions that disappeared this tick.

    A broker position disappearing is a final-close observation, not a
    partial-close retry.  The cursor baseline therefore has to be an explicit
    empty cursor for every position.  Keeping this contract in one helper
    prevents the duplicate-bar and new-bar paths from drifting apart.
    """
    if not closed_pids or bridge is None:
        return {}

    from execution.deal_sync import sync_close_deals_batch

    conn = get_state_pg_conn()
    try:
        return sync_close_deals_batch(
            bridge,
            conn,
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
            observed_close_cursor_out=observed_close_cursor_out,
        )
    finally:
        conn.close()


def mark_recovery_position_closed(
    position_id: int,
    *,
    close_reason: str,
    close_pnl: float,
    closed_at: float,
    meta: dict | None = None,
) -> None:
    recovery_position_store().mark_closed(
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
            defer_close=defer_close_until_authoritative_deal,
            build_payloads=_lifecycle_build_replayed_close_payloads,
            mark_recovery_closed=mark_recovery_position_closed,
            release_close_latch=release_session_close_deal_latch,
            get_risk_state=lambda: (
                live_state_get("risk", {}, clone=True) or {}
            ),
            now=time.time,
            partial_context=_live_service()._RECOVERY_CONTEXT_PARTIAL,
            ledger=_live_service()._LEDGER,
            trade_reviewer=_live_service()._TRADE_REVIEWER,
            experience_builder=_live_service()._EXPERIENCE_BUILDER,
            policy_suggester=_live_service()._POLICY_SUGGESTER,
            attr_engine=(_live_service()._factor_pipeline or {}).get("attribution"),
            debug=logger.debug,
        ),
    )


def result_is_position_not_found(result: Any) -> bool:
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
    pid = int(position_id)
    # The caller has already completed and published a fresh broker
    # reconciliation.  Local cleanup may trim the advisory event projection,
    # but must never advance the authoritative reconcile timestamp.
    positions = live_state_get("positions_event", [], clone=True) or []
    payload = _lifecycle_filter_removed_live_position(positions, position_id=pid)
    if payload["removed"]:
        live_state_update(
            positions_event=payload["positions"],
            positions_event_reason="local_closed_position_cleanup",
        )
    _live_service()._prev_position_ids.discard(pid)
    _live_service()._pos_open_prices.pop(pid, None)
    _live_service()._pos_open_api_volume.pop(pid, None)
    _lifecycle_forget_pending_close_state(
        pending_reasons=_live_service()._pending_close_reasons,
        pending_verdicts=_live_service()._pending_close_verdicts,
        position_id=pid,
    )


def retire_broker_missing_position(
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
            load_recovery_position=load_recovery_position_row,
            open_prices=_live_service()._pos_open_prices,
            get_state_connection=get_state_pg_conn,
            sync_close_deals_batch=sync_close_deals_batch,
            authoritative_close_pnl=_authoritative_close_pnl,
            defer_close=defer_close_until_authoritative_deal,
            replay_close=_replay_recovered_close,
            mark_recovery_closed=mark_recovery_position_closed,
            remove_live_position_state=_remove_live_position_state,
            now=time.time,
            replay_lookback_seconds=_RECOVERY_REPLAY_LOOKBACK_SEC,
            partial_context=_live_service()._RECOVERY_CONTEXT_PARTIAL,
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
        _live_service()._publish_fresh_position_reconcile(
            result,
            broker="ctrader",
            persist=persist,
        )
    )


def bootstrap_position_recovery(
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
        list_active_positions=list_active_recovery_positions,
        pending_session_close_causes=_pending_session_close_causes,
        pending_close_fallback_state=_pending_close_fallback_state,
        pending_close_requirements=_pending_close_requirements,
        get_state_connection=get_state_pg_conn,
        sync_close_deals_batch=sync_close_deals_batch,
        pending_close_cursor_overrides=_pending_close_cursor_overrides,
        pending_close_result_complete=_pending_close_result_complete,
        release_session_close_latch=release_session_close_deal_latch,
        defer_close=defer_close_until_authoritative_deal,
        previous_position_ids=_live_service()._prev_position_ids,
        zero_confirmations=_recovery_zero_confirmations,
        zero_confirmations_required=_RECOVERY_ZERO_CONFIRMATIONS_REQUIRED,
        replay_lookback_seconds=_RECOVERY_REPLAY_LOOKBACK_SEC,
        recovery_replay_lookback_from=_lifecycle_recovery_replay_lookback_from,
        pending_close_required_volume_delta=(
            _pending_close_required_volume_delta
        ),
        replay_recovered_close=_replay_recovered_close,
        recovery_missing_position_ids=_lifecycle_recovery_missing_position_ids,
        open_prices=_live_service()._pos_open_prices,
        open_api_volumes=_live_service()._pos_open_api_volume,
        upsert_recovery_position=upsert_recovery_position_state,
        now=time.time,
    )
    return _runtime_bootstrap_position_recovery(
        bridge,
        broker=broker,
        strategy_name=strategy_name,
        log=log,
        runtime=runtime,
    )


def _repair_session_start_balance_from_account(*, persist: bool = True) -> float:
    """Fill a startup-time zero baseline once broker balance becomes available."""
    existing = float(live_state_get("session_start_balance", 0.0) or 0.0)
    if existing > 0:
        return existing
    account = live_state_get("account", {}, clone=True) or {}
    current_balance = float(account.get("balance", 0.0) or 0.0)
    if current_balance <= 0:
        return 0.0
    session_pnl = float(live_state_get("session_pnl", 0.0) or 0.0)
    reconstructed = current_balance - session_pnl
    if reconstructed <= 0:
        return 0.0
    live_state_update(session_start_balance=reconstructed)
    if persist:
        _persist_session_state()
    return reconstructed



def evaluate_daily_drawdown(risk_limits: RiskLimitSnapshot | None = None) -> dict:
    limits = risk_limits or RiskLimitSnapshot.from_runtime_config()
    session_pnl = float(live_state_get("session_pnl", 0.0) or 0.0)
    consecutive_loss = int(
        live_state_get("session_consecutive_loss", 0) or 0
    )
    start_balance = float(live_state_get("session_start_balance", 0.0) or 0.0)
    if start_balance <= 0:
        return {
            "tripped": False,
            "dd_pct": 0.0,
            "reason": "",
            "session_pnl": session_pnl,
            "start_balance": 0.0,
            "risk_limits": limits.to_dict(),
        }
    # 回撤只统计亏损方向 — 盈利日不得把 abs(PnL) 写成回撤水位。
    dd_pct = -min(session_pnl, 0.0) / start_balance * 100 if start_balance > 0 else 0.0
    prev_dd = float(live_state_get("session_max_drawdown_pct", 0.0) or 0.0)
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
        _live_service()._maybe_update_loss_streak_book(tripped=True, reason=reason)
    elif not enforced:
        updates["circuit_breaker"] = False
        updates["circuit_reason"] = ""
        _live_service()._maybe_update_loss_streak_book(tripped=False)
    live_state_update(**updates)
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


def consume_pending_close_kwargs(kwargs: dict) -> dict:
    """Store pre-update hook: apply pending-close bookkeeping kwargs."""
    pending_add = kwargs.pop('session_pending_close_add', None)
    pending_remove = kwargs.pop('session_pending_close_remove', None)
    if pending_add is not None or pending_remove:
        _track_pending_close_ids(pending_add, pending_remove)
    return kwargs
