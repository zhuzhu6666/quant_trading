from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import STATE_DB, STATE_DB_DDL


class RuleEvolutionGovernor:
    """Govern rule-learning suggestions through approval, rollback, and audit."""

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

    def list_suggestions(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        sql = """
            SELECT suggestion_id, scope_type, scope_key, action, confidence, reason,
                   evidence_json, status, reviewed_at, review_note, created_at
            FROM policy_suggestion
        """
        params: list = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["evidence"] = json.loads(item.pop("evidence_json") or "{}")
            except Exception:
                item["evidence"] = {}
            result.append(item)
        return result

    def review_pending(self) -> dict[str, int]:
        """Auto-review proposed suggestions using accumulated pattern stats."""
        approved = 0
        rejected = 0
        unchanged = 0
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM policy_suggestion
                WHERE status='proposed'
                ORDER BY created_at ASC
                """
            ).fetchall()
            now = time.time()
            for row in rows:
                scope_type = str(row["scope_type"] or "")
                scope_key = str(row["scope_key"] or "")
                action = str(row["action"] or "watch")
                confidence = float(row["confidence"] or 0.0)
                stats = conn.execute(
                    """
                    SELECT * FROM experience_pattern_stats
                    WHERE scope_type=? AND scope_key=?
                    """,
                    (scope_type, scope_key),
                ).fetchone()
                if not stats:
                    unchanged += 1
                    continue

                sample_count = int(stats["sample_count"] or 0)
                win_count = int(stats["win_count"] or 0)
                bad_loss_count = int(stats["bad_loss_count"] or 0)
                avg_reward = float(stats["avg_reward"] or 0.0)
                note = ""
                status = "proposed"

                if action == "downweight":
                    if sample_count >= 3 and bad_loss_count >= 2 and avg_reward <= -0.20 and confidence >= 0.45:
                        status = "approved"
                        note = f"approved by governor: samples={sample_count}, avg_reward={avg_reward:.3f}"
                    elif sample_count >= 4 and avg_reward >= -0.05:
                        status = "rejected"
                        note = f"rejected by governor: negative evidence too weak avg_reward={avg_reward:.3f}"
                elif action == "boost_small":
                    if sample_count >= 4 and win_count >= 3 and avg_reward >= 0.20 and confidence >= 0.40:
                        status = "approved"
                        note = f"approved by governor: win_count={win_count}, avg_reward={avg_reward:.3f}"
                    elif sample_count >= 4 and avg_reward <= 0.05:
                        status = "rejected"
                        note = f"rejected by governor: positive evidence too weak avg_reward={avg_reward:.3f}"
                elif action == "watch":
                    if sample_count < 6:
                        status = "approved"
                        note = f"approved by governor: watch-only observation, samples={sample_count}"
                    elif sample_count >= 6:
                        status = "rejected"
                        note = "watch suggestion expired after sufficient observation window"

                if status == "proposed" and now - float(row["created_at"] or 0.0) > 14 * 86400:
                    status = "rejected"
                    note = "stale suggestion auto-rejected after 14 days"

                if status == "approved":
                    approved += 1
                elif status == "rejected":
                    rejected += 1
                else:
                    unchanged += 1
                    continue

                conn.execute(
                    """
                    UPDATE policy_suggestion
                    SET status=?, reviewed_at=?, review_note=?
                    WHERE suggestion_id=?
                    """,
                    (status, now, note, row["suggestion_id"]),
                )
        return {"approved": approved, "rejected": rejected, "unchanged": unchanged}

    def reconcile_active(self) -> dict[str, int]:
        """Rollback approved suggestions if later evidence flips against them."""
        rolled_back = 0
        kept = 0
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM policy_suggestion
                WHERE status='approved'
                ORDER BY created_at ASC
                """
            ).fetchall()
            now = time.time()
            for row in rows:
                stats = conn.execute(
                    """
                    SELECT * FROM experience_pattern_stats
                    WHERE scope_type=? AND scope_key=?
                    """,
                    (row["scope_type"], row["scope_key"]),
                ).fetchone()
                if not stats:
                    kept += 1
                    continue
                sample_count = int(stats["sample_count"] or 0)
                avg_reward = float(stats["avg_reward"] or 0.0)
                action = str(row["action"] or "watch")
                should_rollback = False
                note = ""
                if action == "downweight" and sample_count >= 5 and avg_reward >= 0.12:
                    should_rollback = True
                    note = f"rolled back: factor recovered avg_reward={avg_reward:.3f}"
                elif action == "boost_small" and sample_count >= 5 and avg_reward <= -0.08:
                    should_rollback = True
                    note = f"rolled back: factor deteriorated avg_reward={avg_reward:.3f}"

                if should_rollback:
                    conn.execute(
                        """
                        UPDATE policy_suggestion
                        SET status='rolled_back', reviewed_at=?, review_note=?
                        WHERE suggestion_id=?
                        """,
                        (now, note, row["suggestion_id"]),
                    )
                    rolled_back += 1
                else:
                    kept += 1
        return {"rolled_back": rolled_back, "kept": kept}

    def set_status(self, suggestion_id: str, status: str, note: str = "") -> bool:
        if status not in {"approved", "rejected", "rolled_back", "proposed"}:
            raise ValueError(f"unsupported status: {status}")
        with self._conn() as conn:
            cur = conn.execute(
                """
                UPDATE policy_suggestion
                SET status=?, reviewed_at=?, review_note=?
                WHERE suggestion_id=?
                """,
                (status, time.time(), note, suggestion_id),
            )
            return cur.rowcount > 0

    def log_application(
        self,
        *,
        scope_type: str,
        scope_key: str,
        action: str,
        bias_multiplier: float,
        old_weight: float,
        new_weight: float,
        suggestion_ids: list[str],
        cycle_ts: float,
        status: str = "applied",
        details: dict | None = None,
    ) -> str:
        application_id = self._new_id("lapp")
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO learning_application_log
                (application_id, cycle_ts, scope_type, scope_key, action, bias_multiplier,
                 old_weight, new_weight, suggestion_ids_json, status, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    float(cycle_ts),
                    scope_type,
                    scope_key,
                    action,
                    float(bias_multiplier),
                    float(old_weight),
                    float(new_weight),
                    json.dumps(suggestion_ids, ensure_ascii=False),
                    status,
                    json.dumps(details or {}, ensure_ascii=False, default=str),
                    time.time(),
                ),
            )
        return application_id
