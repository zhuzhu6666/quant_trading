from __future__ import annotations

import json
import hashlib
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services.failure_taxonomy import build_failure_taxonomy
from backend.services.position_metrics import normalize_path_state, update_position_path_metrics
from backend.services.review_contract import (
    build_entry_timing_context,
    build_execution_quality_evidence,
    extract_decision_freshness_context,
    normalize_trade_review_contract,
    trusted_broker_close_price,
)


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def _loads(raw: object, default: object) -> object:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _dict_path(payload: object, *path: str) -> dict:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _first_float(*values: object, default: float = 0.0) -> float:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return float(default)


def _optional_float(*values: object) -> float | None:
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _event_ts(order_events: list[dict], event_type: str) -> float:
    for row in order_events:
        if str(row.get("event_type") or "") == event_type:
            return _safe_float(row.get("event_ts"))
    return 0.0


def _review_consistency(entry_action: dict, order_events: list[dict]) -> dict:
    """Expose cross-trace mismatches without converting them into causal labels."""

    action = entry_action if isinstance(entry_action, dict) else {}
    execution = action.get("execution_context") if isinstance(action.get("execution_context"), dict) else {}
    sizing = action.get("sizing_trace") if isinstance(action.get("sizing_trace"), dict) else {}
    event_context = action.get("event_context") if isinstance(action.get("event_context"), dict) else {}
    decision_quality = action.get("decision_quality_context") if isinstance(action.get("decision_quality_context"), dict) else {}
    context_state = decision_quality.get("context_state") if isinstance(decision_quality.get("context_state"), dict) else {}

    actual_volume = _optional_float(
        execution.get("actual_api_volume"), action.get("volume"), action.get("actual_api_volume"),
    )
    final_sizing_volume = _optional_float(
        sizing.get("final_api_volume"), sizing.get("event_adjusted_api_volume"),
    )
    filled_volumes = []
    for raw_event in order_events or []:
        event = dict(raw_event) if not isinstance(raw_event, dict) else raw_event
        if str(event.get("event_type") or "") != "filled":
            continue
        volume = _optional_float(event.get("volume"))
        if volume is not None and volume > 0:
            filled_volumes.append(volume)
    filled_volume = max(filled_volumes) if filled_volumes else None

    def _volume_check(expected: float | None, observed: float | None) -> dict:
        if expected is None or observed is None:
            return {"status": "missing", "expected": expected, "observed": observed}
        tolerance = max(1e-6, abs(expected) * 1e-6)
        return {
            "status": "consistent" if abs(expected - observed) <= tolerance else "mismatch",
            "expected": expected,
            "observed": observed,
        }

    event_near = event_context.get("event_near") if "event_near" in event_context else None
    event_window_state = context_state.get("event_window_state")
    event_scope_status = "missing"
    if event_near is not None and event_window_state:
        expected_near = str(event_window_state).lower() in {"near", "active"}
        event_scope_status = "consistent" if bool(event_near) == expected_near else "different_scopes"

    sizing_check = _volume_check(final_sizing_volume, actual_volume)
    fill_check = _volume_check(actual_volume, filled_volume)
    hard_mismatch = sizing_check["status"] == "mismatch" or fill_check["status"] == "mismatch"
    return {
        "schema_version": "review_summary_consistency.v1",
        "overall": "mismatch" if hard_mismatch else ("ambiguous" if event_scope_status == "different_scopes" else "consistent"),
        "causal_level": "observational",
        "checks": {
            "sizing_trace_matches_execution": sizing_check,
            "execution_matches_filled_order": fill_check,
            "event_context_vs_factor_context": {
                "status": event_scope_status,
                "event_near": event_near,
                "context_event_window_state": event_window_state,
                "interpretation": "different_scopes_require_review" if event_scope_status == "different_scopes" else "",
            },
        },
    }


