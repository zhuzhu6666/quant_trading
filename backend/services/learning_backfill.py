from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid

from backend.core.db import get_state_conn


logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 100
_DEFAULT_DELAY_SEC = 180.0
_backfill_thread: threading.Thread | None = None


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def classify_outcome(entry_score: float, pnl: float) -> str:
    if pnl > 0:
        return "lucky_win"
    conviction = abs(float(entry_score or 0.0))
    return "bad_loss" if conviction >= 0.55 else "good_loss"


def fetch_missing_positions(
    conn: sqlite3.Connection,
    *,
    limit: int = _DEFAULT_LIMIT,
    require_decision: bool = True,
) -> list[sqlite3.Row]:
    sql = """
    WITH close_positions AS (
        SELECT
            position_id,
            MAX(exec_timestamp) AS close_ts,
            SUM(COALESCE(gross_profit, 0) + COALESCE(swap, 0) - COALESCE(commission, 0)) AS net_pnl,
            MAX(entry_price) AS entry_price,
            MAX(exec_price) AS exec_price,
            MAX(balance) AS balance,
            MAX(deal_id) AS deal_id,
            MAX(ABS(commission)) AS close_commission,
            MAX(gross_profit) AS gross_profit,
            MAX(swap) AS swap
        FROM ctrader_deals
        WHERE is_close = 1
        GROUP BY position_id
    ),
    missing AS (
        SELECT c.*
        FROM close_positions c
        LEFT JOIN trade_outcome_review r
            ON CAST(r.position_id AS INTEGER) = c.position_id
        WHERE r.position_id IS NULL
    )
    SELECT
        m.position_id,
        m.close_ts,
        m.net_pnl,
        m.entry_price,
        m.exec_price,
        m.balance,
        m.deal_id,
        m.close_commission,
        m.gross_profit,
        m.swap,
        d.decision_id AS entry_decision_id,
        d.trade_id,
        d.regime_id,
        d.action_score AS entry_score,
        d.decision_ts AS entry_ts,
        d.symbol,
        d.timeframe
    FROM missing m
    LEFT JOIN decision_ledger d
        ON d.position_id = CAST(m.position_id AS TEXT) AND d.event_type = 'open'
    """
    params: list[object] = []
    if require_decision:
        sql += " WHERE d.decision_id IS NOT NULL"
    sql += " ORDER BY m.close_ts DESC LIMIT ?"
    params.append(int(limit))
    try:
        return list(conn.execute(sql, params).fetchall())
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            logger.warning("[learning_backfill] skipped: required tables missing: %s", exc)
            return []
        raise


def build_review_record(row: sqlite3.Row) -> dict:
    position_id = str(row["position_id"])
    trade_id = str(row["trade_id"] or position_id)
    pnl = float(row["net_pnl"] or 0.0)
    entry_score = float(row["entry_score"] or 0.0)
    entry_ts = float(row["entry_ts"] or 0.0)
    close_ts = float(row["close_ts"] or 0.0)
    holding_seconds = max(0.0, close_ts - entry_ts) if entry_ts > 0 and close_ts > 0 else 0.0
    outcome_label = classify_outcome(entry_score, pnl)
    summary = (
        f"trade {position_id} closed pnl={pnl:.2f}; "
        f"outcome={outcome_label}; "
        f"primary_factor=n/a; worst_factor=n/a"
    )
    real_pnl = {
        "gross": float(row["gross_profit"] or 0.0),
        "swap": float(row["swap"] or 0.0),
        "commission": float(row["close_commission"] or 0.0),
        "net": pnl,
        "entry_price": float(row["entry_price"] or 0.0),
        "exec_price": float(row["exec_price"] or 0.0),
        "balance": float(row["balance"] or 0.0),
        "deal_id": int(row["deal_id"] or 0),
        "exec_timestamp": float(row["close_ts"] or 0.0),
    }
    review_json = {
        "position_id": position_id,
        "trade_id": trade_id,
        "entry_decision_id": str(row["entry_decision_id"] or ""),
        "exit_decision_id": "",
        "entry_ts": entry_ts,
        "close_ts": close_ts,
        "holding_seconds": round(holding_seconds, 3),
        "holding_minutes": round(holding_seconds / 60.0, 3),
        "timeframe": str(row["timeframe"] or ""),
        "entry_score": entry_score,
        "top_weight_factor": "",
        "top_weight": 0.0,
        "top_factor": "",
        "top_factor_mc": 0.0,
        "worst_factor": "",
        "worst_factor_mc": 0.0,
        "positive_share": 0.0,
        "close_price": float(row["exec_price"] or 0.0),
        "real_pnl": real_pnl,
        "close_reason": "historical_backfill",
        "context_integrity": "full",
        "failure_tags": [outcome_label],
        "factor_contributions": {},
    }
    return {
        "review_id": new_id("review"),
        "trade_id": trade_id,
        "position_id": position_id,
        "entry_decision_id": str(row["entry_decision_id"] or ""),
        "exit_decision_id": "",
        "entry_quality": round(0.55 + (0.25 if pnl > 0 else -0.30) * min(abs(entry_score), 1.0), 4),
        "hold_quality": 0.55 if pnl > 0 else 0.40,
        "exit_quality": 0.55,
        "regime_fit_score": 0.70 if pnl > 0 else (0.35 + (0.10 if outcome_label == "good_loss" else 0.0)),
        "execution_quality": 0.60,
        "pnl": round(pnl, 6),
        "mae": round(abs(min(pnl, 0.0)), 6),
        "mfe": round(max(pnl, 0.0), 6),
        "outcome_label": outcome_label,
        "failure_tags_json": json.dumps([outcome_label], ensure_ascii=False),
        "summary_text": summary,
        "review_json": json.dumps(review_json, ensure_ascii=False, default=str),
        "created_at": float(row["close_ts"] or time.time()),
    }


