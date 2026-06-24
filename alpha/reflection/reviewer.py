from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from backend.core.db import STATE_DB, STATE_DB_DDL


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


class TradeReviewer:
    """Rule-based post-trade reviewer."""

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
    ) -> dict:
        contributions = contributions or {}
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
                    "context_integrity": context_integrity,
                },
            }
        with self._conn() as conn:
            entry = conn.execute(
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
            entry_factors = list(
                conn.execute(
                    """
                    SELECT * FROM decision_factor_snapshot
                    WHERE decision_id=?
                    ORDER BY ABS(contribution_score) DESC, factor ASC
                    """,
                    (entry_decision_id,),
                )
            ) if entry_decision_id else []

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
            outcome_label = "bad_loss" if conviction >= 0.55 else "good_loss"
            if outcome_label == "bad_loss":
                failure_tags.append("bad_loss")
            else:
                failure_tags.append("good_loss")
            if pos_mc > 0 and neg_mc < 0:
                failure_tags.append("factor_conflict")
            if worst_factor:
                if worst_factor == top_weight_factor and abs(top_weight) >= 0.05:
                    failure_tags.append("overweight_noise_factor")
                elif conviction >= 0.70:
                    failure_tags.append("regime_mismatch")

        if not failure_tags and pnl <= 0:
            failure_tags.append("unavoidable_noise")

        entry_quality = _clamp(0.55 + (0.25 if pnl > 0 else -0.30) * min(abs(entry_score), 1.0))
        hold_quality = _clamp(0.55 if pnl > 0 else 0.40)
        exit_quality = _clamp(0.55 if real_pnl else 0.45)
        regime_fit_score = _clamp(0.70 if pnl > 0 else 0.35 + (0.10 if "good_loss" in failure_tags else 0.0))
        execution_quality = _clamp(0.60 if real_pnl else 0.45)
        mae = abs(min(float(pnl), 0.0))
        mfe = max(float(pnl), 0.0)

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
            "position_id": position_id,
            "trade_id": trade_id,
            "entry_decision_id": entry_decision_id,
            "exit_decision_id": exit_decision_id,
            "entry_score": entry_score,
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
            "context_integrity": context_integrity,
            "failure_tags": failure_tags,
            "factor_contributions": contributions,
        }

        review_id = self._new_id("review")
        with self._conn() as conn:
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
                    time.time(),
                ),
            )
            for factor, mc in contributions.items():
                entry_contribution = 0.0
                for row in entry_factors:
                    if str(row["factor"]) == factor:
                        entry_contribution = float(row["contribution_score"] or 0.0)
                        break
                conn.execute(
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
                        "rule_review",
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
