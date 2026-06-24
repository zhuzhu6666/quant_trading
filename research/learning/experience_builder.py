from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import STATE_DB, STATE_DB_DDL


class ExperienceBuilder:
    """Convert trade reviews into reusable experience samples."""

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
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(STATE_DB_DDL)

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    def build_from_review(self, review: dict) -> dict:
        review_json = review.get("review_json", {}) or {}
        failure_tags = list(review.get("failure_tags", []) or [])
        primary_factor = (
            review_json.get("worst_factor")
            or review_json.get("top_factor")
            or review_json.get("top_weight_factor")
            or ""
        )
        scope_parts = [
            str(review.get("regime_id", "") or ""),
            str(primary_factor or ""),
            str(review.get("outcome_label", "") or ""),
        ]
        setup_hash = hashlib.sha1("|".join(scope_parts).encode("utf-8")).hexdigest()[:16]

        reward_score = 0.0
        pnl = float(review.get("pnl", 0.0) or 0.0)
        if pnl > 0:
            reward_score = min(1.0, pnl / max(abs(pnl), 50.0))
        elif pnl < 0:
            reward_score = -min(1.0, abs(pnl) / max(abs(pnl), 50.0))

        if review.get("outcome_label") == "bad_loss":
            recommended_action = "downweight"
        elif review.get("outcome_label") == "good_win":
            recommended_action = "watch"
        elif review.get("outcome_label") == "lucky_win":
            recommended_action = "watch"
        else:
            recommended_action = "watch"

        evidence_strength = min(1.0, max(0.15, abs(reward_score) + 0.20 * len(failure_tags)))
        context = {
            "position_id": review.get("position_id", ""),
            "trade_id": review.get("trade_id", ""),
            "primary_factor": primary_factor,
            "failure_tags": failure_tags,
            "summary_text": review.get("summary_text", ""),
            "review_json": review_json,
        }
        experience_id = self._new_id("exp")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO experience_memory
                (experience_id, trade_id, regime_id, setup_hash, decision_context_json,
                 outcome_label, reward_score, failure_tags_json, recommended_action,
                 evidence_strength, artifact_version, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v1', ?)
                """,
                (
                    experience_id,
                    str(review.get("trade_id", "")),
                    str(review.get("regime_id", "") or ""),
                    setup_hash,
                    json.dumps(context, ensure_ascii=False, default=str),
                    str(review.get("outcome_label", "")),
                    round(reward_score, 6),
                    json.dumps(failure_tags, ensure_ascii=False),
                    recommended_action,
                    round(evidence_strength, 6),
                    time.time(),
                ),
            )

        return {
            "experience_id": experience_id,
            "trade_id": str(review.get("trade_id", "")),
            "regime_id": str(review.get("regime_id", "") or ""),
            "setup_hash": setup_hash,
            "primary_factor": primary_factor,
            "outcome_label": str(review.get("outcome_label", "")),
            "reward_score": float(reward_score),
            "failure_tags": failure_tags,
            "recommended_action": recommended_action,
            "evidence_strength": float(evidence_strength),
            "decision_context_json": context,
        }
