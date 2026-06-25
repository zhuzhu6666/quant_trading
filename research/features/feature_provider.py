from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, STATE_DB_DDL


SCHEMA_VERSION = "learning_sample.v1"
DECISION_SCHEMA_VERSION = "decision_sample.v1"


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _fmt(value: Any, digits: int = 4) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return str(value)


class LearningFeatureProvider:
    """Build model-ready, explainable samples from the rule-learning ledger.

    This is intentionally read-only. It gives future statistical models or LLM
    review tools one stable contract instead of letting them scrape runtime
    tables directly.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(STATE_DB_DDL)
            conn.commit()

    def _factor_snapshots(self, decision_id: str) -> list[dict]:
        if not decision_id:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT factor, source, raw_value, normalized_value, direction,
                       base_weight, policy_weight, shadow_score, health_score,
                       gated, gated_reason, contribution_score
                FROM decision_factor_snapshot
                WHERE decision_id=?
                ORDER BY ABS(contribution_score) DESC, factor ASC
                """,
                (decision_id,),
            ).fetchall()
        return [
            {
                "factor": str(row["factor"] or ""),
                "source": str(row["source"] or "registry"),
                "raw_value": _safe_float(row["raw_value"]),
                "normalized_value": _safe_float(row["normalized_value"]),
                "direction": _safe_float(row["direction"]),
                "base_weight": _safe_float(row["base_weight"]),
                "policy_weight": _safe_float(row["policy_weight"]),
                "shadow_score": _safe_float(row["shadow_score"]),
                "health_score": _safe_float(row["health_score"]),
                "gated": bool(row["gated"]),
                "gated_reason": str(row["gated_reason"] or ""),
                "contribution_score": _safe_float(row["contribution_score"]),
            }
            for row in rows
        ]

    def _factor_contribution_reviews(self, review_id: str) -> list[dict]:
        if not review_id:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT factor, entry_contribution, hold_contribution,
                       exit_contribution, net_contribution, confidence, notes
                FROM factor_contribution_review
                WHERE review_id=?
                ORDER BY ABS(net_contribution) DESC, factor ASC
                """,
                (review_id,),
            ).fetchall()
        return [
            {
                "factor": str(row["factor"] or ""),
                "entry_contribution": _safe_float(row["entry_contribution"]),
                "hold_contribution": _safe_float(row["hold_contribution"]),
                "exit_contribution": _safe_float(row["exit_contribution"]),
                "net_contribution": _safe_float(row["net_contribution"]),
                "confidence": _safe_float(row["confidence"]),
                "notes": str(row["notes"] or ""),
            }
            for row in rows
        ]

    def _order_events(self, *, decision_ids: list[str] | None = None, trade_id: str = "") -> list[dict]:
        ids = [str(item) for item in (decision_ids or []) if str(item)]
        clauses = []
        params: list[Any] = []
        if trade_id:
            clauses.append("trade_id=?")
            params.append(str(trade_id))
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses.append(f"decision_id IN ({placeholders})")
            params.extend(ids)
        if not clauses:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, decision_id, trade_id, order_id, broker_order_id,
                       event_type, event_ts, price, volume, status, details_json
                FROM order_lifecycle_event
                WHERE {" OR ".join(clauses)}
                ORDER BY event_ts ASC, event_id ASC
                """,
                tuple(params),
            ).fetchall()
        seen = set()
        events = []
        for row in rows:
            event_id = str(row["event_id"] or "")
            if event_id and event_id in seen:
                continue
            seen.add(event_id)
            events.append(
                {
                    "event_id": event_id,
                    "decision_id": str(row["decision_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "order_id": str(row["order_id"] or ""),
                    "broker_order_id": str(row["broker_order_id"] or ""),
                    "event_type": str(row["event_type"] or ""),
                    "event_ts": _safe_float(row["event_ts"]),
                    "price": _safe_float(row["price"]),
                    "volume": _safe_float(row["volume"]),
                    "status": str(row["status"] or ""),
                    "details": _loads(row["details_json"], {}),
                }
            )
        return events

    def _position_events(self, *, position_id: str = "", trade_id: str = "") -> list[dict]:
        clauses = []
        params: list[Any] = []
        if position_id:
            clauses.append("position_id=?")
            params.append(str(position_id))
        if trade_id:
            clauses.append("trade_id=?")
            params.append(str(trade_id))
        if not clauses:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, position_id, trade_id, symbol, event_type, event_ts,
                       net_volume, avg_price, unrealized_pnl, realized_pnl, details_json
                FROM position_lifecycle_event
                WHERE {" OR ".join(clauses)}
                ORDER BY event_ts ASC, event_id ASC
                """,
                tuple(params),
            ).fetchall()
        seen = set()
        events = []
        for row in rows:
            event_id = str(row["event_id"] or "")
            if event_id and event_id in seen:
                continue
            seen.add(event_id)
            events.append(
                {
                    "event_id": event_id,
                    "position_id": str(row["position_id"] or ""),
                    "trade_id": str(row["trade_id"] or ""),
                    "symbol": str(row["symbol"] or ""),
                    "event_type": str(row["event_type"] or ""),
                    "event_ts": _safe_float(row["event_ts"]),
                    "net_volume": _safe_float(row["net_volume"]),
                    "avg_price": _safe_float(row["avg_price"]),
                    "unrealized_pnl": _safe_float(row["unrealized_pnl"]),
                    "realized_pnl": _safe_float(row["realized_pnl"]),
                    "details": _loads(row["details_json"], {}),
                }
            )
        return events

    def _execution_trace(
        self,
        *,
        decision_ids: list[str] | None = None,
        trade_id: str = "",
        position_id: str = "",
    ) -> dict:
        order_events = self._order_events(decision_ids=decision_ids, trade_id=trade_id)
        position_events = self._position_events(position_id=position_id, trade_id=trade_id)
        order_statuses: dict[str, int] = {}
        position_statuses: dict[str, int] = {}
        for item in order_events:
            key = str(item.get("status") or item.get("event_type") or "unknown")
            order_statuses[key] = order_statuses.get(key, 0) + 1
        for item in position_events:
            key = str(item.get("event_type") or "unknown")
            position_statuses[key] = position_statuses.get(key, 0) + 1
        return {
            "order_events": order_events,
            "position_events": position_events,
            "summary": {
                "order_event_count": len(order_events),
                "position_event_count": len(position_events),
                "order_statuses": order_statuses,
                "position_event_types": position_statuses,
                "has_broker_lifecycle": bool(order_events or position_events),
                "has_failed_order": any(str(item.get("status") or "") == "failed" for item in order_events),
            },
        }

    @staticmethod
    def _decision_llm_context(
        *,
        decision: dict,
        target: dict,
        factors: list[dict],
        execution_trace: dict,
    ) -> dict:
        event_type = str(target.get("event_type") or decision.get("event_type") or "")
        top = factors[:3]
        evidence = [
            f"event={event_type}, score={_fmt(target.get('action_score'))}, direction={target.get('direction')}",
            f"gate_passed={target.get('gate_passed')}, reason={target.get('gate_reason') or decision.get('action_reason') or 'n/a'}",
        ]
        if target.get("failed_execution"):
            evidence.append(f"execution_failed at {target.get('skip_stage') or 'unknown_stage'}")
        if top:
            evidence.append(
                "top_factors="
                + ", ".join(
                    f"{item.get('factor')}:{_fmt(item.get('contribution_score'))}"
                    for item in top
                )
            )
        summary = execution_trace.get("summary") or {}
        if summary.get("order_event_count") or summary.get("position_event_count"):
            evidence.append(
                f"lifecycle orders={summary.get('order_event_count', 0)}, positions={summary.get('position_event_count', 0)}"
            )
        prompt_card = (
            f"{event_type} | {decision.get('symbol')}/{decision.get('timeframe')} | "
            f"score={_fmt(target.get('action_score'))} | "
            f"reason={target.get('gate_reason') or decision.get('action_reason') or 'n/a'}"
        )
        return {
            "task_hint": "Explain this decision and identify whether factors, gates, or execution lifecycle drove the outcome.",
            "prompt_card": prompt_card,
            "evidence_bullets": evidence,
            "label_summary": {
                "event_type": event_type,
                "executed": bool(target.get("executed")),
                "skipped": bool(target.get("skipped")),
                "failed_execution": bool(target.get("failed_execution")),
            },
        }

    @staticmethod
    def _trade_llm_context(
        *,
        review: dict,
        target: dict,
        factor_outcomes: list[dict],
        attribution_alignment: dict,
        execution_trace: dict,
        application_context: list[dict],
    ) -> dict:
        harmful = [
            item for item in factor_outcomes
            if (item.get("outcome_contribution") or {}).get("outcome_role") == "harmful"
        ][:3]
        helpful = [
            item for item in factor_outcomes
            if (item.get("outcome_contribution") or {}).get("outcome_role") == "helpful"
        ][:3]
        evidence = [
            f"outcome={target.get('outcome_label')}, pnl={_fmt(target.get('pnl'), 2)}, reward={_fmt(target.get('reward_score'))}",
            f"failure_tags={','.join(target.get('failure_tags') or []) or 'none'}",
        ]
        if harmful:
            evidence.append(
                "harmful_factors="
                + ", ".join(
                    f"{item.get('factor')}:{_fmt((item.get('outcome_contribution') or {}).get('net_contribution'), 2)}"
                    for item in harmful
                )
            )
        if helpful:
            evidence.append(
                "helpful_factors="
                + ", ".join(
                    f"{item.get('factor')}:{_fmt((item.get('outcome_contribution') or {}).get('net_contribution'), 2)}"
                    for item in helpful
                )
            )
        labels = attribution_alignment.get("labels") or {}
        if labels:
            evidence.append(
                "attribution_labels="
                + ", ".join(f"{key}:{value}" for key, value in sorted(labels.items()))
            )
        summary = execution_trace.get("summary") or {}
        if summary.get("order_event_count") or summary.get("position_event_count"):
            evidence.append(
                f"execution_trace orders={summary.get('order_event_count', 0)}, positions={summary.get('position_event_count', 0)}, failed_order={summary.get('has_failed_order')}"
            )
        if application_context:
            latest = application_context[0]
            evidence.append(
                f"prior_learning={latest.get('scope_key')} {latest.get('action')} status={latest.get('status')}"
            )
        prompt_card = (
            f"trade {review.get('trade_id') or review.get('position_id')} | "
            f"{target.get('outcome_label')} | pnl={_fmt(target.get('pnl'), 2)} | "
            f"recommended={target.get('recommended_action') or 'n/a'}"
        )
        return {
            "task_hint": "Explain the trade outcome, cite factor evidence, and suggest whether the rule-learning action is justified.",
            "prompt_card": prompt_card,
            "evidence_bullets": evidence,
            "label_summary": {
                "outcome_label": target.get("outcome_label"),
                "recommended_action": target.get("recommended_action"),
                "pnl": target.get("pnl"),
                "failure_tags": target.get("failure_tags") or [],
            },
        }

    @staticmethod
    def _align_factor_outcomes(
        factors: list[dict],
        contribution_reviews: list[dict],
    ) -> list[dict]:
        by_factor = {str(item.get("factor") or ""): item for item in contribution_reviews}
        aligned: list[dict] = []
        for factor in factors:
            name = str(factor.get("factor") or "")
            review = by_factor.get(name, {})
            entry = _safe_float(factor.get("contribution_score"))
            net = _safe_float(review.get("net_contribution"))
            delta = net - entry
            same_direction = (entry == 0.0 and net == 0.0) or (entry > 0 and net > 0) or (entry < 0 and net < 0)
            if not review:
                attribution_label = "missing_outcome"
            elif same_direction and abs(net) >= abs(entry):
                attribution_label = "confirmed"
            elif same_direction:
                attribution_label = "weakened"
            else:
                attribution_label = "contradicted"
            outcome_role = "neutral"
            if net > 0:
                outcome_role = "helpful"
            elif net < 0:
                outcome_role = "harmful"
            aligned.append(
                {
                    **factor,
                    "outcome_contribution": {
                        "entry_contribution": entry,
                        "hold_contribution": _safe_float(review.get("hold_contribution")),
                        "exit_contribution": _safe_float(review.get("exit_contribution")),
                        "net_contribution": net,
                        "contribution_delta": round(delta, 6),
                        "confidence": _safe_float(review.get("confidence")),
                        "notes": str(review.get("notes") or ""),
                        "same_direction": same_direction,
                        "attribution_label": attribution_label,
                        "outcome_role": outcome_role,
                    },
                }
            )
        for review in contribution_reviews:
            name = str(review.get("factor") or "")
            if name and not any(str(item.get("factor") or "") == name for item in aligned):
                net = _safe_float(review.get("net_contribution"))
                aligned.append(
                    {
                        "factor": name,
                        "source": "review_only",
                        "raw_value": 0.0,
                        "normalized_value": 0.0,
                        "direction": 0.0,
                        "base_weight": 0.0,
                        "policy_weight": 0.0,
                        "shadow_score": 0.0,
                        "health_score": 0.0,
                        "gated": True,
                        "gated_reason": "missing_entry_snapshot",
                        "contribution_score": 0.0,
                        "outcome_contribution": {
                            "entry_contribution": 0.0,
                            "hold_contribution": _safe_float(review.get("hold_contribution")),
                            "exit_contribution": _safe_float(review.get("exit_contribution")),
                            "net_contribution": net,
                            "contribution_delta": round(net, 6),
                            "confidence": _safe_float(review.get("confidence")),
                            "notes": str(review.get("notes") or ""),
                            "same_direction": False,
                            "attribution_label": "review_only",
                            "outcome_role": "helpful" if net > 0 else "harmful" if net < 0 else "neutral",
                        },
                    }
                )
        return sorted(aligned, key=lambda item: abs(_safe_float(item.get("outcome_contribution", {}).get("net_contribution"))), reverse=True)

    @staticmethod
    def _attribution_alignment(factors: list[dict]) -> dict:
        labels: dict[str, int] = {}
        total_abs_net = 0.0
        total_abs_delta = 0.0
        for factor in factors:
            outcome = factor.get("outcome_contribution") or {}
            label = str(outcome.get("attribution_label") or "unknown")
            labels[label] = labels.get(label, 0) + 1
            total_abs_net += abs(_safe_float(outcome.get("net_contribution")))
            total_abs_delta += abs(_safe_float(outcome.get("contribution_delta")))
        most_harmful = [
            {
                "factor": item.get("factor"),
                "net_contribution": _safe_float(item.get("outcome_contribution", {}).get("net_contribution")),
                "entry_contribution": _safe_float(item.get("outcome_contribution", {}).get("entry_contribution")),
                "attribution_label": item.get("outcome_contribution", {}).get("attribution_label"),
                "outcome_role": item.get("outcome_contribution", {}).get("outcome_role"),
            }
            for item in sorted(
                factors,
                key=lambda item: _safe_float(item.get("outcome_contribution", {}).get("net_contribution")),
            )[:5]
        ]
        return {
            "factor_count": len(factors),
            "labels": labels,
            "total_abs_net_contribution": round(total_abs_net, 6),
            "total_abs_contribution_delta": round(total_abs_delta, 6),
            "most_harmful_factors": most_harmful,
        }

    def build_decision_features(self, decision_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM decision_ledger WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"decision not found: {decision_id}")

        factors = self._factor_snapshots(decision_id)
        action = _loads(row["action_json"], {})
        risk_state = _loads(row["risk_state_json"], {})
        portfolio_state = _loads(row["portfolio_state_json"], {})
        tags_breakdown = action.get("tags_breakdown") if isinstance(action, dict) else {}
        return {
            "decision_id": str(row["decision_id"]),
            "event_type": str(row["event_type"] or ""),
            "symbol": str(row["symbol"] or ""),
            "timeframe": str(row["timeframe"] or ""),
            "decision_ts": _safe_float(row["decision_ts"]),
            "regime_id": str(row["regime_id"] or ""),
            "regime_confidence": _safe_float(row["regime_confidence"]),
            "policy_version": str(row["policy_version"] or ""),
            "factor_set_version": str(row["factor_set_version"] or ""),
            "action_score": _safe_float(row["action_score"]),
            "action_reason": str(row["action_reason"] or ""),
            "action": action,
            "risk_state": risk_state,
            "portfolio_state": portfolio_state,
            "factor_count": len(factors),
            "active_factor_count": sum(1 for f in factors if not f["gated"]),
            "top_factors": factors[:10],
            "factor_evidence": factors,
            "factor_tags": tags_breakdown or {},
        }

    @staticmethod
    def _decision_quality(decision: dict) -> dict:
        action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
        checks = {
            "has_factor_snapshot": bool(decision.get("factor_evidence")),
            "has_action_payload": bool(action),
            "has_action_reason": bool(decision.get("action_reason")),
            "has_symbol": bool(decision.get("symbol")),
            "has_timeframe": bool(decision.get("timeframe")),
        }
        weights = {
            "has_factor_snapshot": 0.35,
            "has_action_payload": 0.25,
            "has_action_reason": 0.20,
            "has_symbol": 0.10,
            "has_timeframe": 0.10,
        }
        score = sum(weights[k] for k, ok in checks.items() if ok)
        return {
            "quality_score": round(score, 4),
            "model_ready": score >= 0.70 and checks["has_factor_snapshot"] and checks["has_action_reason"],
            "checks": checks,
            "missing": [k for k, ok in checks.items() if not ok],
        }

    def build_decision_sample(self, decision_id: str) -> dict:
        decision = self.build_decision_features(decision_id)
        action = decision.get("action") if isinstance(decision.get("action"), dict) else {}
        event_type = str(decision.get("event_type") or "")
        gate_passed = bool(action.get("gate_passed", False))
        target = {
            "event_type": event_type,
            "executed": event_type == "open",
            "skipped": event_type in {"skip", "hold", "order_failed", "amend_failed"} or (event_type == "signal" and not gate_passed),
            "failed_execution": event_type in {"order_failed", "amend_failed"},
            "gate_passed": gate_passed,
            "gate_reason": str(action.get("gate_reason") or decision.get("action_reason") or ""),
            "skip_stage": str(action.get("skip_stage") or ""),
            "direction": int(action.get("direction") or 0),
            "action_score": _safe_float(decision.get("action_score")),
        }
        factors = list(decision.get("factor_evidence") or [])
        execution_trace = self._execution_trace(
            decision_ids=[decision_id],
            trade_id=str(action.get("position_id") or ""),
            position_id=str(action.get("position_id") or ""),
        )
        llm_context = self._decision_llm_context(
            decision=decision,
            target=target,
            factors=factors,
            execution_trace=execution_trace,
        )
        return {
            "schema_version": DECISION_SCHEMA_VERSION,
            "sample_id": f"decision:{decision_id}",
            "quality": self._decision_quality(decision),
            "target": target,
            "decision": decision,
            "execution_trace": execution_trace,
            "llm_context": llm_context,
            "explainability": {
                "summary_text": (
                    f"{event_type} decision score={target['action_score']:.4f}; "
                    f"reason={target['gate_reason'] or decision.get('action_reason') or 'n/a'}"
                ),
                "top_factors": factors[:5],
                "factor_count": len(factors),
                "execution_summary": execution_trace["summary"],
                "evidence_bullets": llm_context["evidence_bullets"],
                "ledger_links": {
                    "decision_id": decision_id,
                    "trade_id": str(action.get("position_id") or ""),
                },
            },
        }

    def _experience_for_trade(self, trade_id: str) -> dict | None:
        if not trade_id:
            return None
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM experience_memory
                WHERE trade_id=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (trade_id,),
            ).fetchone()
        return self._parse_experience(row) if row else None

    def _application_context(self, factors: list[dict], review_created_at: float) -> list[dict]:
        names = [f["factor"] for f in factors if f.get("factor")]
        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT l.application_id, l.scope_type, l.scope_key, l.action,
                       l.bias_multiplier, l.old_weight, l.new_weight, l.status,
                       l.cycle_ts, e.status AS effect_status,
                       e.observed_trade_count, e.baseline_trade_count,
                       e.delta_avg_reward, e.post_win_rate, e.baseline_win_rate,
                       e.decision_json
                FROM learning_application_log l
                LEFT JOIN learning_application_effect e
                  ON e.application_id = l.application_id
                WHERE l.scope_type='factor'
                  AND l.scope_key IN ({placeholders})
                  AND l.cycle_ts <= ?
                ORDER BY l.cycle_ts DESC
                LIMIT 20
                """,
                (*names, review_created_at),
            ).fetchall()
        return [
            {
                "application_id": str(row["application_id"] or ""),
                "scope_type": str(row["scope_type"] or ""),
                "scope_key": str(row["scope_key"] or ""),
                "action": str(row["action"] or ""),
                "bias_multiplier": _safe_float(row["bias_multiplier"], 1.0),
                "old_weight": _safe_float(row["old_weight"]),
                "new_weight": _safe_float(row["new_weight"]),
                "status": str(row["status"] or ""),
                "effect_status": str(row["effect_status"] or ""),
                "observed_trade_count": int(row["observed_trade_count"] or 0),
                "baseline_trade_count": int(row["baseline_trade_count"] or 0),
                "delta_avg_reward": _safe_float(row["delta_avg_reward"]),
                "post_win_rate": _safe_float(row["post_win_rate"]),
                "baseline_win_rate": _safe_float(row["baseline_win_rate"]),
                "decision": _loads(row["decision_json"], {}),
            }
            for row in rows
        ]

    @staticmethod
    def _parse_experience(row: sqlite3.Row) -> dict:
        context = _loads(row["decision_context_json"], {})
        failure_tags = _loads(row["failure_tags_json"], [])
        return {
            "experience_id": str(row["experience_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
            "regime_id": str(row["regime_id"] or ""),
            "setup_hash": str(row["setup_hash"] or ""),
            "decision_context": context,
            "outcome_label": str(row["outcome_label"] or ""),
            "reward_score": _safe_float(row["reward_score"]),
            "failure_tags": failure_tags if isinstance(failure_tags, list) else [],
            "recommended_action": str(row["recommended_action"] or ""),
            "evidence_strength": _safe_float(row["evidence_strength"]),
            "artifact_version": str(row["artifact_version"] or "v1"),
            "created_at": _safe_float(row["created_at"]),
        }

    @staticmethod
    def _quality(
        *,
        review: dict,
        decision: dict | None,
        factors: list[dict],
        contribution_reviews: list[dict],
        experience: dict | None,
    ) -> dict:
        review_json = review.get("review") or {}
        real_pnl = review_json.get("real_pnl") or {}
        has_real_pnl = isinstance(real_pnl, dict) and real_pnl.get("net") is not None
        context_integrity = str(review_json.get("context_integrity", "full") or "full")
        has_label = bool(review.get("outcome_label"))
        checks = {
            "has_real_pnl": has_real_pnl,
            "full_context": context_integrity == "full",
            "has_entry_decision": bool(decision),
            "has_factor_snapshot": bool(factors),
            "has_factor_contribution_review": bool(contribution_reviews),
            "has_outcome_label": has_label,
            "has_experience": bool(experience),
        }
        weights = {
            "has_real_pnl": 0.22,
            "full_context": 0.16,
            "has_entry_decision": 0.16,
            "has_factor_snapshot": 0.16,
            "has_factor_contribution_review": 0.12,
            "has_outcome_label": 0.10,
            "has_experience": 0.08,
        }
        score = sum(weights[k] for k, ok in checks.items() if ok)
        required = (
            "has_real_pnl",
            "full_context",
            "has_entry_decision",
            "has_factor_snapshot",
            "has_factor_contribution_review",
            "has_outcome_label",
            "has_experience",
        )
        return {
            "quality_score": round(score, 4),
            "model_ready": score >= 0.80 and all(checks[k] for k in required),
            "checks": checks,
            "missing": [k for k, ok in checks.items() if not ok],
        }

    def build_trade_features(self, trade_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM trade_outcome_review
                WHERE trade_id=? OR position_id=? OR review_id=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (trade_id, trade_id, trade_id),
            ).fetchone()
        if not row:
            raise KeyError(f"trade review not found: {trade_id}")
        return self._sample_from_review_row(row)

    def build_experience_features(self, experience_id: str) -> dict:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM experience_memory WHERE experience_id=?",
                (experience_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"experience not found: {experience_id}")
        exp = self._parse_experience(row)
        trade_id = exp.get("trade_id") or ""
        sample = self.build_trade_features(trade_id)
        sample["experience"] = exp
        return sample

    def _sample_from_review_row(self, row: sqlite3.Row) -> dict:
        failure_tags = _loads(row["failure_tags_json"], [])
        review_json = _loads(row["review_json"], {})
        review = {
            "review_id": str(row["review_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
            "position_id": str(row["position_id"] or ""),
            "entry_decision_id": str(row["entry_decision_id"] or ""),
            "exit_decision_id": str(row["exit_decision_id"] or ""),
            "entry_quality": _safe_float(row["entry_quality"]),
            "hold_quality": _safe_float(row["hold_quality"]),
            "exit_quality": _safe_float(row["exit_quality"]),
            "regime_fit_score": _safe_float(row["regime_fit_score"]),
            "execution_quality": _safe_float(row["execution_quality"]),
            "pnl": _safe_float(row["pnl"]),
            "mae": _safe_float(row["mae"]),
            "mfe": _safe_float(row["mfe"]),
            "outcome_label": str(row["outcome_label"] or ""),
            "failure_tags": failure_tags if isinstance(failure_tags, list) else [],
            "summary_text": str(row["summary_text"] or ""),
            "review": review_json if isinstance(review_json, dict) else {},
            "created_at": _safe_float(row["created_at"]),
        }
        decision = None
        factors: list[dict] = []
        if review["entry_decision_id"]:
            try:
                decision = self.build_decision_features(review["entry_decision_id"])
                factors = decision["factor_evidence"]
            except KeyError:
                decision = None
                factors = self._factor_snapshots(review["entry_decision_id"])
        contribution_reviews = self._factor_contribution_reviews(review["review_id"])
        factor_outcomes = self._align_factor_outcomes(factors, contribution_reviews)
        attribution_alignment = self._attribution_alignment(factor_outcomes)
        experience = self._experience_for_trade(review["trade_id"])
        application_context = self._application_context(factors, review["created_at"])
        execution_trace = self._execution_trace(
            decision_ids=[review["entry_decision_id"], review["exit_decision_id"]],
            trade_id=review["trade_id"],
            position_id=review["position_id"],
        )
        quality = self._quality(
            review=review,
            decision=decision,
            factors=factors,
            contribution_reviews=contribution_reviews,
            experience=experience,
        )
        target = {
            "outcome_label": review["outcome_label"],
            "reward_score": experience["reward_score"] if experience else None,
            "pnl": review["pnl"],
            "failure_tags": review["failure_tags"],
            "recommended_action": experience["recommended_action"] if experience else "",
        }
        llm_context = self._trade_llm_context(
            review=review,
            target=target,
            factor_outcomes=factor_outcomes,
            attribution_alignment=attribution_alignment,
            execution_trace=execution_trace,
            application_context=application_context,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "sample_id": f"trade:{review['trade_id'] or review['review_id']}",
            "quality": quality,
            "target": target,
            "decision": decision,
            "factor_outcomes": factor_outcomes,
            "attribution_alignment": attribution_alignment,
            "review": review,
            "experience": experience,
            "execution_trace": execution_trace,
            "application_context": application_context,
            "llm_context": llm_context,
            "explainability": {
                "summary_text": review["summary_text"],
                "top_factors": factor_outcomes[:5],
                "factor_count": len(factors),
                "attribution_alignment": attribution_alignment,
                "execution_summary": execution_trace["summary"],
                "evidence_bullets": llm_context["evidence_bullets"],
                "failure_tags": review["failure_tags"],
                "ledger_links": {
                    "entry_decision_id": review["entry_decision_id"],
                    "exit_decision_id": review["exit_decision_id"],
                    "trade_id": review["trade_id"],
                    "position_id": review["position_id"],
                },
            },
        }

    def build_training_samples(
        self,
        *,
        limit: int = 200,
        model_ready_only: bool = False,
    ) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM trade_outcome_review
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        samples = [self._sample_from_review_row(row) for row in rows]
        if model_ready_only:
            samples = [s for s in samples if s["quality"]["model_ready"]]
        return samples

    def build_decision_samples(
        self,
        *,
        limit: int = 200,
        event_types: list[str] | None = None,
        model_ready_only: bool = False,
    ) -> list[dict]:
        params: list[Any] = []
        where = ""
        if event_types:
            clean = [str(item) for item in event_types if str(item)]
            if clean:
                placeholders = ",".join("?" for _ in clean)
                where = f"WHERE event_type IN ({placeholders})"
                params.extend(clean)
        params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(
                f"""
                SELECT decision_id
                FROM decision_ledger
                {where}
                ORDER BY decision_ts DESC, created_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        samples = [self.build_decision_sample(str(row["decision_id"])) for row in rows]
        if model_ready_only:
            samples = [s for s in samples if s["quality"]["model_ready"]]
        return samples
