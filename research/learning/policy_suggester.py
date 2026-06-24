from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import STATE_DB, STATE_DB_DDL


class PolicySuggester:
    """Conservative rule-based learning suggester."""

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

    def suggest_from_experience(self, experience: dict) -> dict | None:
        primary_factor = str(experience.get("primary_factor", "") or "")
        if not primary_factor:
            return None

        reward = float(experience.get("reward_score", 0.0) or 0.0)
        outcome_label = str(experience.get("outcome_label", "") or "")
        now = time.time()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT * FROM experience_pattern_stats
                WHERE scope_type='factor' AND scope_key=?
                """,
                (primary_factor,),
            ).fetchone()
            if row:
                sample_count = int(row["sample_count"]) + 1
                win_count = int(row["win_count"]) + (1 if reward > 0 else 0)
                bad_loss_count = int(row["bad_loss_count"]) + (1 if outcome_label == "bad_loss" else 0)
                prev_avg = float(row["avg_reward"] or 0.0)
                avg_reward = prev_avg + (reward - prev_avg) / max(sample_count, 1)
            else:
                sample_count = 1
                win_count = 1 if reward > 0 else 0
                bad_loss_count = 1 if outcome_label == "bad_loss" else 0
                avg_reward = reward

            if sample_count >= 3 and avg_reward <= -0.20:
                action = "downweight"
                confidence = min(0.95, 0.45 + 0.08 * sample_count + 0.10 * bad_loss_count)
                reason = f"factor {primary_factor} shows repeated negative outcomes ({sample_count} samples)"
            elif sample_count >= 5 and avg_reward >= 0.25:
                action = "boost_small"
                confidence = min(0.85, 0.40 + 0.05 * sample_count)
                reason = f"factor {primary_factor} shows stable positive outcomes ({sample_count} samples)"
            else:
                action = "watch"
                confidence = min(0.70, 0.25 + 0.05 * sample_count)
                reason = f"factor {primary_factor} still accumulating evidence"

            conn.execute(
                """
                INSERT OR REPLACE INTO experience_pattern_stats
                (scope_type, scope_key, sample_count, win_count, bad_loss_count,
                 avg_reward, last_outcome_label, recommended_action, updated_at)
                VALUES ('factor', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    primary_factor,
                    sample_count,
                    win_count,
                    bad_loss_count,
                    round(avg_reward, 6),
                    outcome_label,
                    action,
                    now,
                ),
            )

            suggestion_id = self._new_id("psg")
            payload = {
                "sample_count": sample_count,
                "win_count": win_count,
                "bad_loss_count": bad_loss_count,
                "avg_reward": round(avg_reward, 6),
                "experience_id": experience.get("experience_id", ""),
                "failure_tags": experience.get("failure_tags", []),
            }
            conn.execute(
                """
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence, reason,
                 evidence_json, status, created_at)
                VALUES (?, 'factor', ?, ?, ?, ?, ?, 'proposed', ?)
                """,
                (
                    suggestion_id,
                    primary_factor,
                    action,
                    round(confidence, 6),
                    reason,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                ),
            )

        return {
            "suggestion_id": suggestion_id,
            "scope_type": "factor",
            "scope_key": primary_factor,
            "action": action,
            "confidence": float(confidence),
            "reason": reason,
        }
