"""Machine-readable provenance and freshness envelopes for public API facts."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, MutableMapping


FACT_STATES = frozenset({"known", "unknown", "stale", "error"})
DEFAULT_STALE_AFTER_SEC = {
    "ws": 5.0,
    "state": 5.0,
    "account": 15.0,
    "positions": 15.0,
    "loop": 15.0,
    "risk": 30.0,
    "readiness": 180.0,
    "recovery": 75.0,
}


@dataclass(frozen=True)
class FactEnvelope:
    envelope: str
    contract: str
    state: str
    source: str
    observed_at: float | str | None
    generated_at: float
    stale_after_sec: float
    reason_code: str | None = None
    components: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state not in FACT_STATES:
            raise ValueError(f"invalid_fact_state:{self.state}")
        if not str(self.contract or "").strip():
            raise ValueError("fact_contract_required")
        if float(self.stale_after_sec) <= 0:
            raise ValueError("fact_stale_after_sec_must_be_positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fact_envelope(
    *,
    contract: str,
    source: str,
    observed_at: float | str | None,
    stale_after_sec: float,
    error: Any = None,
    reason_code: str | None = None,
    components: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> FactEnvelope:
    generated_at = float(time.time() if now is None else now)
    stale_after = float(stale_after_sec)
    state = "known"
    normalized_reason = str(reason_code or "").strip() or None
    if error:
        state = "error"
        normalized_reason = normalized_reason or "source_error"
    else:
        try:
            observed_epoch = float(observed_at or 0.0)
        except (TypeError, ValueError):
            observed_epoch = 0.0
        if observed_epoch <= 0:
            state = "unknown"
            normalized_reason = normalized_reason or "missing_observed_at"
        elif generated_at - observed_epoch > stale_after:
            state = "stale"
            normalized_reason = normalized_reason or "freshness_expired"
    return FactEnvelope(
        envelope="fact.v1",
        contract=str(contract),
        state=state,
        source=str(source or "none"),
        observed_at=observed_at,
        generated_at=generated_at,
        stale_after_sec=stale_after,
        reason_code=normalized_reason,
        components=dict(components or {}),
    )


def attach_fact(
    payload: MutableMapping[str, Any],
    *,
    contract: str,
    source: str,
    observed_at: float | str | None,
    stale_after_sec: float,
    error: Any = None,
    reason_code: str | None = None,
    components: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> MutableMapping[str, Any]:
    """Attach ``_fact`` without changing any existing response field."""
    payload["_fact"] = fact_envelope(
        contract=contract,
        source=source,
        observed_at=observed_at,
        stale_after_sec=stale_after_sec,
        error=error,
        reason_code=reason_code,
        components=components,
        now=now,
    ).to_dict()
    return payload
