from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services.failure_taxonomy import build_failure_taxonomy
from backend.services.position_metrics import normalize_path_state, update_position_path_metrics
from backend.services.review_contract import normalize_trade_review_contract


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
        if has_net:
            return True, ""
        if close_reason in {"broker_close", "restart_replay", "emergency_close"}:
            return False, "missing_real_pnl"
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
        attribution_integrity: str = "full",
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
            entry_score = float(entry["action_score"] or 0.0) if entry else 0.0
            regime_id = str(entry["regime_id"] or "") if entry else ""
            entry_ts = float(entry["decision_ts"] or 0.0) if entry else 0.0
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

        top_weight_factor = ""
        top_weight = 0.0
        if entry_factors:
            best = max(entry_factors, key=lambda r: abs(float(r["policy_weight"] or 0.0)))
            top_weight_factor = str(best["factor"])
            top_weight = float(best["policy_weight"] or 0.0)

        worst_factor = ""
        worst_mc = 0.0
        if contributions:
            worst_factor, worst_mc = min(contributions.items(), key=lambda kv: kv[1])

        pos_mc = sum(v for v in contributions.values() if v > 0)
        neg_mc = sum(v for v in contributions.values() if v < 0)
        total_abs_mc = sum(abs(v) for v in contributions.values()) or 1.0
        positive_share = pos_mc / total_abs_mc

        failure_tags: list[str] = []
        if pnl > 0:
            outcome_label = "good_win" if positive_share >= 0.55 else "lucky_win"
            if outcome_label == "lucky_win":
                failure_tags.append("lucky_win")
        else:
            conviction = abs(entry_score)
            has_entry_context = entry is not None
            has_attribution = bool(contributions)
            conflict = has_attribution and pos_mc > 0 and neg_mc < 0
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

        entry_quality = _clamp(0.55 + (0.25 if pnl > 0 else -0.30) * min(abs(entry_score), 1.0))
        hold_quality = _clamp(0.55 if pnl > 0 else 0.40)
        exit_quality = _clamp(0.55 if real_pnl else 0.45)
        regime_fit_score = _clamp(
            0.70
            if pnl > 0
            else 0.35 + (0.10 if "clean_good_loss" in failure_tags else 0.0) - (0.05 if "avoidable_loss" in failure_tags else 0.0)
        )
        execution_quality = _clamp(0.60 if real_pnl else 0.45)
        close_ts = float(close_ts or time.time())
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

        top_factor = ""
        top_factor_mc = 0.0
        if contributions:
            top_factor, top_factor_mc = max(contributions.items(), key=lambda kv: abs(kv[1]))

        summary = (
            f"trade {position_id} closed pnl={pnl:.2f}; "
            f"outcome={outcome_label}; "
            f"primary_factor={top_factor or top_weight_factor or 'n/a'}; "
            f"worst_factor={worst_factor or 'n/a'}"
        )
        review_json = {
            "contract_version": "phase_d.v1",
            "position_id": position_id,
            "trade_id": trade_id,
            "entry_decision_id": entry_decision_id,
            "exit_decision_id": exit_decision_id,
            "entry_ts": entry_ts,
            "close_ts": close_ts,
            "holding_seconds": round(holding_seconds, 3),
            "holding_minutes": round(holding_seconds / 60.0, 3),
            "timeframe": timeframe,
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
            "entry_action": entry_action if isinstance(entry_action, dict) else {},
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
            "data_quality_context": data_quality_context or {},
            "decision_quality_context": (entry_action or {}).get("decision_quality_context", {}) if isinstance(entry_action, dict) else {},
            "top_weight_factor": top_weight_factor,
            "top_weight": top_weight,
            "top_factor": top_factor,
            "top_factor_mc": top_factor_mc,
            "worst_factor": worst_factor,
            "worst_factor_mc": worst_mc,
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
                        round(_clamp(abs(mc) / max(abs(pnl), 1.0)), 4),
                        json.dumps(
                            {
                                "source": "rule_review",
                                "primary_responsibility": taxonomy["primary_responsibility"],
                                "responsibility_labels": taxonomy["responsibility_labels"],
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

