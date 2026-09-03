from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any


_UNKNOWN = {"", "unknown", "none", "null", "n/a"}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _known(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    return "" if normalized in _UNKNOWN else normalized


def project_current_market_regime(
    experience_rows: Sequence[Mapping[str, Any]],
    *,
    recent_window: int = 5,
) -> dict[str, Any]:
    """Project the current market regime from recent experience memory rows.

    Read-only consumer of the existing `experience_memory.regime_id` fact source
    (no new writer, no new table).  Latest known regime wins; when the latest
    `recent_window` rows do not share one regime, fall back to a recent-majority
    projection with reduced confidence.  Unknown/empty regime ids are ignored.
    """
    known = [
        row
        for row in experience_rows
        if _known(_field(row, "regime_id", ""))
    ]
    if not known:
        return {
            "regime_id": "",
            "confidence": 0.0,
            "source": "unavailable",
            "dimensions": {},
        }
    ordered = sorted(known, key=lambda row: float(_field(row, "created_at", 0.0) or 0.0))
    latest = ordered[-1]
    latest_regime = _known(_field(latest, "regime_id", ""))
    window = ordered[-max(1, int(recent_window)):]
    window_regimes = [_known(_field(row, "regime_id", "")) for row in window]
    counts = Counter(regime for regime in window_regimes if regime)
    if not counts:
        return {
            "regime_id": "",
            "confidence": 0.0,
            "source": "unavailable",
            "dimensions": {},
        }
    majority_regime, majority_count = counts.most_common(1)[0]
    if majority_regime == latest_regime:
        confidence = min(1.0, majority_count / max(1, len(window)))
        return {
            "regime_id": majority_regime,
            "confidence": round(confidence, 4),
            "source": "experience_memory.latest",
            "dimensions": {},
        }
    return {
        "regime_id": majority_regime,
        "confidence": round(majority_count / max(1, len(window)), 4),
        "source": "experience_memory.recent_majority",
        "dimensions": {},
    }


def resolve_market_regime(composite: Any) -> dict[str, Any]:
    """Resolve a stable, low-cardinality regime from a composite decision.

    Explicit regime facts win. Otherwise the regime is derived from the same
    trend/volatility context already used by the decision policy. Session is a
    factual fallback only when both market dimensions are unavailable.
    """
    context = _field(composite, "context_state", {}) or {}
    for container, source in ((composite, "composite"), (context, "context_state")):
        for key in ("regime_id", "regime", "regime_state"):
            regime_id = _known(_field(container, key, ""))
            if not regime_id:
                continue
            confidence = _field(container, "regime_confidence", 1.0)
            try:
                confidence = max(0.0, min(1.0, float(confidence)))
            except (TypeError, ValueError):
                confidence = 1.0
            return {
                "regime_id": regime_id,
                "confidence": round(confidence, 4),
                "source": f"{source}.{key}",
                "dimensions": {},
            }

    trend = _known(_field(context, "trend_strength_state", ""))
    volatility = _known(_field(context, "volatility_state", ""))
    dimensions = {
        key: value
        for key, value in (("trend", trend), ("volatility", volatility))
        if value
    }
    if dimensions:
        regime_id = "|".join(f"{key}={value}" for key, value in dimensions.items())
        confidence = 0.8 if len(dimensions) == 2 else 0.55
        return {
            "regime_id": regime_id,
            "confidence": confidence,
            "source": "context_state.market_dimensions",
            "dimensions": dimensions,
        }

    session = _known(_field(context, "session_state", ""))
    if session:
        return {
            "regime_id": f"session={session}",
            "confidence": 0.35,
            "source": "context_state.session_fallback",
            "dimensions": {"session": session},
        }
    return {
        "regime_id": "",
        "confidence": 0.0,
        "source": "unavailable",
        "dimensions": {},
    }


def current_regime_projection(db_path) -> dict[str, Any]:
    """Read-only current-regime projection over `experience_memory`.

    Shared fact owner for revival gates: the latest labeled rows through
    :func:`project_current_market_regime`.  No new writer, no new table.
    Returns an `unavailable` projection when there is no data.
    """
    try:
        from pathlib import Path

        from backend.core.db import (
            connect_sqlite,
            get_state_pg_conn,
            is_state_db_path,
            state_table_exists,
        )

        production_state = is_state_db_path(db_path)
        if not production_state and not Path(db_path).exists():
            return {
                "regime_id": "",
                "confidence": 0.0,
                "source": "unavailable",
                "dimensions": {},
            }
        conn = (
            get_state_pg_conn(read_only=True)
            if production_state
            else connect_sqlite(db_path, read_only=True)
        )
        try:
            if not production_state:
                import sqlite3

                conn.row_factory = sqlite3.Row
            if not state_table_exists(conn, "experience_memory"):
                return {
                    "regime_id": "",
                    "confidence": 0.0,
                    "source": "unavailable",
                    "dimensions": {},
                }
            rows = conn.execute(
                """
                SELECT regime_id, created_at, trade_id
                FROM experience_memory
                WHERE regime_id IS NOT NULL AND regime_id <> ''
                ORDER BY created_at DESC
                LIMIT 15
                """
            ).fetchall()
            experience_rows = [
                {
                    "regime_id": str(row["regime_id"] or ""),
                    "created_at": float(row["created_at"] or 0.0),
                    "trade_id": str(row["trade_id"] or ""),
                }
                for row in rows
            ]
            return project_current_market_regime(experience_rows)
        finally:
            conn.close()
    except Exception:
        return {
            "regime_id": "",
            "confidence": 0.0,
            "source": "unavailable",
            "dimensions": {},
        }
