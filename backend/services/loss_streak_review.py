"""Forced loss-review statement for the loss-streak probation ladder.

When the backend's daily-loss circuit trips, the ladder locks the current
session and expects the learning loop to produce an explicit statement
before the next session opens:

- ``tighten`` : concrete tightening actions derived from today's experience
                memory / pattern stats (weak-entry clusters, factor
                downweights).  Only risk-*reducing* moves are eligible.
- ``no_change``: the losses show no shared root cause; recorded as a
                deliberate "normal variance" verdict.

The statement is written to ``runtime_kv["loss_streak_review_statement"]``
so the backend process can pick it up on its next ladder evaluation.  If
this module fails entirely, the ladder degrades to the legacy behaviour
(next-session unlock without a statement) — never a stuck lock.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any


SCHEMA_VERSION = "loss_streak_review_statement.v1"
KV_KEY = "loss_streak_review_statement"
STATEMENT_GRACE_SECONDS = 5400.0  # 90 min: lock never waits longer than this


def statement_grace_seconds() -> float:
    return STATEMENT_GRACE_SECONDS


def build_loss_review_statement(
    *,
    trip_date: str,
    trade_pnls: list[float],
    failure_tags: list[str],
    weak_entry_loss_count: int,
    total_loss_count: int,
) -> dict[str, Any]:
    """Derive one explicit statement from observed facts (pure function)."""
    net = float(sum(trade_pnls or []))
    losers = [float(p) for p in (trade_pnls or []) if float(p or 0.0) < 0.0]
    # A shared root cause exists when most losses carry the same tag family.
    dominant_share = (
        (weak_entry_loss_count / total_loss_count)
        if total_loss_count > 0
        else 0.0
    )
    if total_loss_count >= 2 and dominant_share >= 0.5:
        action = "tighten"
        summary = (
            f"{total_loss_count} losses, {weak_entry_loss_count} share "
            f"weak_entry root cause ({dominant_share:.0%}); tighten entry "
            "quality gate for probation."
        )
        recommendations = [
            "entry_threshold_addon_applied_by_ladder",
            "watch_weak_signal_cluster",
        ]
    else:
        action = "no_change"
        summary = (
            f"{total_loss_count} losses with mixed/no dominant tag "
            f"(dominant share {dominant_share:.0%}); treat as normal variance."
        )
        recommendations = []
    return {
        "schema_version": SCHEMA_VERSION,
        "trip_date": str(trip_date or ""),
        "action": action,
        "summary": summary,
        "net_pnl": round(net, 2),
        "loser_count": len(losers),
        "dominant_tag_share": round(dominant_share, 3),
        "recommendations": recommendations,
        "produced_at": time.time(),
    }


def persist_loss_review_statement(
    statement: dict[str, Any],
    *,
    connection_factory: Any,
    state_execute: Any,
    now: float | None = None,
) -> bool:
    """Write the statement into runtime_kv via the caller's PG connection."""
    conn = None
    try:
        conn = connection_factory()
        ts = float(now if now is not None else time.time())
        from backend.services.runtime_kv_store import set_on_conn

        set_on_conn(conn, KV_KEY, statement, updated_at=ts, ensure=False)
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


def load_loss_review_statement(
    *, trip_date: str, kv_reader: Any
) -> dict[str, Any] | None:
    """Read the statement matching this trip date (backend side)."""
    try:
        raw = kv_reader(KV_KEY) or {}
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    if str(raw.get("trip_date") or "") != str(trip_date or ""):
        return None
    return raw
