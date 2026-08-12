"""Machine-readable provenance and freshness envelopes for public API facts."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping, MutableMapping


FACT_STATES = frozenset({"known", "unknown", "stale", "error"})
DEFAULT_STALE_AFTER_SEC = {
    "ws": 5.0,
    "state": 5.0,
    # Public spot freshness follows the final open admission contract.  The
    # observation must still come from a real cTrader quote event; transport
    # heartbeats must not refresh it.
    "spot": 15.0,
    "account": 15.0,
    "positions": 15.0,
    "loop": 15.0,
    "risk": 30.0,
    "session": 30.0,
    "system_health": 75.0,
    "readiness": 180.0,
    "learning": 180.0,
    "ops": 180.0,
    "recovery": 75.0,
}

_UNAVAILABLE_SOURCES = frozenset({
    "",
    "none",
    "unknown",
    "unavailable",
    "not_registered",
    "degraded_cache",
})


def observed_epoch(value: float | str | datetime | None) -> float:
    """Normalize supported fact timestamps to epoch seconds.

    Public APIs already expose a mix of epoch numbers and ISO-8601 strings.
    Treating every string as zero would incorrectly downgrade fresh facts to
    ``unknown``, while silently accepting arbitrary text would do the reverse.
    """

    if isinstance(value, datetime):
        return float(value.timestamp())
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            try:
                return float(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp())
            except (TypeError, ValueError, OverflowError):
                return 0.0
    try:
        return float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


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
    normalized_source = str(source or "none").strip() or "none"
    if error:
        state = "error"
        normalized_reason = normalized_reason or "source_error"
    else:
        observed_ts = observed_epoch(observed_at)
        if observed_ts <= 0:
            state = "unknown"
            normalized_reason = normalized_reason or "missing_observed_at"
        elif normalized_source.lower() in _UNAVAILABLE_SOURCES:
            state = "unknown"
            normalized_reason = normalized_reason or "source_unavailable"
        elif generated_at - observed_ts > stale_after:
            state = "stale"
            normalized_reason = normalized_reason or "freshness_expired"
    return FactEnvelope(
        envelope="fact.v1",
        contract=str(contract),
        state=state,
        source=normalized_source,
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
