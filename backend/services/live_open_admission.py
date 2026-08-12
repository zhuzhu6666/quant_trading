"""Final fail-closed facts required immediately before a broker open RPC.

This module is deliberately open-only.  Position close/reduce/tighten paths
must never import or depend on PostgreSQL, market-session, or quote health.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from typing import Any, Callable, Mapping

from backend.services.fact_envelope import DEFAULT_STALE_AFTER_SEC

# Keep final open quote admission on the same canonical freshness contract
# exposed by live.spot-quote.v1.  The admission result remains an independent
# fail-closed safety decision.
FINAL_OPEN_FACT_MAX_AGE_SECONDS = DEFAULT_STALE_AFTER_SEC["spot"]


@dataclass(frozen=True)
class FinalOpenAdmissionResult:
    ok: bool
    blockers: tuple[str, ...]
    checked_at: float
    postgres: dict[str, Any]
    market_session: dict[str, Any]
    spot_quote: dict[str, Any]
    schema_version: str = "final_open_admission.v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_postgres_authority(
    connect: Callable[[], Any],
    *,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Return a fresh PostgreSQL liveness fact without mutating state.

    The caller performs this probe before acquiring the broker-mutation lock,
    so a stalled database cannot delay emergency risk-reduction ownership.
    """

    started_at = float(now())
    conn = None
    try:
        conn = connect()
        cursor = conn.execute("SELECT 1")
        if hasattr(cursor, "fetchone") and cursor.fetchone() is None:
            raise RuntimeError("postgres_liveness_probe_returned_no_row")
        return {
            "state": "known",
            "ok": True,
            "started_at": started_at,
            "observed_at": float(now()),
            "error": "",
        }
    except Exception as exc:
        return {
            "state": "error",
            "ok": False,
            "started_at": started_at,
            "observed_at": float(now()),
            "error": f"{type(exc).__name__}:{exc}"[:500],
        }
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _timestamp_blocker(
    *,
    value: Any,
    checked_at: float,
    unknown: str,
    stale: str,
    invalid: str,
    max_age_seconds: float,
) -> str | None:
    try:
        observed_at = float(value or 0.0)
    except (TypeError, ValueError):
        observed_at = 0.0
    if observed_at <= 0:
        return unknown
    age = checked_at - observed_at
    if age < -1.0:
        return invalid
    if age > max_age_seconds:
        return stale
    return None


def evaluate_final_open_admission(
    *,
    postgres: Mapping[str, Any] | None,
    market_session: Mapping[str, Any] | None,
    spot_quote: Mapping[str, Any] | None,
    now_ts: float | None = None,
    max_age_seconds: float = FINAL_OPEN_FACT_MAX_AGE_SECONDS,
) -> FinalOpenAdmissionResult:
    """Evaluate immutable facts used at the final new-risk boundary."""

    checked_at = float(time.time() if now_ts is None else now_ts)
    max_age = max(0.1, float(max_age_seconds))
    pg = dict(postgres or {})
    session = dict(market_session or {})
    quote = dict(spot_quote or {})
    blockers: list[str] = []

    if not bool(pg.get("ok")):
        blockers.append("state_pg_unavailable")
    pg_timestamp_blocker = _timestamp_blocker(
        value=pg.get("observed_at"),
        checked_at=checked_at,
        unknown="state_pg_probe_unknown",
        stale="state_pg_probe_stale",
        invalid="state_pg_probe_timestamp_invalid",
        max_age_seconds=max_age,
    )
    if pg_timestamp_blocker:
        blockers.append(pg_timestamp_blocker)

    if not session:
        blockers.append("market_session_unknown")
    elif not bool(session.get("can_open_positions")):
        blockers.append("market_session_blocks_open")
    if session.get("broker_connected") is False:
        blockers.append("market_session_broker_disconnected")
    session_timestamp_blocker = _timestamp_blocker(
        value=session.get("now_ts"),
        checked_at=checked_at,
        unknown="market_session_timestamp_unknown",
        stale="market_session_stale",
        invalid="market_session_timestamp_invalid",
        max_age_seconds=max_age,
    )
    if session_timestamp_blocker:
        blockers.append(session_timestamp_blocker)

    quote_timestamp_blocker = _timestamp_blocker(
        value=quote.get("ts"),
        checked_at=checked_at,
        unknown="spot_quote_unknown",
        stale="spot_quote_stale",
        invalid="spot_quote_timestamp_invalid",
        max_age_seconds=max_age,
    )
    if quote_timestamp_blocker:
        blockers.append(quote_timestamp_blocker)
    quote_values: list[float] = []
    for key in ("bid", "ask", "mid"):
        try:
            quote_values.append(float(quote.get(key) or 0.0))
        except (TypeError, ValueError):
            quote_values.append(0.0)
    if not any(value > 0 for value in quote_values):
        blockers.append("spot_quote_invalid")
    bid, ask, mid = quote_values
    if bid <= 0.0 or ask <= 0.0 or mid <= 0.0 or ask < bid:
        blockers.append("spot_quote_bid_ask_invalid")
    elif ask - bid <= 0.0:
        blockers.append("spot_quote_spread_invalid")

    normalized = tuple(sorted(set(blockers)))
    return FinalOpenAdmissionResult(
        ok=not normalized,
        blockers=normalized,
        checked_at=checked_at,
        postgres=pg,
        market_session=session,
        spot_quote=quote,
    )
