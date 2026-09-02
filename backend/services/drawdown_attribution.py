"""Session drawdown attribution: aggregate per-trade review evidence into an
account-level diagnosis.

A losing session must produce a stated cause, not just an open freeze.  Each
trade_review already carries responsibility domains, failure labels and
per-factor marginal contributions; this module rolls those up over a trailing
window into ``session.drawdown_attribution.v1`` facts that governance and
reviews can consume.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, is_state_db_path


SCHEMA_VERSION = "session.drawdown_attribution.v1"
KV_KEY = "session.drawdown_attribution.v1"
DEFAULT_WINDOW_HOURS = 24.0
DEFAULT_MAX_REVIEWS = 200
_RESPONSIBILITY_FIELD = "primary_responsibility"
_LABELS_FIELD = "responsibility_labels"
_FACTOR_MC_FIELD = "factor_contributions"


def aggregate_reviews(
    reviews: list[dict[str, Any]],
    *,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    max_reviews: int = DEFAULT_MAX_REVIEWS,
    now: float | None = None,
) -> dict[str, Any]:
    """Aggregate decoded trade_review payloads into a drawdown diagnosis.

    ``reviews`` entries are payload dicts (as returned by
    ``canonical_v2_reader.iter_reviews``).  Pure function: no I/O, unit-testable.
    """
    now_ts = float(now if now is not None else time.time())
    cutoff = now_ts - float(window_hours) * 3600.0
    bounded: list[dict[str, Any]] = []
    for item in list(reviews or [])[-int(max_reviews):]:
        try:
            observed = float(item.get("_observed_at") or 0.0)
        except (TypeError, ValueError):
            observed = 0.0
        if observed and observed < cutoff:
            continue
        bounded.append(item)

    buckets: dict[str, dict[str, Any]] = {}
    factor_mc: dict[str, float] = {}
    total_pnl = 0.0
    losing = 0
    winning = 0
    for review in bounded:
        try:
            pnl = float(review.get("pnl") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        total_pnl += pnl
        if pnl < 0:
            losing += 1
        elif pnl > 0:
            winning += 1
        rv = review.get("review") if isinstance(review.get("review"), dict) else {}
        responsibility = str(
            rv.get(_RESPONSIBILITY_FIELD)
            or review.get("outcome_label")
            or "unknown"
        )
        bucket = buckets.setdefault(
            responsibility,
            {
                "responsibility": responsibility,
                "trade_count": 0,
                "total_pnl": 0.0,
                "losing_count": 0,
                "entry_quality_sum": 0.0,
                "hold_quality_sum": 0.0,
                "labels": {},
            },
        )
        bucket["trade_count"] += 1
        bucket["total_pnl"] += pnl
        if pnl < 0:
            bucket["losing_count"] += 1
        for quality_field, key in (
            ("entry_quality", "entry_quality_sum"),
            ("hold_quality", "hold_quality_sum"),
        ):
            try:
                bucket[key] += float(review.get(quality_field) or 0.0)
            except (TypeError, ValueError):
                pass
        for label in rv.get(_LABELS_FIELD) or []:
            label = str(label)
            bucket["labels"][label] = bucket["labels"].get(label, 0) + 1
        contributions = rv.get(_FACTOR_MC_FIELD)
        if isinstance(contributions, dict):
            for factor, mc in contributions.items():
                try:
                    value = float(mc or 0.0)
                except (TypeError, ValueError):
                    continue
                factor_mc[str(factor)] = factor_mc.get(str(factor), 0.0) + value

    responsibility_buckets = []
    for bucket in buckets.values():
        count = int(bucket["trade_count"])
        out = dict(bucket)
        out["avg_entry_quality"] = round(
            bucket["entry_quality_sum"] / count, 4
        ) if count else 0.0
        out["avg_hold_quality"] = round(
            bucket["hold_quality_sum"] / count, 4
        ) if count else 0.0
        out.pop("entry_quality_sum", None)
        out.pop("hold_quality_sum", None)
        responsibility_buckets.append(out)
    responsibility_buckets.sort(
        key=lambda b: (float(b["total_pnl"]), -int(b["trade_count"]))
    )

    # Factor damage is reported on losing trades only: winners did not hurt.
    losing_factor_mc: dict[str, float] = {}
    for review in bounded:
        try:
            pnl = float(review.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        if pnl >= 0:
            continue
        rv = review.get("review") if isinstance(review.get("review"), dict) else {}
        contributions = rv.get(_FACTOR_MC_FIELD)
        if isinstance(contributions, dict):
            for factor, mc in contributions.items():
                try:
                    value = float(mc or 0.0)
                except (TypeError, ValueError):
                    continue
                losing_factor_mc[str(factor)] = (
                    losing_factor_mc.get(str(factor), 0.0) + value
                )
    factor_damage = sorted(
        (
            {"factor": factor, "loss_weighted_mc": round(value, 4)}
            for factor, value in losing_factor_mc.items()
        ),
        key=lambda item: item["loss_weighted_mc"],
    )[:8]

    primary = responsibility_buckets[0] if responsibility_buckets else None
    top_factor = factor_damage[0] if factor_damage else None
    narrative = _narrative(
        trade_count=len(bounded),
        total_pnl=total_pnl,
        losing=losing,
        winning=winning,
        primary=primary,
        top_factor=top_factor,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "window_hours": float(window_hours),
        "generated_at": now_ts,
        "trade_count": len(bounded),
        "losing_count": losing,
        "winning_count": winning,
        "total_pnl": round(total_pnl, 4),
        "primary_responsibility": (
            str(primary.get("responsibility") or "unknown")
            if primary
            else "no_reviews_in_window"
        ),
        "responsibility_buckets": responsibility_buckets,
        "top_loss_factors": factor_damage,
        "narrative": narrative,
    }


def _narrative(
    *,
    trade_count: int,
    total_pnl: float,
    losing: int,
    winning: int,
    primary: dict[str, Any] | None,
    top_factor: dict[str, Any] | None,
) -> str:
    if trade_count == 0:
        return "no closed-trade reviews in the trailing window; nothing to attribute"
    parts = [
        f"window trades={trade_count} (loss {losing}/win {winning}) "
        f"net pnl=${total_pnl:.2f}"
    ]
    if primary is not None:
        label_summary = sorted(
            primary.get("labels", {}).items(),
            key=lambda kv: -int(kv[1]),
        )[:3]
        labels = ", ".join(
            f"{label} x{count}" for label, count in label_summary
        ) or "none"
        parts.append(
            f"primary responsibility={primary.get('responsibility')} "
            f"({primary.get('trade_count')} trades, "
            f"pnl=${primary.get('total_pnl', 0.0):.2f}; labels: {labels})"
        )
    if top_factor is not None and float(top_factor.get("loss_weighted_mc") or 0) < 0:
        parts.append(
            f"largest loss-side factor={top_factor.get('factor')} "
            f"(loss-weighted mc={top_factor.get('loss_weighted_mc')})"
        )
    return "; ".join(parts)


def load_recent_reviews(
    conn: Any,
    *,
    limit: int = DEFAULT_MAX_REVIEWS,
) -> list[dict[str, Any]]:
    """Load the most recent decoded trade reviews with observed timestamps."""
    from backend.services import canonical_v2_reader as reader

    rows = reader.iter_reviews(conn, limit=limit)
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        item = dict(payload)
        try:
            item["_observed_at"] = reader.observed_epoch(row.get("observed_at"))
        except (TypeError, ValueError):
            item["_observed_at"] = 0.0
        out.append(item)
    return out


def build_drawdown_attribution(
    *,
    db_path: str | Path = STATE_DB,
    window_hours: float = DEFAULT_WINDOW_HOURS,
    limit: int = DEFAULT_MAX_REVIEWS,
    now: float | None = None,
) -> dict[str, Any]:
    """Read recent canonical reviews and aggregate them into a diagnosis."""
    from backend.core.db import connect_sqlite, get_state_pg_conn

    conn = (
        get_state_pg_conn()
        if is_state_db_path(db_path)
        else connect_sqlite(db_path)
    )
    try:
        reviews = load_recent_reviews(conn, limit=limit)
    finally:
        conn.close()
    return aggregate_reviews(
        reviews,
        window_hours=window_hours,
        max_reviews=limit,
        now=now,
    )

def persist_drawdown_attribution(
    report: dict[str, Any],
    *,
    db_path: str | Path = STATE_DB,
) -> dict[str, Any]:
    """Publish the attribution fact to runtime_kv (best-effort, fail-open)."""
    from backend.services.runtime_kv_store import set_on_conn
    from backend.core.db import connect_sqlite, get_state_pg_conn

    conn = (
        get_state_pg_conn()
        if is_state_db_path(db_path)
        else connect_sqlite(db_path)
    )
    try:
        set_on_conn(
            conn,
            KV_KEY,
            report,
            updated_at=float(report.get("generated_at") or time.time()),
            ensure=False,
        )
        conn.commit()
    finally:
        conn.close()
    return report
