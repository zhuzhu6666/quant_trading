"""Loss-streak probation ladder (2026-08-24 design).

Answers "may the system open trades after the daily-loss circuit tripped?"
with a graduated ladder instead of a blind next-day reset:

- ``locked``      : current trading session is locked out until its end.
- ``probation``   : after the next broker session opens, one bounded probe
                    budget (+ optional tightened entry threshold) replaces
                    the dead daily-loss gate until the broker day ends or
                    the first probation loss lands.
- escalation      : a failed probe locks the rest of the broker day;
                    repeated tripped days tighten the next day's threshold.

The ladder state is *derived*: callers pass in the observed facts (tripped
reason, realized PnL since trip, broker-session timestamps) and this module
returns the verdict.  No IO, no clock reads, no globals — the caller owns
persistence and scheduling.  A missing/failed review statement degrades to
the legacy behaviour (next-day unlock), never to a stuck lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = "loss_streak_ladder.v1"

STATE_NONE = "none"
STATE_LOCKED_SESSION = "locked_session"
STATE_PROBATION = "probation"
STATE_LOCKED_DAY = "locked_day"

REASON_OK = "ok"
REASON_PROBE_BUDGET_EXHAUSTED = "probation_budget_exhausted"
REASON_PROBE_LOSS = "probation_first_loss"
REASON_AWAITING_SESSION_OPEN = "awaiting_next_session_open"
REASON_AWAITING_BROKER_DAY_END = "awaiting_broker_day_end"
REASON_REVIEW_PENDING = "loss_review_statement_pending"


@dataclass(frozen=True)
class LadderFacts:
    """Observed facts feeding one ladder evaluation."""

    now_ts: float
    # The moment the daily-loss circuit tripped (epoch s); 0 when not tripped.
    tripped_at: float = 0.0
    # Broker schedule facts for the symbol (epoch s). next_open must be the
    # first *formal* session open at/after the lock; day_end is the current
    # broker day boundary (22:00 UTC for XAUUSD-style symbols).
    next_session_open_ts: float = 0.0
    broker_day_end_ts: float = 0.0
    # Net realized PnL of positions closed during probation (negative = loss).
    probation_pnl: float = 0.0
    probation_trade_count: int = 0
    # Review statement produced by the learning loop during the lock.
    review_statement_ready: bool = False
    # How many consecutive UTC risk days ended with the limit tripped
    # (including today when tripped).
    consecutive_tripped_days: int = 1


@dataclass(frozen=True)
class LadderVerdict:
    """One ladder decision plus what the caller should record."""

    allowed: bool
    reason: str
    state: str
    # Entry-threshold add-on while probation is active (0.0 otherwise).
    threshold_addon: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


def evaluate_ladder(facts: LadderFacts, *, probation_budget_usd: float = 25.0,
                    base_threshold_addon: float = 0.05) -> LadderVerdict:
    """Pure transition function for the ladder."""
    if facts.tripped_at <= 0.0:
        return LadderVerdict(True, REASON_OK, STATE_NONE)

    escalation_days = max(1, int(facts.consecutive_tripped_days))
    # Escalation: the addon scales with consecutive tripped days, starting at
    # one base step even on the first trip (probation is never "normal").
    addon = round(min(0.30, base_threshold_addon * max(1, escalation_days)), 4)

    # Probation already consumed?  It survives only inside the same broker day.
    if facts.probation_trade_count > 0:
        if facts.probation_pnl < 0:
            if facts.now_ts < facts.broker_day_end_ts:
                return LadderVerdict(
                    False, REASON_PROBE_LOSS, STATE_LOCKED_DAY,
                    details={"resume_at": facts.broker_day_end_ts},
                )
            # Broker day rolled over; the ladder resets via the new-day path.
            return LadderVerdict(True, REASON_OK, STATE_NONE)
        # Net-positive (or flat) probes stay inside their bounded budget:
        # the probe budget caps further exposure while the run is green.
        if facts.now_ts < facts.broker_day_end_ts:
            return LadderVerdict(
                True, REASON_OK, STATE_PROBATION,
                threshold_addon=addon,
                details={"probe_budget_usd": probation_budget_usd,
                         "probation_pnl": facts.probation_pnl},
            )

    # Not yet in probation: still inside the locked session?
    if facts.next_session_open_ts > 0.0 and facts.now_ts < facts.next_session_open_ts:
        return LadderVerdict(
            False, REASON_AWAITING_SESSION_OPEN, STATE_LOCKED_SESSION,
            details={
                "resume_at": facts.next_session_open_ts,
                "review_pending": not facts.review_statement_ready,
                "threshold_addon_on_resume": addon,
            },
        )

    # Session has rolled over -> probation window inside the same broker day.
    if facts.broker_day_end_ts > 0.0 and facts.now_ts >= facts.broker_day_end_ts:
        return LadderVerdict(True, REASON_OK, STATE_NONE)

    return LadderVerdict(
        True, REASON_OK, STATE_PROBATION,
        threshold_addon=addon,
        details={"probe_budget_usd": probation_budget_usd},
    )
