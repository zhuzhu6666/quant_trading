"""Executable contracts translated from docs/system-source-of-truth.md.

A documented constraint that is not expressed in code is not a constraint.
These assertions turn violations into hard failures at the call site instead
of silent states that only show up as unlabelled production data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# cTrader broker default minimum lot.  Callers that know the live bridge
# granularity should pass it explicitly via ``min_lot``.
MIN_LOT = 1.0


class ContractViolation(RuntimeError):
    """A documented system invariant was violated."""


def assert_exploration_min_preserved(
    before: float,
    after: float,
    trace: Mapping[str, Any] | None,
    *,
    min_lot: float | None = None,
) -> None:
    """Soft sizing must never zero out the demo exploration minimum lot.

    权威依据: docs/system-source-of-truth.md:219
    """

    values = dict(trace or {})
    if not (values.get("demo_exploration") or values.get("demo_nursery_exploration")):
        return
    lot = MIN_LOT if min_lot is None else float(min_lot)
    if lot <= 0:
        return
    before_value = float(before or 0.0)
    after_value = float(after or 0.0)
    if before_value >= lot > after_value:
        raise ContractViolation(
            "soft sizing zeroed the demo exploration minimum lot: "
            f"{before_value} -> {after_value} (min_lot={lot})"
        )


CONTAMINATED_CLOSE_REASONS = frozenset(
    {"chain_broken", "restart_replay", "broker_position_not_found"}
)


def learning_eligible(
    *,
    attribution_integrity: Any,
    context_integrity: Any,
    close_reason: Any,
) -> bool:
    """A review may enter the learning pool only with explicit full integrity.

    X1/L0-0R: missing fields are unknown, and unknown is refused instead of
    being treated as clean.  The ``close_reason`` denial is defence in depth
    for rows that slipped through with a stale integrity value.
    """

    if str(attribution_integrity or "").strip().lower() != "full":
        return False
    if str(context_integrity or "").strip().lower() != "full":
        return False
    if str(close_reason or "").strip().lower() in CONTAMINATED_CLOSE_REASONS:
        return False
    return True
