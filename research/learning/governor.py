from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite, get_state_pg_conn, is_state_db_path


class RuleEvolutionGovernor:
    """Govern rule-learning suggestions through approval, rollback, and audit."""

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

    def _executemany(self, conn, sql: str, seq_of_params):
        if self._use_pg():
            cur = conn.cursor()
            cur.executemany(self._sql(sql), [tuple(params) for params in seq_of_params])
            return cur
        return conn.executemany(sql, seq_of_params)

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
    def _reward_from_review(item: dict) -> float:
        review = item.get("review") or {}
        pnl = float(item.get("pnl", 0.0) or 0.0)
        close_reason = str(review.get("close_reason", "") or "")
        context_integrity = str(review.get("context_integrity", "full") or "full")
        reward = 0.0
        if pnl > 0:
            reward = min(1.0, pnl / max(abs(pnl), 50.0))
        elif pnl < 0:
            reward = -min(1.0, abs(pnl) / max(abs(pnl), 50.0))
        reward_scale = 1.0
        if context_integrity != "full":
            reward_scale *= 0.5
        if close_reason in {"emergency_close", "restart_replay"}:
            reward_scale *= 0.6
        return reward * reward_scale

    @staticmethod
    def _parse_application_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            item["suggestion_ids"] = json.loads(item.pop("suggestion_ids_json") or "[]")
        except Exception:
            item["suggestion_ids"] = []
        try:
            item["details"] = json.loads(item.pop("details_json") or "{}")
        except Exception:
            item["details"] = {}
        return item

    @staticmethod
    def _parse_review_row(row: sqlite3.Row) -> dict:
        item = dict(row)
        try:
            item["failure_tags"] = json.loads(item.pop("failure_tags_json") or "[]")
        except Exception:
            item["failure_tags"] = []
        try:
            item["review"] = json.loads(item.pop("review_json") or "{}")
        except Exception:
            item["review"] = {}
        return item

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
            rows = self._execute(conn, sql, tuple(params)).fetchall()
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
            rows = self._execute(conn,
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
                stats = self._execute(conn,
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
                    status = "rejected"
                    note = f"observation-only factor kept in stats, not promoted to executable suggestion (samples={sample_count})"

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

                self._execute(conn,
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
            rows = self._execute(conn,
                """
                SELECT * FROM policy_suggestion
                WHERE status='approved'
                ORDER BY created_at ASC
                """
            ).fetchall()
            now = time.time()
            for row in rows:
                stats = self._execute(conn,
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
                    self._execute(conn,
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
            cur = self._execute(conn,
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
        suggestion_ids = [str(item) for item in (suggestion_ids or []) if str(item)]
        suggestion_ids_json = json.dumps(sorted(set(suggestion_ids)), ensure_ascii=False)
        details_json = json.dumps(details or {}, ensure_ascii=False, default=str)
        with self._conn() as conn:
            existing = self._execute(conn,
                """
                SELECT application_id, suggestion_ids_json, status
                FROM learning_application_log
                WHERE scope_type=? AND scope_key=? AND action=?
                  AND status IN ('applied', 'observing', 'effective')
                ORDER BY cycle_ts DESC, created_at DESC
                LIMIT 1
                """,
                (scope_type, scope_key, action),
            ).fetchone()
            if existing:
                try:
                    existing_ids = json.dumps(
                        sorted(set(str(item) for item in json.loads(existing["suggestion_ids_json"] or "[]"))),
                        ensure_ascii=False,
                    )
                except Exception:
                    existing_ids = "[]"
                existing_status = str(existing["status"] or "")
                if existing_ids == suggestion_ids_json and existing_status in {"applied", "observing", "effective"}:
                    self._execute(conn,
                        """
                        UPDATE learning_application_log
                        SET status='superseded'
                        WHERE scope_type=? AND scope_key=? AND action=?
                          AND application_id<>?
                          AND status IN ('applied', 'observing', 'effective')
                          AND suggestion_ids_json=?
                        """,
                        (
                            scope_type,
                            scope_key,
                            action,
                            str(existing["application_id"]),
                            suggestion_ids_json,
                        ),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_effect
                        SET status='superseded', updated_at=?
                        WHERE application_id IN (
                            SELECT application_id
                            FROM learning_application_log
                            WHERE scope_type=? AND scope_key=? AND action=?
                              AND application_id<>?
                              AND status='superseded'
                              AND suggestion_ids_json=?
                        )
                        """,
                        (
                            time.time(),
                            scope_type,
                            scope_key,
                            action,
                            str(existing["application_id"]),
                            suggestion_ids_json,
                        ),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_log
                        SET cycle_ts=?, bias_multiplier=?, old_weight=?, new_weight=?, details_json=?
                        WHERE application_id=?
                        """,
                        (
                            float(cycle_ts),
                            float(bias_multiplier),
                            float(old_weight),
                            float(new_weight),
                            details_json,
                            str(existing["application_id"]),
                        ),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_effect
                        SET decision_json=?, updated_at=?
                        WHERE application_id=?
                        """,
                        (
                            json.dumps(
                                {
                                    "suggestion_ids": suggestion_ids,
                                    "bias_multiplier": bias_multiplier,
                                    "old_weight": old_weight,
                                    "new_weight": new_weight,
                                    "details": details or {},
                                },
                                ensure_ascii=False,
                                default=str,
                            ),
                            time.time(),
                            str(existing["application_id"]),
                        ),
                    )
                    return str(existing["application_id"])

            application_id = self._new_id("lapp")
            self._execute(conn,
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
                    suggestion_ids_json,
                    status,
                    details_json,
                    time.time(),
                ),
            )
            self._execute(conn,
                """
                INSERT INTO learning_application_effect
                (application_id, scope_type, scope_key, action, status, decision_json,
                 updated_at, created_at)
                VALUES (?, ?, ?, ?, 'observing', ?, ?, ?)
                ON CONFLICT(application_id) DO UPDATE SET
                    scope_type=excluded.scope_type,
                    scope_key=excluded.scope_key,
                    action=excluded.action,
                    status=excluded.status,
                    decision_json=excluded.decision_json,
                    updated_at=excluded.updated_at
                """,
                (
                    application_id,
                    scope_type,
                    scope_key,
                    action,
                    json.dumps(
                        {
                            "suggestion_ids": suggestion_ids,
                            "bias_multiplier": bias_multiplier,
                            "old_weight": old_weight,
                            "new_weight": new_weight,
                            "details": details or {},
                        },
                        ensure_ascii=False,
                        default=str,
                    ),
                    time.time(),
                    time.time(),
                ),
            )
        return application_id

    def reconcile_application_effects(
        self,
        *,
        min_trades: int = 3,
        observe_trades: int = 5,
        baseline_min_trades: int = 2,
        reward_delta_for_effective: float = 0.08,
        reward_delta_for_bad: float = -0.08,
    ) -> dict[str, int]:
        observed = 0
        rolled_back = 0
        reinforced = 0
        waiting = 0
        template_runtime_sync_needed = False

        with self._conn() as conn:
            rows = self._execute(conn,
                """
                SELECT *
                FROM learning_application_log
                WHERE status IN ('applied', 'observing', 'effective')
                ORDER BY cycle_ts ASC
                """
            ).fetchall()
            now = time.time()

            for row in rows:
                app = self._parse_application_row(row)
                scope_type = str(app.get("scope_type") or "")
                if scope_type not in {"factor", "parameter_template"}:
                    continue
                factor = (
                    str(app.get("scope_key") or "")
                    if scope_type == "factor"
                    else str((app.get("details") or {}).get("factor_id") or str(app.get("scope_key") or "").split(":", 1)[0])
                )
                if not factor:
                    continue

                post_rows = self._execute(conn,
                    """
                    SELECT r.review_id, r.trade_id, r.position_id, r.entry_decision_id, r.pnl,
                           r.outcome_label, r.failure_tags_json, r.summary_text, r.review_json, r.created_at
                    FROM trade_outcome_review r
                    WHERE r.created_at > ?
                      AND EXISTS (
                          SELECT 1
                          FROM decision_factor_snapshot dfs
                          WHERE dfs.decision_id = r.entry_decision_id
                            AND dfs.factor = ?
                      )
                    ORDER BY r.created_at ASC
                    LIMIT ?
                    """,
                    (float(app.get("cycle_ts") or 0.0), factor, int(observe_trades)),
                ).fetchall()
                post_reviews = [self._parse_review_row(r) for r in post_rows]

                pre_rows = self._execute(conn,
                    """
                    SELECT r.review_id, r.trade_id, r.position_id, r.entry_decision_id, r.pnl,
                           r.outcome_label, r.failure_tags_json, r.summary_text, r.review_json, r.created_at
                    FROM trade_outcome_review r
                    WHERE r.created_at <= ?
                      AND EXISTS (
                          SELECT 1
                          FROM decision_factor_snapshot dfs
                          WHERE dfs.decision_id = r.entry_decision_id
                            AND dfs.factor = ?
                      )
                    ORDER BY r.created_at DESC
                    LIMIT ?
                    """,
                    (float(app.get("cycle_ts") or 0.0), factor, int(observe_trades)),
                ).fetchall()
                pre_reviews = [self._parse_review_row(r) for r in pre_rows]

                post_rewards = [self._reward_from_review(r) for r in post_reviews]
                pre_rewards = [self._reward_from_review(r) for r in pre_reviews]
                post_avg = sum(post_rewards) / len(post_rewards) if post_rewards else 0.0
                pre_avg = sum(pre_rewards) / len(pre_rewards) if pre_rewards else 0.0
                delta = post_avg - pre_avg
                post_win_rate = sum(1 for r in post_reviews if float(r.get("pnl", 0.0) or 0.0) > 0) / max(len(post_reviews), 1)
                pre_win_rate = sum(1 for r in pre_reviews if float(r.get("pnl", 0.0) or 0.0) > 0) / max(len(pre_reviews), 1)

                decision = {
                    "application_id": app["application_id"],
                    "scope_type": scope_type,
                    "scope_key": factor,
                    "action": app["action"],
                    "post_review_ids": [r["review_id"] for r in post_reviews],
                    "baseline_review_ids": [r["review_id"] for r in pre_reviews],
                    "post_avg_reward": round(post_avg, 6),
                    "baseline_avg_reward": round(pre_avg, 6),
                    "delta_avg_reward": round(delta, 6),
                    "post_win_rate": round(post_win_rate, 4),
                    "baseline_win_rate": round(pre_win_rate, 4),
                    "baseline_ready": len(pre_reviews) >= baseline_min_trades,
                    "observe_ready": len(post_reviews) >= min_trades,
                }

                next_status = "observing"
                if len(post_reviews) < min_trades or len(pre_reviews) < baseline_min_trades:
                    next_status = "observing"
                elif delta >= reward_delta_for_effective:
                    next_status = "effective"
                elif delta <= reward_delta_for_bad:
                    next_status = "ineffective"
                else:
                    next_status = "mixed"

                self._execute(conn,
                    """
                    INSERT INTO learning_application_effect
                    (application_id, scope_type, scope_key, action, status,
                     observed_trade_count, baseline_trade_count,
                     post_avg_reward, baseline_avg_reward, delta_avg_reward,
                     post_win_rate, baseline_win_rate, decision_json,
                     last_review_at, updated_at, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(application_id) DO UPDATE SET
                        scope_type=excluded.scope_type,
                        scope_key=excluded.scope_key,
                        action=excluded.action,
                        status=excluded.status,
                        observed_trade_count=excluded.observed_trade_count,
                        baseline_trade_count=excluded.baseline_trade_count,
                        post_avg_reward=excluded.post_avg_reward,
                        baseline_avg_reward=excluded.baseline_avg_reward,
                        delta_avg_reward=excluded.delta_avg_reward,
                        post_win_rate=excluded.post_win_rate,
                        baseline_win_rate=excluded.baseline_win_rate,
                        decision_json=excluded.decision_json,
                        last_review_at=excluded.last_review_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        app["application_id"],
                        scope_type,
                        str(app.get("scope_key") or factor),
                        app["action"],
                        next_status,
                        len(post_reviews),
                        len(pre_reviews),
                        round(post_avg, 6),
                        round(pre_avg, 6),
                        round(delta, 6),
                        round(post_win_rate, 4),
                        round(pre_win_rate, 4),
                        json.dumps(decision, ensure_ascii=False, default=str),
                        max((float(r.get("created_at", 0.0) or 0.0) for r in post_reviews), default=0.0),
                        now,
                        now,
                    ),
                )

                self._execute(conn,
                    """
                    UPDATE learning_application_log
                    SET status=?, details_json=?
                    WHERE application_id=?
                    """,
                    (
                        next_status,
                        json.dumps({**(app.get("details") or {}), "effect": decision}, ensure_ascii=False, default=str),
                        app["application_id"],
                    ),
                )
                observed += 1

                if next_status == "observing":
                    waiting += 1
                    continue

                suggestion_ids = list(app.get("suggestion_ids") or [])
                if next_status == "ineffective" and suggestion_ids:
                    self._executemany(conn,
                        """
                        UPDATE policy_suggestion
                        SET status='rolled_back', reviewed_at=?, review_note=?
                        WHERE suggestion_id=?
                        """,
                        [
                            (now, f"auto rollback by application effect delta={delta:.3f}", sid)
                            for sid in suggestion_ids
                        ],
                    )
                    rolled_back += 1
                    if scope_type == "parameter_template":
                        details = app.get("details") or {}
                        factor_id = str(details.get("factor_id") or factor)
                        regime_key = str(details.get("regime_key") or "")
                        old_template_id = str(details.get("old_template_id") or "")
                        new_template_id = str(details.get("new_template_id") or "")
                        if old_template_id:
                            self._execute(conn,
                                """
                                UPDATE parameter_template_registry
                                SET active=CASE WHEN template_id=? THEN 1 ELSE 0 END, updated_at=?
                                WHERE factor_id=? AND regime_key=?
                                """,
                                (old_template_id, now, factor_id, regime_key),
                            )
                            old_row = self._execute(conn,
                                """
                                SELECT template_version FROM parameter_template_registry WHERE template_id=?
                                """,
                                (old_template_id,),
                            ).fetchone()
                            self._execute(conn,
                                """
                                INSERT INTO parameter_template_active
                                (factor_id, regime_key, template_id, template_version, status, suggestion_id,
                                 context_json, activated_at, updated_at)
                                VALUES (?, ?, ?, ?, 'rolled_back', ?, ?, ?, ?)
                                ON CONFLICT(factor_id, regime_key) DO UPDATE SET
                                    template_id=excluded.template_id,
                                    template_version=excluded.template_version,
                                    status=excluded.status,
                                    suggestion_id=excluded.suggestion_id,
                                    context_json=excluded.context_json,
                                    activated_at=excluded.activated_at,
                                    updated_at=excluded.updated_at
                                """,
                                (
                                    factor_id,
                                    regime_key,
                                    old_template_id,
                                    str((old_row["template_version"] if old_row else "") or ""),
                                    suggestion_ids[0],
                                    json.dumps(
                                        {
                                            "rolled_back_from": new_template_id,
                                            "reason": f"application effect delta={delta:.3f}",
                                        },
                                        ensure_ascii=False,
                                        default=str,
                                    ),
                                    now,
                                    now,
                                ),
                            )
                            self._execute(conn,
                                """
                                INSERT INTO parameter_template_switch_log
                                (switch_id, factor_id, regime_key, old_template_id, new_template_id,
                                 suggestion_id, risk_verdict_json, context_json, status, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, '{}', ?, 'rolled_back', ?)
                                """,
                                (
                                    self._new_id("ptsw"),
                                    factor_id,
                                    regime_key,
                                    new_template_id,
                                    old_template_id,
                                    suggestion_ids[0],
                                    json.dumps(
                                        {
                                            "application_id": app["application_id"],
                                            "reason": f"auto rollback by application effect delta={delta:.3f}",
                                        },
                                        ensure_ascii=False,
                                        default=str,
                                    ),
                                    now,
                                ),
                            )
                            template_runtime_sync_needed = True
                elif next_status == "effective" and len(post_reviews) >= observe_trades:
                    suggestion_id = self._new_id("psg")
                    evidence = {
                        "source_application_id": app["application_id"],
                        "sample_count": len(post_reviews),
                        "baseline_sample_count": len(pre_reviews),
                        "post_avg_reward": round(post_avg, 6),
                        "baseline_avg_reward": round(pre_avg, 6),
                        "delta_avg_reward": round(delta, 6),
                    }
                    self._execute(conn,
                        """
                        INSERT INTO policy_suggestion
                        (suggestion_id, scope_type, scope_key, action, confidence, reason,
                         evidence_json, status, reviewed_at, review_note, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'approved', ?, ?, ?)
                        """,
                        (
                            suggestion_id,
                            scope_type,
                            str(app.get("scope_key") or factor),
                            app["action"],
                            min(0.75, 0.35 + max(0.0, delta)),
                            f"auto reinforced by application effect delta={delta:.3f}",
                            json.dumps(evidence, ensure_ascii=False, default=str),
                            now,
                            f"auto reinforce from application {app['application_id']}",
                            now,
                        ),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_effect
                        SET status='reinforced', updated_at=?
                        WHERE application_id=?
                        """,
                        (now, app["application_id"]),
                    )
                    self._execute(conn,
                        """
                        UPDATE learning_application_log
                        SET status='reinforced'
                        WHERE application_id=?
                        """,
                        (app["application_id"],),
                    )
                    reinforced += 1

        if template_runtime_sync_needed:
            try:
                from backend.services.parameter_templates import ParameterTemplateService

                ParameterTemplateService(str(self.db_path)).sync_runtime_config()
            except Exception:
                pass

        return {
            "observed": observed,
            "rolled_back": rolled_back,
            "reinforced": reinforced,
            "waiting": waiting,
        }
