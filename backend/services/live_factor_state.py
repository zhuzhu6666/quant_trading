"""State transitions applied after a live factor decision is ready."""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
import time
from typing import Any

from backend.services.live_tick_pipeline import (
    build_factor_snapshot_summary,
    build_factor_votes,
)


@dataclass(frozen=True)
class DecisionBarProgress:
    """Normalized decision-bar progress used by the live tick facade."""

    bar_ts: float
    last_processed_ts: float

    @property
    def already_processed(self) -> bool:
        return self.bar_ts > 0 and self.last_processed_ts >= self.bar_ts


@dataclass(frozen=True)
class CommittedFactorDecision:
    """Values exposed to the execution part of a committed decision tick."""

    factor_values: Any
    signals: Any
    composite: Any
    gate_result: Any


def _timestamp(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def resolve_decision_bar_progress(
    bar: Mapping[str, Any],
    last_processed_value: Any,
) -> DecisionBarProgress:
    """Normalize bar timestamps without letting malformed state stop the loop."""

    return DecisionBarProgress(
        bar_ts=_timestamp(bar.get("time")),
        last_processed_ts=_timestamp(last_processed_value),
    )


def commit_ready_factor_decision(
    *,
    decision_frame: Any,
    progress: DecisionBarProgress,
    pipeline: MutableMapping[str, Any],
    update_live_state: Callable[..., Any],
    set_factor_snapshot: Callable[[dict[str, Any], dict[str, Any]], Any],
    tick: int,
    log: Callable[[str], Any],
    now: Callable[[], float] = time.time,
) -> CommittedFactorDecision:
    """Commit the non-execution state produced by ``LiveDecisionFrame``.

    The decision timestamp is committed before the best-effort snapshot,
    matching the live facade's existing partial-commit order.  Execution,
    ledger writes, and risk authorization are deliberately outside this
    boundary.
    """

    factor_values = decision_frame.factor_values
    pipeline["last_factor_values"] = dict(factor_values or {})
    if progress.bar_ts > 0:
        update_live_state(last_processed_decision_bar_ts=progress.bar_ts)

    signals = decision_frame.signals
    composite = decision_frame.composite
    gate_result = decision_frame.gate_result
    try:
        set_factor_snapshot(
            build_factor_votes(
                signals,
                factor_values,
                getattr(composite, "factor_roles", {}),
                getattr(composite, "active_weights", {}),
            ),
            # Keep the decision-bar identity in the same snapshot that the
            # renderer consumes.  ``ts`` is publication time and cannot
            # distinguish two decisions with identical factor values.
            build_factor_snapshot_summary(
                composite,
                gate_result,
                now=now(),
                decision_bar_ts=progress.bar_ts,
            ),
        )
    except Exception as exc:
        log(f"tick {tick}: factor votes save failed (non-fatal): {exc}")

    return CommittedFactorDecision(
        factor_values=factor_values,
        signals=signals,
        composite=composite,
        gate_result=gate_result,
    )
