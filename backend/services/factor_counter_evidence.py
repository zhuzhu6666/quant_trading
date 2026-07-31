from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.services._brain_helpers import loads
from backend.services.failure_taxonomy import FACTOR_PENALTY_BLOCKED_RESPONSIBILITIES
from backend.services.review_contract import review_has_system_contamination


KEEP_BLOCK_THRESHOLD = 0.65
REGIME_EXCEPTION_THRESHOLD = 0.55


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _conn_is_pg(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn: Any, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn: Any, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


class FactorCounterEvidenceService:
    """Read-only counter-evidence scan for factor pruning candidates."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "factor_counter_evidence_boundary.v1",
            "read_only": True,
            "affects_trading": False,
            "does_not_write_policy_suggestion": True,
            "does_not_apply_factor_weights": True,
            "does_not_disable_factors": True,
            "used_as_pruning_brake": True,
        }

    def build_for_factor(self, factor: str, *, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
        factor = str(factor or "")
        if not factor:
            return self._empty("missing_factor")
        conn = _connect(self.db_path, read_only=True)
        try:
            shadow = self._shadow_counter_evidence(conn, factor)
            contribution = self._contribution_counter_evidence(conn, factor)
            memory = self._memory_counter_evidence(conn, factor)
        finally:
            conn.close()
        keep_score = min(1.0, shadow["keep_score"] + contribution["keep_score"] + memory["keep_score"])
        prune_score = min(1.0, _safe_float((candidate or {}).get("evidence_score"), 0.0) * 0.35 + contribution["prune_score"] + memory["prune_score"])
        regime_exception = self._regime_exception(contribution)
        if keep_score >= KEEP_BLOCK_THRESHOLD:
            recommended_stage = "block_pruning"
        elif bool(regime_exception.get("exists")):
            recommended_stage = "regime_exception_review"
        else:
            recommended_stage = "allow_pruning"
        return {
            "ok": True,
            "schema_version": "factor_counter_evidence.v1",
            "factor": factor,
            "recommended_stage": recommended_stage,
            "keep_score": round(keep_score, 4),
            "prune_score": round(prune_score, 4),
            "regime_exception": regime_exception,
            "sources": {
                "shadow_factor_perf": shadow,
                "factor_contribution_review": contribution,
                "experience_memory": memory,
            },
            "candidate": {
                "candidate_id": (candidate or {}).get("candidate_id", ""),
                "evidence_score": (candidate or {}).get("evidence_score", 0.0),
            },
            "boundary": self.boundary(),
        }

    def _shadow_counter_evidence(self, conn: Any, factor: str) -> dict[str, Any]:
        if not state_table_exists(conn, "shadow_factor_perf"):
            return {"available": False, "keep_score": 0.0, "sample_count": 0}
        row = _execute(
            conn,
            """
            SELECT factor, oos_bars, cumulative_pnl, hit_rate, max_drawdown, metrics_json, updated_at
            FROM shadow_factor_perf
            WHERE factor=?
            LIMIT 1
            """,
            (factor,),
        ).fetchone()
        if not row:
            return {"available": True, "keep_score": 0.0, "sample_count": 0}
        oos_bars = int(_safe_float(row["oos_bars"]))
        pnl = _safe_float(row["cumulative_pnl"])
        hit_rate = _safe_float(row["hit_rate"])
        keep = 0.0
        if oos_bars >= 100 and pnl > 0:
            keep += 0.25
        if oos_bars >= 100 and hit_rate >= 0.52:
            keep += 0.20
        if pnl > 50:
            keep += 0.15
        return {
            "available": True,
            "keep_score": round(min(0.55, keep), 4),
            "sample_count": oos_bars,
            "cumulative_pnl": round(pnl, 6),
            "hit_rate": round(hit_rate, 6),
            "updated_at": _safe_float(row["updated_at"]),
        }

    def _contribution_counter_evidence(self, conn: Any, factor: str) -> dict[str, Any]:
        if not state_table_exists(conn, "factor_contribution_review") or not state_table_exists(conn, "trade_outcome_review"):
            return {"available": False, "keep_score": 0.0, "prune_score": 0.0, "sample_count": 0, "regimes": []}
        rows = _execute(
            conn,
            """
            SELECT f.net_contribution, f.confidence, r.pnl, r.outcome_label,
                   r.regime_fit_score, r.review_json, r.created_at
            FROM factor_contribution_review f
            JOIN trade_outcome_review r ON r.review_id = f.review_id
            WHERE f.factor=?
            ORDER BY r.created_at DESC, f.id DESC
            LIMIT 80
            """,
            (factor,),
        ).fetchall()
        rows = [
            row
            for row in rows
            if self._primary_responsibility(row["review_json"])
            not in FACTOR_PENALTY_BLOCKED_RESPONSIBILITIES
        ]
        if not rows:
            return {"available": True, "keep_score": 0.0, "prune_score": 0.0, "sample_count": 0, "regimes": []}
        weighted_sum = 0.0
        confidence_sum = 0.0
        positive = 0
        negative = 0
        by_regime: dict[str, dict[str, float]] = {}
        for row in rows:
            net = _safe_float(row["net_contribution"])
            confidence = max(0.05, min(1.0, _safe_float(row["confidence"], 0.5)))
            weighted_sum += net * confidence
            confidence_sum += confidence
            if net > 0:
                positive += 1
            elif net < 0:
                negative += 1
            regime = self._extract_regime(row["review_json"])
            bucket = by_regime.setdefault(regime, {"count": 0.0, "net": 0.0, "confidence": 0.0})
            bucket["count"] += 1.0
            bucket["net"] += net * confidence
            bucket["confidence"] += confidence
        avg_net = weighted_sum / max(confidence_sum, 1e-9)
        sample_count = len(rows)
        keep = 0.0
        prune = 0.0
        if sample_count >= 3 and avg_net >= 0.08:
            keep += 0.35
        if sample_count >= 3 and positive / max(sample_count, 1) >= 0.55:
            keep += 0.20
        if sample_count >= 3 and avg_net <= -0.08:
            prune += 0.35
        if sample_count >= 3 and negative / max(sample_count, 1) >= 0.55:
            prune += 0.20
        regimes = []
        for name, bucket in by_regime.items():
            avg = bucket["net"] / max(bucket["confidence"], 1e-9)
            regimes.append({"regime": name, "sample_count": int(bucket["count"]), "avg_net_contribution": round(avg, 6)})
        regimes = sorted(regimes, key=lambda item: (item["avg_net_contribution"], item["sample_count"]), reverse=True)
        return {
            "available": True,
            "keep_score": round(min(0.65, keep), 4),
            "prune_score": round(min(0.65, prune), 4),
            "sample_count": sample_count,
            "avg_net_contribution": round(avg_net, 6),
            "positive_count": positive,
            "negative_count": negative,
            "regimes": regimes[:8],
        }

    def _memory_counter_evidence(self, conn: Any, factor: str) -> dict[str, Any]:
        if not state_table_exists(conn, "experience_memory"):
            return {"available": False, "keep_score": 0.0, "prune_score": 0.0, "sample_count": 0}
        like = f"%{factor}%"
        rows = _execute(
            conn,
            """
            SELECT e.reward_score, e.recommended_action, e.failure_tags_json,
                   e.decision_context_json, e.created_at,
                   r.review_json AS source_review_json
            FROM experience_memory e
            JOIN trade_outcome_review r
              ON e.source_table='trade_outcome_review'
             AND r.review_id=e.source_id
            WHERE e.append_source='trade_lesson_memory.v1'
              AND (e.decision_context_json LIKE ? OR e.recommended_action LIKE ?)
            ORDER BY e.created_at DESC
            """,
            (like, like),
        ).fetchall()
        if not rows:
            return {"available": True, "keep_score": 0.0, "prune_score": 0.0, "sample_count": 0}
        keep = 0.0
        prune = 0.0
        positive = 0
        negative = 0
        sample_count = 0
        for row in rows:
            if review_has_system_contamination(loads(row["source_review_json"], {})):
                continue
            if self._primary_responsibility(row["source_review_json"]) in FACTOR_PENALTY_BLOCKED_RESPONSIBILITIES:
                continue
            if sample_count >= 50:
                break
            sample_count += 1
            reward = _safe_float(row["reward_score"])
            action = str(row["recommended_action"] or "").lower()
            if reward >= 0.15 and "downweight" not in action:
                positive += 1
            if reward <= -0.15 or "downweight" in action:
                negative += 1
        if sample_count >= 2 and positive / max(sample_count, 1) >= 0.5:
            keep += 0.25
        if sample_count >= 2 and negative / max(sample_count, 1) >= 0.5:
            prune += 0.25
        return {
            "available": True,
            "keep_score": round(keep, 4),
            "prune_score": round(prune, 4),
            "sample_count": sample_count,
            "positive_memory_count": positive,
            "negative_memory_count": negative,
        }

    @staticmethod
    def _primary_responsibility(raw: Any) -> str:
        payload = loads(raw, {})
        if not isinstance(payload, dict):
            return ""
        taxonomy = payload.get("failure_taxonomy") or {}
        return str(
            payload.get("primary_responsibility")
            or (taxonomy.get("primary_responsibility") if isinstance(taxonomy, dict) else "")
            or ""
        ).strip().lower()

    @staticmethod
    def _extract_regime(raw: Any) -> str:
        import json

        try:
            payload = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
        except Exception:
            payload = {}
        return str(payload.get("regime") or payload.get("market_regime") or "unknown")

    @staticmethod
    def _regime_exception(contribution: dict[str, Any]) -> dict[str, Any]:
        regimes = list(contribution.get("regimes") or [])
        for item in regimes:
            if int(item.get("sample_count") or 0) >= 2 and _safe_float(item.get("avg_net_contribution")) >= REGIME_EXCEPTION_THRESHOLD:
                return {
                    "exists": True,
                    "regime": item.get("regime", ""),
                    "avg_net_contribution": item.get("avg_net_contribution", 0.0),
                    "sample_count": item.get("sample_count", 0),
                }
        return {"exists": False}

    def _empty(self, status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "factor_counter_evidence.v1",
            "status": status,
            "keep_score": 0.0,
            "prune_score": 0.0,
            "regime_exception": {"exists": False},
            "boundary": self.boundary(),
        }