def _review_summary(
    *,
    position_id: str,
    pnl: float,
    outcome_label: str,
    primary_responsibility: str,
    system_labels: list[str],
    top_factor: str,
    top_weight_factor: str,
    worst_factor: str,
) -> str:
    parts = [
        f"trade {position_id} closed pnl={pnl:.2f}",
        f"outcome={outcome_label}",
    ]
    if primary_responsibility:
        parts.append(f"primary_responsibility={primary_responsibility}")
    if system_labels:
        parts.append(f"system_issue={','.join(system_labels[:4])}")
    largest_contribution_factor = top_factor or top_weight_factor
    if largest_contribution_factor:
        parts.append(f"largest_contribution_factor={largest_contribution_factor}")
    if worst_factor:
        parts.append(f"worst_factor={worst_factor}")
    return "; ".join(parts)


class TradeReviewer:
    """Rule-based post-trade reviewer."""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _execute(self, conn, sql: str, params: tuple | list | None = None):
        if params is None:
            return conn.execute(self._sql(sql))
        return conn.execute(self._sql(sql), tuple(params))

    @contextmanager
    def _conn(self):
        conn = get_state_pg_conn() if self._use_pg() else connect_sqlite(self.db_path)
        if not self._use_pg():
            conn.row_factory = sqlite3.Row
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            if not self._use_pg():
                conn.executescript(STATE_DB_DDL)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def is_review_verifiable(
        *,
        real_pnl: dict | None = None,
        close_reason: str = "",
        context_integrity: str = "full",
    ) -> tuple[bool, str]:
        payload = real_pnl or {}
        has_net = isinstance(payload, dict) and payload.get("net") is not None
        broker_close = close_reason in {
            "broker_close",
            "restart_replay",
            "emergency_close",
        }
        if (
            broker_close or "price_quality" in payload
        ) and trusted_broker_close_price(payload) is None:
            return False, "unknown_execution_price"
        if has_net:
            return True, ""
        if context_integrity != "full":
            return False, "partial_context"
        return False, "missing_real_pnl"

    def review_closed_trade(
        self,
        *,
        position_id: str,
        pnl: float,
        close_price: float,
        close_ts: float,
        contributions: dict[str, float] | None = None,
        exit_decision_id: str = "",
        real_pnl: dict | None = None,
        close_reason: str = "",
        context_integrity: str = "full",
        attribution_integrity: str = "",
        close_reason_source: str = "",
        inferred_close_supervisor: dict | None = None,
    ) -> dict:
        contributions = contributions or {}
        attribution_integrity = str(attribution_integrity or ("full" if contributions else "missing"))
        inferred_close_supervisor = inferred_close_supervisor or {}
        is_verifiable, skip_reason = self.is_review_verifiable(
            real_pnl=real_pnl,
            close_reason=close_reason,
            context_integrity=context_integrity,
        )
        if not is_verifiable:
            return {
                "accepted": False,
                "skip_reason": skip_reason,
                "position_id": position_id,
                "trade_id": str(position_id),
                "outcome_label": "",
                "pnl": float(pnl),
                "failure_tags": ["unverified_close"],
                "summary_text": f"trade {position_id} skipped review: {skip_reason}",
                "review_json": {
                    "position_id": position_id,
                    "real_pnl": real_pnl or {},
                    "close_reason": close_reason,
                    "close_reason_source": close_reason_source,
                    "inferred_close_supervisor": inferred_close_supervisor,
                    "context_integrity": context_integrity,
                    "attribution_integrity": attribution_integrity,
                },
            }
        with self._conn() as conn:
            entry = self._execute(conn,
                """
                SELECT * FROM decision_ledger
                WHERE position_id=? AND event_type='open'
                ORDER BY decision_ts DESC LIMIT 1
                """,
                (position_id,),
            ).fetchone()
            entry_decision_id = str(entry["decision_id"]) if entry else ""
            trade_id = str(entry["trade_id"]) if entry and entry["trade_id"] else str(position_id)
            entry_score = (
                float(entry["action_score"])
                if entry and entry["action_score"] is not None
                else None
            )
            regime_id = str(entry["regime_id"] or "") if entry else ""
            entry_decision_ts = float(entry["decision_ts"] or 0.0) if entry else 0.0
            timeframe = str(entry["timeframe"] or "") if entry else ""
            entry_action = _loads(entry["action_json"], {}) if entry else {}
            entry_risk_state = _loads(entry["risk_state_json"], {}) if entry else {}
            entry_factors = list(
                self._execute(conn,
                    """
                    SELECT * FROM decision_factor_snapshot
                    WHERE decision_id=?
                    ORDER BY ABS(contribution_score) DESC, factor ASC
                    """,
                    (entry_decision_id,),
                )
            ) if entry_decision_id else []
            recovery = self._execute(conn,
                """
                SELECT recovery_meta_json
                FROM recovery_position_state
                WHERE position_id=?
                LIMIT 1
                """,
                (position_id,),
            ).fetchone()
            order_events = list(
                self._execute(conn,
                    """
                    SELECT event_type, event_ts, price, volume, status, details_json
                    FROM order_lifecycle_event
                    WHERE (? <> '' AND decision_id=?)
                       OR (? <> '' AND trade_id=?)
                    ORDER BY event_ts ASC
                    """,
                    (entry_decision_id, entry_decision_id, trade_id, trade_id),
                )
            ) if entry_decision_id or trade_id else []
            broker_entry = self._execute(
                conn,
                """
                SELECT deal_id, exec_price, raw_execution_price, price_quality,
                       exec_timestamp, entry_price, trade_side
                FROM ctrader_deals
                WHERE position_id=? AND is_close=0
                ORDER BY exec_timestamp ASC
                LIMIT 1
                """,
                (position_id,),
            ).fetchone()

        top_weight_factor = ""
        top_weight = 0.0
        if entry_factors:
            best = max(entry_factors, key=lambda r: abs(float(r["policy_weight"] or 0.0)))
            top_weight_factor = str(best["factor"])
            top_weight = float(best["policy_weight"] or 0.0)

        decision_quality_context = (
            (entry_action or {}).get("decision_quality_context")
            if isinstance(entry_action, dict)
            else {}
        ) or {}
        factor_roles = dict(
            decision_quality_context.get("factor_roles")
            or ((entry_action or {}).get("factor_roles") if isinstance(entry_action, dict) else {})
            or {}
        )
        alpha_contributions = {
            name: value
            for name, value in contributions.items()
            if str(factor_roles.get(name) or "alpha") == "alpha"
        }
        admission_contributions = {
            name: value
            for name, value in contributions.items()
            if str(factor_roles.get(name) or "alpha") == "gate"
        }
        context_contributions = {
            name: value
            for name, value in contributions.items()
            if str(factor_roles.get(name) or "alpha") in {"context", "sizing"}
        }
        worst_factor = ""
        worst_mc = 0.0
        if alpha_contributions:
            worst_factor, worst_mc = min(alpha_contributions.items(), key=lambda kv: kv[1])

        pos_mc = sum(v for v in contributions.values() if v > 0)
        neg_mc = sum(v for v in contributions.values() if v < 0)
        total_abs_mc = sum(abs(v) for v in contributions.values()) or 1.0
        positive_share = pos_mc / total_abs_mc

        failure_tags: list[str] = []
        # Profit branches never enter the loss-only avoidability analysis,
        # but the orthogonal outcome contract still requires a deterministic
        # value for this field.
        avoidable_entry = False
        if pnl > 0:
            outcome_label = "good_win" if positive_share >= 0.55 else "lucky_win"
            if outcome_label == "lucky_win":
                failure_tags.append("lucky_win")
        else:
            conviction = abs(float(entry_score or 0.0))
            has_entry_context = entry is not None
            has_attribution = bool(contributions)
            decision_quality_context = (
                (entry_action or {}).get("decision_quality_context")
                if isinstance(entry_action, dict)
                else {}
            ) or {}
            factor_conflict_ratio = _safe_float(decision_quality_context.get("factor_conflict_ratio"))
            effective_alpha_factor_count = _safe_int(
                decision_quality_context.get("effective_alpha_factor_count")
                or decision_quality_context.get("n_active_alpha_factors")
            )
            conflict = (
                has_attribution
                and pos_mc > 0
                and neg_mc < 0
                and factor_conflict_ratio >= 0.4
                and effective_alpha_factor_count >= 3
            )
            weak_entry = has_entry_context and conviction < 0.55
            avoidable_entry = weak_entry and (conflict or (has_attribution and positive_share < 0.45))
            outcome_label = "bad_loss" if conviction >= 0.55 or avoidable_entry else "good_loss"
            if outcome_label == "bad_loss":
                failure_tags.append("bad_loss")
            else:
                failure_tags.append("good_loss")
                failure_tags.append("clean_good_loss" if not conflict else "conflict_entry_loss")
            if weak_entry:
                failure_tags.append("weak_entry_loss")
            if avoidable_entry:
                failure_tags.append("avoidable_loss")
            if conflict:
                failure_tags.append("factor_conflict")
                if "conflict_entry_loss" not in failure_tags:
                    failure_tags.append("conflict_entry_loss")
            if worst_factor:
                if worst_factor == top_weight_factor and abs(top_weight) >= 0.05:
                    failure_tags.append("overweight_noise_factor")
                elif conviction >= 0.70:
                    failure_tags.append("regime_mismatch")

        if not failure_tags and pnl <= 0:
            failure_tags.append("unavoidable_noise")
        if attribution_integrity == "missing" and "attribution_missing" not in failure_tags:
            failure_tags.append("attribution_missing")
        entry_cluster = entry_action.get("entry_cluster") if isinstance(entry_action, dict) else {}
        same_direction_open_count = _safe_int(
            (entry_action or {}).get("same_direction_open_count")
            if isinstance(entry_action, dict)
            else 0
        )
        recent_same_direction_entries = (
            (entry_action or {}).get("recent_same_direction_entries")
            if isinstance(entry_action, dict)
            else {}
        ) or {}
        event_context = (
            (entry_action or {}).get("event_context")
            or (entry_action or {}).get("event_sizing")
            if isinstance(entry_action, dict)
            else {}
        ) or {}
        market_micro_context = (
            (entry_action or {}).get("market_micro_context")
            if isinstance(entry_action, dict)
            else {}
        ) or {}
        execution_context = (
            (entry_action or {}).get("execution_context")
            if isinstance(entry_action, dict)
            else {}
        ) or {}
        data_quality_context = (
            (entry_action or {}).get("data_quality_context")
            if isinstance(entry_action, dict)
            else {}
        ) or {}
        adverse_slippage = _safe_float((market_micro_context or {}).get("adverse_slippage_points"))
        if pnl <= 0 and same_direction_open_count >= 2 and "entry_cluster_risk" not in failure_tags:
            failure_tags.append("entry_cluster_risk")
        if pnl <= 0 and bool((event_context or {}).get("event_near")) and "event_window_bad_entry" not in failure_tags:
            failure_tags.append("event_window_bad_entry")
        if pnl <= 0 and adverse_slippage > 0 and "execution_slippage" not in failure_tags:
            failure_tags.append("execution_slippage")
        if pnl <= 0 and data_quality_context and not bool(data_quality_context.get("quote_fresh", True)):
            if "data_quality_issue" not in failure_tags:
                failure_tags.append("data_quality_issue")

        # Provisional values are replaced after the complete position path is
        # available.  Do not infer review quality from final PnL alone.
        entry_quality = 0.5
        hold_quality = 0.5
        exit_quality = 0.5
        regime_fit_score = 0.5
        execution_quality_evidence = build_execution_quality_evidence(
            order_events=[dict(row) for row in order_events],
            entry_action=entry_action if isinstance(entry_action, dict) else {},
            broker_deal=dict(broker_entry) if broker_entry else {},
            direction=(entry_action or {}).get("direction") if isinstance(entry_action, dict) else 0,
        )
        execution_quality = _safe_float(execution_quality_evidence.get("score"))
        close_ts = float(close_ts or time.time())
        risk_verdict = (
            entry_action.get("risk_verdict")
            if isinstance(entry_action, dict)
            else {}
        ) or {}
        entry_temporal = _dict_path(risk_verdict, "audit_payload", "temporal_context")
        decision_freshness_context = extract_decision_freshness_context(
            entry_action=entry_action if isinstance(entry_action, dict) else {},
            entry_risk_state=entry_risk_state if isinstance(entry_risk_state, dict) else {},
        )
        submitted_at = _event_ts([dict(row) for row in order_events], "submitted")
        fill_ts = _event_ts([dict(row) for row in order_events], "filled")
        entry_timing_context = build_entry_timing_context(
            signal_bar_ts=entry_decision_ts,
            decision_evaluated_at=_first_float(entry_temporal.get("evaluated_at"), entry_decision_ts),
            order_submitted_at=submitted_at,
            fill_ts=fill_ts,
            close_ts=close_ts,
            timeframe=timeframe or entry_temporal.get("timeframe") or "",
            source="trade_reviewer",
        )
        entry_ts = _safe_float(entry_timing_context.get("actual_entry_ts"), entry_decision_ts)
        holding_seconds = max(0.0, close_ts - entry_ts) if entry_ts > 0 else 0.0
        recovery_meta = {}
        if recovery and recovery["recovery_meta_json"]:
            try:
                recovery_meta = json.loads(recovery["recovery_meta_json"])
            except Exception:
                recovery_meta = {}
        path_state = normalize_path_state((recovery_meta or {}).get("position_path"))
        current_regime = str((recovery_meta or {}).get("current_regime") or "")
        next_state, path_metrics = update_position_path_metrics(
            previous_state=path_state,
            current_pnl=float(pnl),
            now_ts=close_ts,
            holding_seconds=holding_seconds,
            max_holding_seconds=0.0,
            entry_regime=str((recovery_meta or {}).get("entry_regime") or ""),
            current_regime=current_regime,
        )
        mae = float(path_metrics["mae"])
        mfe = float(path_metrics["mfe"])
        hold_quality = _clamp(0.30 + path_metrics["holding_efficiency"] * 0.7)

        capture = float(path_metrics["profit_capture_ratio"] or 0.0)
        giveback = float(path_metrics["giveback_ratio"] or 0.0)
        time_in_profit_ratio = float(path_metrics["time_in_profit_ratio"] or 0.0)
        meaningful_mfe = mfe >= max(0.25, mae * 0.35)
        clean_direction = meaningful_mfe and (mae <= max(0.25, mfe * 0.35))
        direction_failed = mfe <= max(0.15, mae * 0.20) and mae > 0.0
        capture_failed = meaningful_mfe and (capture < 0.35 or giveback >= 0.65)

        # Reclassify the outcome from path evidence.  A clean profitable path
        # is a good win even when attribution factors disagree; conversely a
        # trade that first worked and then gave everything back is primarily
        # an exit/holding failure rather than proof that entry alpha was bad.
        if pnl > 0:
            outcome_label = "good_win" if clean_direction and capture >= 0.50 else "lucky_win"
            if outcome_label == "good_win" and "lucky_win" in failure_tags:
                failure_tags.remove("lucky_win")
            elif outcome_label == "lucky_win" and "lucky_win" not in failure_tags:
                failure_tags.append("lucky_win")
        if capture_failed:
            for label in ("profit_giveback", "alpha_correct_but_capture_failed"):
                if label not in failure_tags:
                    failure_tags.append(label)

        conviction = min(abs(float(entry_score or 0.0)), 1.0)
        if clean_direction:
            entry_quality = _clamp(0.62 + 0.18 * conviction)
        elif direction_failed:
            entry_quality = _clamp(0.42 - 0.18 * conviction)
        else:
            entry_quality = _clamp(0.48 + 0.12 * time_in_profit_ratio)
        exit_quality = _clamp(0.25 + 0.70 * capture)
        if mfe <= 0.0 and pnl <= 0.0:
            exit_quality = max(exit_quality, 0.50)  # little profit existed to protect
        conflict_present = "factor_conflict" in failure_tags or "conflicting_factor_entry" in failure_tags
        regime_fit_score = _clamp(
            0.72
            if clean_direction
            else (0.32 if direction_failed or "regime_mismatch" in failure_tags else 0.52)
        )
        if conflict_present:
            regime_fit_score = min(regime_fit_score, 0.45)

        top_factor = ""
        top_factor_mc = 0.0
        if alpha_contributions:
            top_factor, top_factor_mc = max(alpha_contributions.items(), key=lambda kv: abs(kv[1]))

        factor_names = sorted(str(name) for name in contributions)
        factor_generation = "runtime_bounded_v1" if len(factor_names) <= 64 else "legacy_unbounded"
        factor_training_lineage = {
            "schema_version": "factor_training_lineage.v1",
            "generation": factor_generation,
            "composer_version": str(decision_quality_context.get("composer_version") or ""),
            "decision_quality_schema": str(decision_quality_context.get("schema_version") or ""),
            "factor_count": len(factor_names),
            "factor_universe_sha256": hashlib.sha256(
                json.dumps(factor_names, ensure_ascii=False).encode("utf-8")
            ).hexdigest(),
        }

        review_json = {
            "contract_version": "phase_d.v1",
            "position_id": position_id,
            "trade_id": trade_id,
            "entry_decision_id": entry_decision_id,
            "exit_decision_id": exit_decision_id,
            "entry_ts": entry_ts,
            "entry_decision_ts": entry_decision_ts,
            "signal_bar_ts": entry_decision_ts,
            "close_ts": close_ts,
            "holding_seconds": round(holding_seconds, 3),
            "holding_minutes": round(holding_seconds / 60.0, 3),
            "timeframe": timeframe,
            "regime_id": regime_id,
            "entry_regime": regime_id,
            "entry_timing_context": entry_timing_context,
            "decision_freshness_context": decision_freshness_context,
            "mfe": round(mfe, 6),
            "mae": round(mae, 6),
            "giveback_ratio": path_metrics["giveback_ratio"],
            "profit_capture_ratio": path_metrics["profit_capture_ratio"],
            "time_in_profit": path_metrics["time_in_profit_seconds"],
            "time_in_profit_seconds": path_metrics["time_in_profit_seconds"],
            "time_in_profit_ratio": path_metrics["time_in_profit_ratio"],
            "holding_efficiency": path_metrics["holding_efficiency"],
            "time_decay_score": path_metrics["time_decay_score"],
            "thesis_status": path_metrics["thesis_status"],
            "thesis_status_at_exit": path_metrics["thesis_status"],
            "regime_shift": path_metrics["regime_shift"],
            "regime_shift_at_exit": path_metrics["regime_shift"],
            "entry_score": entry_score,
            "signal_score": entry_score,
            "action_score": entry_score,
            "entry_action": entry_action if isinstance(entry_action, dict) else {},
            "factor_training_lineage": factor_training_lineage,
            "entry_risk_state": entry_risk_state if isinstance(entry_risk_state, dict) else {},
            "direction": _safe_int((entry_action or {}).get("direction") if isinstance(entry_action, dict) else 0),
            "same_direction_open_count": same_direction_open_count,
            "recent_same_direction_entries": recent_same_direction_entries,
            "entry_cluster": entry_cluster or {},
            "portfolio_exposure": (entry_action or {}).get("portfolio_exposure", {}) if isinstance(entry_action, dict) else {},
            "market_micro_context": market_micro_context or {},
            "spread": _safe_float((entry_action or {}).get("spread") if isinstance(entry_action, dict) else 0.0),
            "bar_context": (entry_action or {}).get("bar_context", {}) if isinstance(entry_action, dict) else {},
            "event_context": event_context or {},
            "execution_context": execution_context or {},
            "execution_quality_evidence": execution_quality_evidence,
            "execution_quality_state": str(execution_quality_evidence.get("evidence_state") or "unknown"),
            "summary_consistency": _review_consistency(
                entry_action if isinstance(entry_action, dict) else {},
                [dict(row) for row in order_events],
            ),
            "data_quality_context": data_quality_context or {},
            "market_session": (entry_action or {}).get("market_session", {}) if isinstance(entry_action, dict) else {},
            "decision_quality_context": (entry_action or {}).get("decision_quality_context", {}) if isinstance(entry_action, dict) else {},
            "top_weight_factor": top_weight_factor,
            "top_weight": top_weight,
            "top_factor": top_factor,
            "top_factor_mc": top_factor_mc,
            "largest_contribution_factor": top_factor,
            "factor_attribution": {
                "schema_version": "factor_attribution.v1",
                "largest_contribution_factor": top_factor,
                "largest_contribution_score": top_factor_mc,
                "causal_level": "observational",
                "causal_claim": False,
            },
            "worst_factor": worst_factor,
            "worst_factor_mc": worst_mc,
            "responsibility_domains": {
                "entry_signal": {
                    "eligible_factors": sorted(alpha_contributions),
                    "worst_factor": worst_factor,
                    "worst_contribution": worst_mc,
                },
                "entry_admission": {
                    "eligible_factors": sorted(admission_contributions),
                    "worst_factor": min(admission_contributions, key=admission_contributions.get) if admission_contributions else "",
                },
                "position_management": {
                    "close_reason_source": close_reason_source,
                    "supervisor": inferred_close_supervisor,
                },
                "execution": {
                    "execution_quality": round(execution_quality, 4),
                    "evidence_state": str(execution_quality_evidence.get("evidence_state") or "unknown"),
                    "issues": list(execution_quality_evidence.get("issues") or []),
                },
                "context": {
                    "eligible_factors": sorted(context_contributions),
                },
            },
            "outcome_dimensions": {
                "schema_version": "trade_outcome_dimensions.v1",
                "financial_result": "profit" if pnl > 0 else "loss" if pnl < 0 else "flat",
                "entry_avoidability": "avoidable" if avoidable_entry else "not_established",
                "exit_control_quality": (
                    "controlled" if exit_quality >= 0.65
                    else "weak" if exit_quality < 0.40
                    else "mixed"
                ),
                "counterfactual_result": "pending_post_close_maturity",
                "legacy_outcome_label": outcome_label,
            },
            "positive_share": round(positive_share, 4),
            "close_price": close_price,
            "real_pnl": real_pnl or {},
            "close_reason": close_reason,
            "close_reason_source": close_reason_source,
            "inferred_close_supervisor": inferred_close_supervisor,
            "context_integrity": context_integrity,
            "attribution_integrity": attribution_integrity,
            "failure_tags": failure_tags,
            "factor_contributions": contributions,
            "position_path_state": next_state,
            "entry_quality": round(entry_quality, 4),
            "hold_quality": round(hold_quality, 4),
            "exit_quality": round(exit_quality, 4),
            "regime_fit_score": round(regime_fit_score, 4),
            "regime_fit": round(regime_fit_score, 4),
            "execution_quality": round(execution_quality, 4),
        }
        review_json = normalize_trade_review_contract(
            review_json,
            entry_quality=entry_quality,
            hold_quality=hold_quality,
            exit_quality=exit_quality,
            regime_fit_score=regime_fit_score,
            execution_quality=execution_quality,
        )
        taxonomy = build_failure_taxonomy({**review_json, "pnl": pnl})
        review_json["failure_taxonomy"] = taxonomy
        review_json["primary_responsibility"] = taxonomy["primary_responsibility"]
        review_json["responsibility_labels"] = taxonomy["responsibility_labels"]
        for label in taxonomy["responsibility_labels"]:
            if label not in failure_tags:
                failure_tags.append(label)
        system_issue = review_json.get("system_issue_context") or {}
        summary = _review_summary(
            position_id=position_id,
            pnl=float(pnl),
            outcome_label=outcome_label,
            primary_responsibility=str(taxonomy.get("primary_responsibility") or ""),
            system_labels=list(system_issue.get("labels") or []),
            top_factor=top_factor,
            top_weight_factor=top_weight_factor,
            worst_factor=worst_factor,
        )

        review_id = self._new_id("review")
        with self._conn() as conn:
            existing = self._find_existing_review(
                conn,
                position_id=position_id,
                real_pnl=real_pnl or {},
                close_ts=close_ts,
            )
            if existing:
                existing_review = dict(existing["review_json"])
                existing_review["deduplicated"] = True
                return {
                    "accepted": True,
                    "review_id": existing["review_id"],
                    "trade_id": existing["trade_id"],
                    "position_id": position_id,
                    "regime_id": regime_id,
                    "outcome_label": existing["outcome_label"],
                    "pnl": float(existing["pnl"]),
                    "failure_tags": existing["failure_tags"],
                    "summary_text": existing["summary_text"],
                    "review_json": existing_review,
                    "deduplicated": True,
                }
            self._execute(conn,
                """
                INSERT INTO trade_outcome_review
                (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
                 entry_quality, hold_quality, exit_quality, regime_fit_score,
                 execution_quality, pnl, mae, mfe, outcome_label,
                 failure_tags_json, summary_text, review_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    trade_id,
                    position_id,
                    entry_decision_id,
                    exit_decision_id,
                    round(entry_quality, 4),
                    round(hold_quality, 4),
                    round(exit_quality, 4),
                    round(regime_fit_score, 4),
                    round(execution_quality, 4),
                    round(float(pnl), 6),
                    round(mae, 6),
                    round(mfe, 6),
                    outcome_label,
                    json.dumps(failure_tags, ensure_ascii=False),
                    summary,
                    json.dumps(review_json, ensure_ascii=False, default=str),
                    close_ts,
                ),
            )
            for factor, mc in contributions.items():
                entry_contribution = 0.0
                for row in entry_factors:
                    if str(row["factor"]) == factor:
                        entry_contribution = float(row["contribution_score"] or 0.0)
                        break
                self._execute(conn,
                    """
                    INSERT INTO factor_contribution_review
                    (review_id, trade_id, factor, entry_contribution, hold_contribution,
                     exit_contribution, net_contribution, confidence, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        trade_id,
                        factor,
                        round(entry_contribution, 6),
                        0.0,
                        0.0,
                        round(float(mc), 6),
                        round(
                            _clamp(abs(mc) / max(abs(pnl), 1.0))
                            * (0.2 if bool((system_issue or {}).get("contaminates_learning")) else 1.0),
                            4,
                        ),
                        json.dumps(
                            {
                                "source": "rule_review",
                                "factor_generation": factor_generation,
                                "factor_training_lineage": factor_training_lineage,
                                "primary_responsibility": taxonomy["primary_responsibility"],
                                "responsibility_labels": taxonomy["responsibility_labels"],
                                "system_contaminated": bool((system_issue or {}).get("contaminates_learning")),
                                "system_issue_labels": list((system_issue or {}).get("labels") or []),
                                "factor_training_allowed": not bool((system_issue or {}).get("contaminates_learning")),
                                "factor_role": (
                                    "harmful"
                                    if float(mc) < 0
                                    else ("helpful" if float(mc) > 0 else "neutral")
                                ),
                                "thesis_status_at_exit": review_json["thesis_status_at_exit"],
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )

        return {
            "accepted": True,
            "review_id": review_id,
            "trade_id": trade_id,
            "position_id": position_id,
            "regime_id": regime_id,
            "outcome_label": outcome_label,
            "pnl": float(pnl),
            "failure_tags": failure_tags,
            "summary_text": summary,
            "review_json": review_json,
        }

    def _find_existing_review(
        self,
        conn: sqlite3.Connection,
        *,
        position_id: str,
        real_pnl: dict,
        close_ts: float,
    ) -> dict | None:
        real_deal_id = _safe_int((real_pnl or {}).get("deal_id"))
        rows = self._execute(conn,
            """
            SELECT review_id, trade_id, position_id, outcome_label, pnl,
                   failure_tags_json, summary_text, review_json, created_at
            FROM trade_outcome_review
            WHERE position_id=?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (position_id,),
        ).fetchall()
        for row in rows:
            try:
                review_json = json.loads(row["review_json"] or "{}")
            except Exception:
                review_json = {}
            existing_real = review_json.get("real_pnl") or {}
            existing_deal_id = _safe_int(existing_real.get("deal_id"))
            existing_close_ts = _safe_float(review_json.get("close_ts"), _safe_float(row["created_at"]))
            same_deal = real_deal_id > 0 and existing_deal_id == real_deal_id
            same_close = real_deal_id <= 0 and existing_close_ts > 0 and abs(existing_close_ts - close_ts) < 1.0
            if not (same_deal or same_close):
                continue
            try:
                failure_tags = json.loads(row["failure_tags_json"] or "[]")
            except Exception:
                failure_tags = []
            return {
                "review_id": str(row["review_id"]),
                "trade_id": str(row["trade_id"] or position_id),
                "outcome_label": str(row["outcome_label"] or ""),
                "pnl": float(row["pnl"] or 0.0),
                "failure_tags": failure_tags if isinstance(failure_tags, list) else [],
                "summary_text": str(row["summary_text"] or ""),
                "review_json": review_json,
            }
        return None

