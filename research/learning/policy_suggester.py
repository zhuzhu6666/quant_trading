from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite, get_state_pg_conn, is_state_db_path


class PolicySuggester:
    """Conservative rule-based learning suggester."""

    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _p(self) -> str:
        return "%s" if self._use_pg() else "?"

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

    def suggest_from_experience(self, experience: dict) -> dict | None:
        primary_factor = str(experience.get("primary_factor", "") or "")
        if not primary_factor:
            return None

        reward = float(experience.get("reward_score", 0.0) or 0.0)
        outcome_label = str(experience.get("outcome_label", "") or "")
        failure_tags = list(experience.get("failure_tags", []) or [])
        recommended_action = str(experience.get("recommended_action", "") or "")
        supervisor_entry_failure = bool(
            "supervisor_thesis_broken" in failure_tags
            and "supervisor_entry_feedback" in failure_tags
            and recommended_action == "downweight"
        )
        now = time.time()
        with self._conn() as conn:
            p = self._p()
            row = conn.execute(
                f"""
                SELECT * FROM experience_pattern_stats
                WHERE scope_type='factor' AND scope_key={p}
                """,
                (primary_factor,),
            ).fetchone()
            if row:
                sample_count = int(row["sample_count"]) + 1
                win_count = int(row["win_count"]) + (1 if reward > 0 else 0)
                bad_loss_count = int(row["bad_loss_count"]) + (1 if outcome_label == "bad_loss" or supervisor_entry_failure else 0)
                prev_avg = float(row["avg_reward"] or 0.0)
                avg_reward = prev_avg + (reward - prev_avg) / max(sample_count, 1)
            else:
                sample_count = 1
                win_count = 1 if reward > 0 else 0
                bad_loss_count = 1 if outcome_label == "bad_loss" or supervisor_entry_failure else 0
                avg_reward = reward

            if sample_count >= 3 and avg_reward <= -0.20:
                action = "downweight"
                confidence = min(0.95, 0.45 + 0.08 * sample_count + 0.10 * bad_loss_count)
                if supervisor_entry_failure:
                    reason = f"factor {primary_factor} repeatedly led to supervisor thesis-broken exits ({sample_count} samples)"
                else:
                    reason = f"factor {primary_factor} shows repeated negative outcomes ({sample_count} samples)"
            elif sample_count >= 4 and win_count >= 3 and avg_reward >= 0.22:
                action = "boost_small"
                confidence = min(0.85, 0.40 + 0.05 * sample_count)
                reason = f"factor {primary_factor} shows stable positive outcomes ({sample_count} samples)"
            else:
                action = "watch"
                confidence = 0.0
                reason = f"factor {primary_factor} still accumulating evidence"

            conn.execute(
                f"""
                INSERT INTO experience_pattern_stats
                (scope_type, scope_key, sample_count, win_count, bad_loss_count,
                 avg_reward, last_outcome_label, recommended_action, updated_at)
                VALUES ('factor', {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p})
                ON CONFLICT(scope_type, scope_key) DO UPDATE SET
                    sample_count=excluded.sample_count,
                    win_count=excluded.win_count,
                    bad_loss_count=excluded.bad_loss_count,
                    avg_reward=excluded.avg_reward,
                    last_outcome_label=excluded.last_outcome_label,
                    recommended_action=excluded.recommended_action,
                    updated_at=excluded.updated_at
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

            if action == "watch":
                return None

            payload = {
                "source_table": experience.get("source_table", ""),
                "source_id": experience.get("source_id", ""),
                "append_source": experience.get("append_source", ""),
                "sample_count": sample_count,
                "win_count": win_count,
                "bad_loss_count": bad_loss_count,
                "avg_reward": round(avg_reward, 6),
                "experience_id": experience.get("experience_id", ""),
                "failure_tags": experience.get("failure_tags", []),
                "supervisor_entry_failure": supervisor_entry_failure,
            }
            existing = conn.execute(
                f"""
                SELECT suggestion_id
                FROM policy_suggestion
                WHERE scope_type='factor' AND scope_key={p} AND action={p} AND status='proposed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (primary_factor, action),
            ).fetchone()
            if existing:
                suggestion_id = str(existing["suggestion_id"])
                conn.execute(
                    f"""
                    UPDATE policy_suggestion
                    SET confidence={p}, reason={p}, evidence_json={p}, created_at={p}
                    WHERE suggestion_id={p}
                    """,
                    (
                        round(confidence, 6),
                        reason,
                        json.dumps(payload, ensure_ascii=False, default=str),
                        now,
                        suggestion_id,
                    ),
                )
            else:
                suggestion_id = self._new_id("psg")
                conn.execute(
                    f"""
                    INSERT INTO policy_suggestion
                    (suggestion_id, scope_type, scope_key, action, confidence, reason,
                     evidence_json, status, created_at)
                    VALUES ({p}, 'factor', {p}, {p}, {p}, {p}, {p}, 'proposed', {p})
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