def insert_review(conn: sqlite3.Connection, record: dict) -> None:
    conn.execute(
        """
        INSERT INTO trade_outcome_review
        (review_id, trade_id, position_id, entry_decision_id, exit_decision_id,
         entry_quality, hold_quality, exit_quality, regime_fit_score,
         execution_quality, pnl, mae, mfe, outcome_label,
         failure_tags_json, summary_text, review_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["review_id"],
            record["trade_id"],
            record["position_id"],
            record["entry_decision_id"],
            record["exit_decision_id"],
            record["entry_quality"],
            record["hold_quality"],
            record["exit_quality"],
            record["regime_fit_score"],
            record["execution_quality"],
            record["pnl"],
            record["mae"],
            record["mfe"],
            record["outcome_label"],
            record["failure_tags_json"],
            record["summary_text"],
            record["review_json"],
            record["created_at"],
        ),
    )


def rebuild_learning_state(conn: sqlite3.Connection) -> tuple[int, int]:
    conn.execute("DELETE FROM experience_memory")
    conn.execute("DELETE FROM experience_pattern_stats")
    conn.execute("DELETE FROM policy_suggestion")
    reviews = conn.execute(
        """
        SELECT review_id, trade_id, position_id, outcome_label, pnl, failure_tags_json,
               summary_text, review_json, created_at
        FROM trade_outcome_review
        ORDER BY created_at ASC
        """
    ).fetchall()
    stats: dict[str, dict] = {}
    suggestions_created = 0
    rebuilt = 0
    now = time.time()

    for row in reviews:
        review_json = json.loads(row["review_json"] or "{}")
        failure_tags = list(json.loads(row["failure_tags_json"] or "[]"))
        outcome_label = str(row["outcome_label"] or "")
        pnl = float(row["pnl"] or 0.0)
        close_reason = str(review_json.get("close_reason") or "")
        context_integrity = str(review_json.get("context_integrity", "full") or "full")
        top_weight_factor = str(review_json.get("top_weight_factor") or "")
        top_factor = str(review_json.get("top_factor") or "")
        worst_factor = str(review_json.get("worst_factor") or "")

        def actionable(name: str) -> bool:
            return bool(name) and not name.startswith("dsl_auto_")

        if outcome_label in {"bad_loss", "good_loss"}:
            primary_factor = worst_factor if actionable(worst_factor) else (top_weight_factor or top_factor or worst_factor)
        else:
            primary_factor = top_weight_factor or top_factor or worst_factor
            if not actionable(primary_factor):
                primary_factor = top_weight_factor or top_factor or worst_factor

        reward_score = 0.0
        if pnl > 0:
            reward_score = min(1.0, pnl / max(abs(pnl), 50.0))
        elif pnl < 0:
            reward_score = -min(1.0, abs(pnl) / max(abs(pnl), 50.0))
        reward_scale = 1.0
        evidence_scale = 1.0
        if context_integrity != "full":
            reward_scale *= 0.5
            evidence_scale *= 0.35
        if close_reason in {"emergency_close", "restart_replay"}:
            reward_scale *= 0.6
            evidence_scale *= 0.5
        reward_score *= reward_scale

        if context_integrity != "full" and "partial_context" not in failure_tags:
            failure_tags.append("partial_context")
        if close_reason == "emergency_close" and "manual_intervention" not in failure_tags:
            failure_tags.append("manual_intervention")
        if close_reason == "restart_replay" and "restart_replay" not in failure_tags:
            failure_tags.append("restart_replay")

        recommended_action = "downweight" if outcome_label == "bad_loss" else "watch"
        if context_integrity != "full" or close_reason in {"emergency_close", "restart_replay"}:
            recommended_action = "watch"
        evidence_strength = min(1.0, max(0.15, abs(reward_score) + 0.20 * len(failure_tags)))
        evidence_strength = max(0.05, evidence_strength * evidence_scale)

        setup_hash = hashlib.sha1(f"|{primary_factor}|{outcome_label}".encode("utf-8")).hexdigest()[:16]
        experience_id = new_id("exp")
        context = {
            "position_id": str(row["position_id"] or ""),
            "trade_id": str(row["trade_id"] or ""),
            "primary_factor": primary_factor,
            "failure_tags": failure_tags,
            "close_reason": close_reason,
            "context_integrity": context_integrity,
            "summary_text": str(row["summary_text"] or ""),
            "review_json": review_json,
        }
        conn.execute(
            """
            INSERT INTO experience_memory
            (experience_id, trade_id, regime_id, setup_hash, decision_context_json,
             outcome_label, reward_score, failure_tags_json, recommended_action,
             evidence_strength, artifact_version, created_at)
            VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?, 'v1', ?)
            """,
            (
                experience_id,
                str(row["trade_id"] or ""),
                setup_hash,
                json.dumps(context, ensure_ascii=False),
                outcome_label,
                round(reward_score, 6),
                json.dumps(failure_tags, ensure_ascii=False),
                recommended_action,
                round(evidence_strength, 6),
                now,
            ),
        )
        rebuilt += 1

        if not primary_factor:
            continue

        stat = stats.get(primary_factor, {"sample_count": 0, "win_count": 0, "bad_loss_count": 0, "avg_reward": 0.0})
        stat["sample_count"] += 1
        stat["win_count"] += 1 if reward_score > 0 else 0
        stat["bad_loss_count"] += 1 if outcome_label == "bad_loss" else 0
        prev_avg = stat["avg_reward"]
        stat["avg_reward"] = prev_avg + (reward_score - prev_avg) / stat["sample_count"]
        stats[primary_factor] = stat

        sample_count = stat["sample_count"]
        avg_reward = stat["avg_reward"]
        bad_loss_count = stat["bad_loss_count"]
        win_count = stat["win_count"]
        if sample_count >= 3 and avg_reward <= -0.20:
            action = "downweight"
            confidence = min(0.95, 0.45 + 0.08 * sample_count + 0.10 * bad_loss_count)
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

        if action != "watch":
            existing = conn.execute(
                """
                SELECT suggestion_id
                FROM policy_suggestion
                WHERE scope_type='factor' AND scope_key=? AND action=? AND status='proposed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (primary_factor, action),
            ).fetchone()
            evidence = {
                "sample_count": sample_count,
                "win_count": win_count,
                "bad_loss_count": bad_loss_count,
                "avg_reward": round(avg_reward, 6),
                "experience_id": experience_id,
                "failure_tags": failure_tags,
            }
            if existing:
                conn.execute(
                    """
                    UPDATE policy_suggestion
                    SET confidence=?, reason=?, evidence_json=?, created_at=?
                    WHERE suggestion_id=?
                    """,
                    (
                        round(confidence, 6),
                        reason,
                        json.dumps(evidence, ensure_ascii=False),
                        now,
                        str(existing["suggestion_id"]),
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO policy_suggestion
                    (suggestion_id, scope_type, scope_key, action, confidence, reason,
                     evidence_json, status, created_at)
                    VALUES (?, 'factor', ?, ?, ?, ?, ?, 'proposed', ?)
                    """,
                    (
                        new_id("psg"),
                        primary_factor,
                        action,
                        round(confidence, 6),
                        reason,
                        json.dumps(evidence, ensure_ascii=False),
                        now,
                    ),
                )
                suggestions_created += 1

    return rebuilt, suggestions_created


def run_learning_backfill(
    *,
    limit: int = _DEFAULT_LIMIT,
    allow_partial: bool = False,
    rebuild_learning: bool = True,
) -> dict:
    conn = get_state_conn()
    try:
        rows = fetch_missing_positions(conn, limit=limit, require_decision=not allow_partial)
        inserted = []
        for row in rows:
            record = build_review_record(row)
            insert_review(conn, record)
            inserted.append(
                {
                    "position_id": record["position_id"],
                    "trade_id": record["trade_id"],
                    "outcome_label": record["outcome_label"],
                    "pnl": record["pnl"],
                }
            )
        rebuilt = 0
        suggestions = 0
        if rebuild_learning and inserted:
            rebuilt, suggestions = rebuild_learning_state(conn)
        conn.commit()
        result = {
            "inserted_reviews": inserted,
            "inserted_count": len(inserted),
            "rebuild_reviews": rebuilt,
            "rebuild_suggestions": suggestions,
            "require_decision": not allow_partial,
        }
        if inserted:
            logger.info("[learning_backfill] inserted %d missing reviews", len(inserted))
        return result
    finally:
        conn.close()


def schedule_learning_backfill(
    *,
    delay_sec: float = _DEFAULT_DELAY_SEC,
    limit: int = _DEFAULT_LIMIT,
    allow_partial: bool = False,
    rebuild_learning: bool = True,
) -> bool:
    global _backfill_thread
    if _backfill_thread is not None and _backfill_thread.is_alive():
        return False

    def _worker() -> None:
        time.sleep(max(0.0, delay_sec))
        try:
            result = run_learning_backfill(
                limit=limit,
                allow_partial=allow_partial,
                rebuild_learning=rebuild_learning,
            )
            logger.info("[learning_backfill] scheduled run completed: %s", result)
        except Exception as exc:
            logger.warning("[learning_backfill] scheduled run failed: %s", exc)

    _backfill_thread = threading.Thread(
        target=_worker,
        name="learning_backfill_startup",
        daemon=True,
    )
    _backfill_thread.start()
    return True
