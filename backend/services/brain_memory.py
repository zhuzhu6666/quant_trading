from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_exists,
)


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _memory_id(source_table: str, source_id: str) -> str:
    raw = f"{source_table}:{source_id}".encode("utf-8")
    return f"mem_{hashlib.sha256(raw).hexdigest()[:24]}"


def ensure_brain_memory_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS brain_memory (
                memory_id TEXT PRIMARY KEY,
                memory_type TEXT DEFAULT '',
                source_table TEXT DEFAULT '',
                source_id TEXT DEFAULT '',
                symbol TEXT DEFAULT '',
                timeframe TEXT DEFAULT '',
                regime TEXT DEFAULT '',
                text_summary TEXT DEFAULT '',
                structured_json TEXT NOT NULL DEFAULT '{}',
                evidence_score REAL NOT NULL DEFAULT 0.0,
                similarity_score REAL NOT NULL DEFAULT 0.0,
                polarity TEXT DEFAULT 'neutral',
                created_at REAL NOT NULL DEFAULT 0.0,
                last_used_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_memory_source ON brain_memory(source_table, source_id)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_memory_type ON brain_memory(memory_type, created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_brain_memory_score ON brain_memory(evidence_score, similarity_score)")
        conn.commit()
    finally:
        conn.close()


class BrainMemoryService:
    """Read-only V16 memory retrieval over existing audit facts.

    This service materializes lightweight memory metadata for auditability, but
    it does not create learning labels, mutate runtime config, or authorize
    trading/governance actions.
    """

    SHADOW_TABLES = {
        "open_quality_shadow_audit": {
            "id": "inference_id",
            "score": "quality_score",
            "risk": "risk_score",
            "summary": "open quality shadow audit",
        },
        "position_quality_shadow_audit": {
            "id": "inference_id",
            "score": "hold_score",
            "risk": "exit_risk_score",
            "summary": "position quality shadow audit",
        },
        "factor_governance_shadow_audit": {
            "id": "inference_id",
            "score": "positive_score",
            "risk": "weakness_score",
            "summary": "factor governance shadow audit",
        },
        "meta_model_shadow_audit": {
            "id": "inference_id",
            "score": "posture_score",
            "risk": "recover_score",
            "summary": "meta model shadow audit",
        },
    }

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "phase": "v16_phase1_read_only_memory",
            "read_only": True,
            "affects_trading": False,
            "does_not_write_learning_samples": True,
            "does_not_authorize_actions": True,
            "source_facts_remain_authoritative": True,
        }

    def retrieve(
        self,
        *,
        world_model: dict[str, Any] | None = None,
        hypotheses: list[dict[str, Any]] | None = None,
        limit: int = 12,
        persist: bool = True,
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        terms = self._query_terms(world_model or {}, hypotheses or [])
        source_gaps: list[str] = []
        items: list[dict[str, Any]] = []
        conn = _connect(self.db_path, read_only=True)
        try:
            items.extend(self._experience_memories(conn, terms, source_gaps))
            items.extend(self._trade_outcome_memories(conn, terms, source_gaps))
            items.extend(self._policy_suggestion_memories(conn, terms, source_gaps))
            items.extend(self._model_permission_memories(conn, terms, source_gaps))
            items.extend(self._shadow_audit_memories(conn, terms, source_gaps))
        finally:
            conn.close()
        ranked = sorted(
            items,
            key=lambda item: (
                _safe_float(item.get("similarity_score")),
                _safe_float(item.get("evidence_score")),
                _safe_float(item.get("created_at")),
            ),
            reverse=True,
        )[:limit]
        if persist and ranked:
            self._persist_items(ranked)
        negative_matches = [item for item in ranked if item.get("polarity") == "negative"]
        counter_evidence = [
            item for item in ranked
            if item.get("polarity") == "positive" and _safe_float(item.get("similarity_score")) >= 0.1
        ]
        return {
            "ok": True,
            "schema_version": "brain_memory_retrieval.v1",
            "items": ranked,
            "negative_matches": negative_matches[:5],
            "counter_evidence": counter_evidence[:5],
            "source_gaps": sorted(set(source_gaps)),
            "query_terms": sorted(terms),
            "boundary": self.boundary(),
            "read_only": True,
            "affects_trading": False,
            "generated_at": time.time(),
        }

    def latest_indexed(self, *, limit: int = 50) -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "brain_memory"):
                return {
                    "ok": False,
                    "schema_version": "brain_memory_index.v1",
                    "status": "missing_table",
                    "items": [],
                    "read_only": True,
                    "affects_trading": False,
                }
            rows = _execute(
                conn,
                """
                SELECT memory_id, memory_type, source_table, source_id, symbol, timeframe,
                       regime, text_summary, structured_json, evidence_score,
                       similarity_score, polarity, created_at, last_used_at
                FROM brain_memory
                ORDER BY last_used_at DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return {
                "ok": True,
                "schema_version": "brain_memory_index.v1",
                "status": "available",
                "items": [self._row_to_item(row) for row in rows],
                "read_only": True,
                "affects_trading": False,
            }
        finally:
            conn.close()

    def _persist_items(self, items: list[dict[str, Any]]) -> None:
        ensure_brain_memory_table(self.db_path)
        now = time.time()
        conn = _connect(self.db_path)
        try:
            for item in items:
                _execute(
                    conn,
                    """
                    INSERT INTO brain_memory
                    (memory_id, memory_type, source_table, source_id, symbol, timeframe,
                     regime, text_summary, structured_json, evidence_score,
                     similarity_score, polarity, created_at, last_used_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        memory_type=excluded.memory_type,
                        symbol=excluded.symbol,
                        timeframe=excluded.timeframe,
                        regime=excluded.regime,
                        text_summary=excluded.text_summary,
                        structured_json=excluded.structured_json,
                        evidence_score=excluded.evidence_score,
                        similarity_score=excluded.similarity_score,
                        polarity=excluded.polarity,
                        last_used_at=excluded.last_used_at
                    """,
                    (
                        item["memory_id"],
                        item.get("memory_type", ""),
                        item.get("source_table", ""),
                        item.get("source_id", ""),
                        item.get("symbol", ""),
                        item.get("timeframe", ""),
                        item.get("regime", ""),
                        item.get("text_summary", ""),
                        _dumps(item.get("structured", {})),
                        _safe_float(item.get("evidence_score")),
                        _safe_float(item.get("similarity_score")),
                        item.get("polarity", "neutral"),
                        _safe_float(item.get("created_at")),
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _experience_memories(self, conn: Any, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "experience_memory"):
            gaps.append("experience_memory")
            return []
        rows = _execute(
            conn,
            """
            SELECT experience_id, trade_id, source_table, source_id, regime_id,
                   outcome_label, reward_score, failure_tags_json,
                   recommended_action, evidence_strength, created_at
            FROM experience_memory
            ORDER BY created_at DESC
            LIMIT 50
            """,
        ).fetchall()
        items = []
        for row in rows:
            tags = _loads(row["failure_tags_json"], [])
            summary = " ".join(
                str(part or "")
                for part in [
                    row["outcome_label"],
                    row["recommended_action"],
                    row["regime_id"],
                    " ".join(str(tag) for tag in tags),
                ]
            ).strip()
            reward = _safe_float(row["reward_score"])
            polarity = "negative" if reward < 0 or tags else "positive" if reward > 0 else "neutral"
            items.append(
                self._item(
                    source_table="experience_memory",
                    source_id=str(row["experience_id"] or ""),
                    memory_type="negative" if polarity == "negative" else "episodic",
                    text_summary=summary or "experience memory",
                    structured={
                        "trade_id": row["trade_id"],
                        "source_table": row["source_table"],
                        "source_id": row["source_id"],
                        "outcome_label": row["outcome_label"],
                        "reward_score": reward,
                        "failure_tags": tags,
                    },
                    evidence_score=max(0.0, min(_safe_float(row["evidence_strength"]), 1.0)),
                    polarity=polarity,
                    created_at=_safe_float(row["created_at"]),
                    terms=terms,
                    regime=str(row["regime_id"] or ""),
                )
            )
        return items

    def _trade_outcome_memories(self, conn: Any, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "trade_outcome_review"):
            gaps.append("trade_outcome_review")
            return []
        rows = _execute(
            conn,
            """
            SELECT review_id, trade_id, position_id, entry_decision_id, pnl,
                   outcome_label, failure_tags_json, summary_text, review_json, created_at
            FROM trade_outcome_review
            ORDER BY created_at DESC
            LIMIT 50
            """,
        ).fetchall()
        items = []
        for row in rows:
            tags = _loads(row["failure_tags_json"], [])
            review = _loads(row["review_json"], {})
            pnl = _safe_float(row["pnl"])
            summary = " ".join(
                str(part or "")
                for part in [row["outcome_label"], row["summary_text"], " ".join(str(tag) for tag in tags)]
            ).strip()
            polarity = "negative" if pnl < 0 or tags else "positive" if pnl > 0 else "neutral"
            items.append(
                self._item(
                    source_table="trade_outcome_review",
                    source_id=str(row["review_id"] or ""),
                    memory_type="negative" if polarity == "negative" else "episodic",
                    text_summary=summary or "trade outcome review",
                    structured={
                        "trade_id": row["trade_id"],
                        "position_id": row["position_id"],
                        "entry_decision_id": row["entry_decision_id"],
                        "pnl": pnl,
                        "outcome_label": row["outcome_label"],
                        "failure_tags": tags,
                        "review": review,
                    },
                    evidence_score=0.75,
                    polarity=polarity,
                    created_at=_safe_float(row["created_at"]),
                    terms=terms,
                )
            )
        return items

    def _policy_suggestion_memories(self, conn: Any, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "policy_suggestion"):
            gaps.append("policy_suggestion")
            return []
        rows = _execute(
            conn,
            """
            SELECT suggestion_id, scope_type, scope_key, action, confidence,
                   reason, evidence_json, status, created_at
            FROM policy_suggestion
            ORDER BY created_at DESC
            LIMIT 50
            """,
        ).fetchall()
        items = []
        for row in rows:
            status = str(row["status"] or "")
            action = str(row["action"] or "")
            polarity = "negative" if status in {"rolled_back", "blocked_by_risk"} else "neutral"
            summary = f"{row['scope_type']} {row['scope_key']} {action} {status} {row['reason']}"
            items.append(
                self._item(
                    source_table="policy_suggestion",
                    source_id=str(row["suggestion_id"] or ""),
                    memory_type="procedural",
                    text_summary=summary,
                    structured={
                        "scope_type": row["scope_type"],
                        "scope_key": row["scope_key"],
                        "action": action,
                        "status": status,
                        "evidence": _loads(row["evidence_json"], {}),
                    },
                    evidence_score=max(0.0, min(_safe_float(row["confidence"]), 1.0)),
                    polarity=polarity,
                    created_at=_safe_float(row["created_at"]),
                    terms=terms,
                )
            )
        return items

    def _model_permission_memories(self, conn: Any, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        if not state_table_exists(conn, "model_permission_audit"):
            gaps.append("model_permission_audit")
            return []
        rows = _execute(
            conn,
            """
            SELECT audit_id, model_type, status, reason, capabilities_json,
                   violations_json, context_json, created_at
            FROM model_permission_audit
            ORDER BY created_at DESC
            LIMIT 30
            """,
        ).fetchall()
        items = []
        for row in rows:
            status = str(row["status"] or "")
            polarity = "negative" if status == "blocked" else "neutral"
            summary = f"{row['model_type']} permission {status} {row['reason']}"
            items.append(
                self._item(
                    source_table="model_permission_audit",
                    source_id=str(row["audit_id"] or ""),
                    memory_type="semantic" if polarity != "negative" else "negative",
                    text_summary=summary,
                    structured={
                        "model_type": row["model_type"],
                        "status": status,
                        "reason": row["reason"],
                        "capabilities": _loads(row["capabilities_json"], {}),
                        "violations": _loads(row["violations_json"], []),
                        "context": _loads(row["context_json"], {}),
                    },
                    evidence_score=0.9 if polarity == "negative" else 0.65,
                    polarity=polarity,
                    created_at=_safe_float(row["created_at"]),
                    terms=terms,
                )
            )
        return items

    def _shadow_audit_memories(self, conn: Any, terms: set[str], gaps: list[str]) -> list[dict[str, Any]]:
        items = []
        for table, spec in self.SHADOW_TABLES.items():
            if not state_table_exists(conn, table):
                gaps.append(table)
                continue
            rows = _execute(
                conn,
                f"""
                SELECT *
                FROM {table}
                ORDER BY created_at DESC
                LIMIT 20
                """,
            ).fetchall()
            for row in rows:
                score = _safe_float(row[spec["score"]]) if spec["score"] in row.keys() else 0.0
                risk = _safe_float(row[spec["risk"]]) if spec["risk"] in row.keys() else 0.0
                source_id = str(row[spec["id"]] or "")
                summary = f"{spec['summary']} score={score:.3f} risk={risk:.3f}"
                polarity = "negative" if risk >= 0.65 else "positive" if score >= 0.65 else "neutral"
                items.append(
                    self._item(
                        source_table=table,
                        source_id=source_id,
                        memory_type="semantic",
                        text_summary=summary,
                        structured={key: row[key] for key in row.keys() if key.endswith("_id") or key in {"model_type", "factor", "mode"}},
                        evidence_score=max(score, risk, 0.25),
                        polarity=polarity,
                        created_at=_safe_float(row["created_at"]),
                        terms=terms,
                    )
                )
        return items

    @staticmethod
    def _query_terms(world_model: dict[str, Any], hypotheses: list[dict[str, Any]]) -> set[str]:
        tokens = {
            str(world_model.get("market_regime") or ""),
            str(world_model.get("strategy_posture") or ""),
            str(world_model.get("factor_posture") or ""),
            str(world_model.get("execution_posture") or ""),
            str(world_model.get("learning_posture") or ""),
            str(world_model.get("autonomy_posture") or ""),
            str(world_model.get("incident_mode") or ""),
        }
        tokens.update(str(item) for item in world_model.get("stale_governance_tables") or [])
        for hypothesis in hypotheses:
            tokens.add(str(hypothesis.get("scope") or ""))
            tokens.update(str(hypothesis.get("claim") or "").lower().replace(";", " ").split())
        return {token.lower() for token in tokens if token and len(token) >= 3}

    @staticmethod
    def _similarity(text: str, terms: set[str]) -> float:
        if not terms:
            return 0.0
        haystack = text.lower()
        hits = sum(1 for term in terms if term in haystack)
        return round(min(1.0, hits / max(3, min(len(terms), 12))), 4)

    def _item(
        self,
        *,
        source_table: str,
        source_id: str,
        memory_type: str,
        text_summary: str,
        structured: dict[str, Any],
        evidence_score: float,
        polarity: str,
        created_at: float,
        terms: set[str],
        symbol: str = "",
        timeframe: str = "",
        regime: str = "",
    ) -> dict[str, Any]:
        similarity = self._similarity(" ".join([text_summary, _dumps(structured), regime]), terms)
        return {
            "memory_id": _memory_id(source_table, source_id),
            "schema_version": "brain_memory_item.v1",
            "memory_type": memory_type,
            "source_table": source_table,
            "source_id": source_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "regime": regime,
            "text_summary": text_summary,
            "structured": structured,
            "evidence_score": round(max(0.0, min(float(evidence_score), 1.0)), 4),
            "similarity_score": similarity,
            "polarity": polarity,
            "created_at": created_at,
        }

    @staticmethod
    def _row_to_item(row: Any) -> dict[str, Any]:
        return {
            "memory_id": str(row["memory_id"] or ""),
            "schema_version": "brain_memory_item.v1",
            "memory_type": str(row["memory_type"] or ""),
            "source_table": str(row["source_table"] or ""),
            "source_id": str(row["source_id"] or ""),
            "symbol": str(row["symbol"] or ""),
            "timeframe": str(row["timeframe"] or ""),
            "regime": str(row["regime"] or ""),
            "text_summary": str(row["text_summary"] or ""),
            "structured": _loads(row["structured_json"], {}),
            "evidence_score": _safe_float(row["evidence_score"]),
            "similarity_score": _safe_float(row["similarity_score"]),
            "polarity": str(row["polarity"] or "neutral"),
            "created_at": _safe_float(row["created_at"]),
            "last_used_at": _safe_float(row["last_used_at"]),
        }
