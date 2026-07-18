"""Serial, fail-closed safety-plane orchestration for the live loop.

The module never owns a broker thread and never exposes entry-order methods.
Callers provide reconciliation, candidate generation and risk-reducing
execution callbacks from the single live-loop thread.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping


SAFETY_MODES = frozenset({"off", "shadow", "enforce"})
SAFETY_ACTIONS = frozenset(
    {
        "entry_protection",
        "repair_entry_protection",
        "timeout",
        "supervisor",
        "trailing",
        "close",
        "reduce",
        "tighten",
        "emergency_close",
    }
)


@dataclass(frozen=True)
class SafetyCandidate:
    action: str
    position_id: int
    reason: str = ""
    controls: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: str = ""

    def __post_init__(self) -> None:
        normalized = str(self.action or "").strip().lower()
        if normalized not in SAFETY_ACTIONS:
            raise ValueError(f"unsafe_safety_plane_action:{normalized or 'missing'}")
        if int(self.position_id or 0) <= 0:
            raise ValueError("safety_candidate_position_id_required")
        object.__setattr__(self, "action", normalized)


@dataclass(frozen=True)
class SafetyCycleResult:
    mode: str
    status: str
    accepting_new_risk: bool
    reconciliation_state: str
    reconcile_id: str
    position_ids: tuple[int, ...]
    unknown_execution_count: int
    candidates: tuple[SafetyCandidate, ...]
    executed: tuple[Mapping[str, Any], ...]
    comparison: Mapping[str, Any]
    heartbeat_at: float
    next_full_cycle_in_sec: float
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["position_ids"] = list(self.position_ids)
        payload["candidates"] = [asdict(item) for item in self.candidates]
        payload["executed"] = [dict(item) for item in self.executed]
        payload["blockers"] = list(self.blockers)
        return payload


def _read(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _reconcile_success(value: Any) -> bool:
    explicit = _read(value, "success", None)
    if explicit is not None:
        return bool(explicit)
    state = str(_read(value, "state", _read(value, "status", "")) or "").lower()
    return state in {"fresh", "confirmed", "success", "known", "empty"}


def _position_id(position: Any) -> int:
    if isinstance(position, Mapping):
        return int(position.get("position_id") or position.get("ticket") or 0)
    return int(getattr(position, "position_id", 0) or getattr(position, "ticket", 0) or 0)


class LiveSafetyPlane:
    """Cadence and execution boundary for position-protection work."""

    def __init__(self, *, mode: str, clock: Callable[[], float] = time.time) -> None:
        normalized = str(mode or "off").strip().lower()
        if normalized not in SAFETY_MODES:
            raise ValueError(f"invalid_safety_plane_mode:{normalized}")
        self.mode = normalized
        self._clock = clock
        self._last_full_cycle_at = 0.0
        self._last_alpha_at = 0.0
        self._last_alpha_bar_id = ""

    @staticmethod
    def full_cycle_interval(*, has_positions: bool, unknown_execution_count: int) -> float:
        return 5.0 if has_positions or int(unknown_execution_count or 0) > 0 else 30.0

    def full_cycle_due(self, *, has_positions: bool, unknown_execution_count: int) -> bool:
        interval = self.full_cycle_interval(
            has_positions=has_positions,
            unknown_execution_count=unknown_execution_count,
        )
        return self._last_full_cycle_at <= 0 or self._clock() - self._last_full_cycle_at >= interval

    def alpha_due(self, *, closed_bar_id: str) -> bool:
        bar_id = str(closed_bar_id or "")
        if not bar_id or bar_id == self._last_alpha_bar_id:
            return False
        return self._last_alpha_at <= 0 or self._clock() - self._last_alpha_at >= 60.0

    def mark_alpha_run(self, *, closed_bar_id: str) -> None:
        bar_id = str(closed_bar_id or "")
        if not bar_id:
            raise ValueError("closed_bar_id_required")
        self._last_alpha_at = self._clock()
        self._last_alpha_bar_id = bar_id

    def run_cycle(
        self,
        *,
        reconcile_result: Any,
        unknown_execution_count: int,
        candidate_provider: Callable[[list[Any]], Iterable[SafetyCandidate | Mapping[str, Any]]],
        executor: Callable[[SafetyCandidate], Mapping[str, Any]],
        legacy_candidates: Iterable[SafetyCandidate | Mapping[str, Any]] | None = None,
    ) -> SafetyCycleResult:
        now = self._clock()
        reconcile_state = str(
            _read(reconcile_result, "state", _read(reconcile_result, "status", "unknown"))
            or "unknown"
        ).lower()
        reconcile_id = str(_read(reconcile_result, "reconcile_id", "") or "")
        raw_positions = list(_read(reconcile_result, "positions", []) or [])
        position_ids = tuple(sorted(pid for pid in (_position_id(p) for p in raw_positions) if pid > 0))
        unknown_count = max(0, int(unknown_execution_count or 0))
        blockers: list[str] = []

        if not _reconcile_success(reconcile_result):
            blockers.append("positions_reconciliation_failed")
            return SafetyCycleResult(
                mode=self.mode,
                status="reconciliation_failed",
                accepting_new_risk=False,
                reconciliation_state=reconcile_state,
                reconcile_id=reconcile_id,
                position_ids=position_ids,
                unknown_execution_count=unknown_count,
                candidates=(),
                executed=(),
                comparison={},
                heartbeat_at=now,
                next_full_cycle_in_sec=5.0,
                blockers=tuple(blockers),
            )
        if unknown_count:
            blockers.append("unknown_execution")

        interval = self.full_cycle_interval(
            has_positions=bool(position_ids),
            unknown_execution_count=unknown_count,
        )
        elapsed = now - self._last_full_cycle_at if self._last_full_cycle_at > 0 else interval
        if elapsed < interval:
            return SafetyCycleResult(
                mode=self.mode,
                status="heartbeat",
                accepting_new_risk=not blockers,
                reconciliation_state=reconcile_state,
                reconcile_id=reconcile_id,
                position_ids=position_ids,
                unknown_execution_count=unknown_count,
                candidates=(),
                executed=(),
                comparison={},
                heartbeat_at=now,
                next_full_cycle_in_sec=max(0.0, interval - elapsed),
                blockers=tuple(blockers),
            )

        self._last_full_cycle_at = now
        candidates = tuple(self._coerce_candidate(item) for item in candidate_provider(raw_positions))
        comparison = self._compare_candidates(candidates, legacy_candidates or ())
        executed: list[Mapping[str, Any]] = []
        status = "off" if self.mode == "off" else "shadow"
        if self.mode == "enforce":
            status = "completed"
            for candidate in candidates:
                try:
                    result = dict(executor(candidate) or {})
                except Exception as exc:
                    result = {
                        "ok": False,
                        "status": "exception",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                executed.append({"candidate": asdict(candidate), **result})
            if any(not bool(item.get("ok", False)) for item in executed):
                status = "partial"
                blockers.append("safety_action_failed")

        return SafetyCycleResult(
            mode=self.mode,
            status=status,
            accepting_new_risk=not blockers,
            reconciliation_state=reconcile_state,
            reconcile_id=reconcile_id,
            position_ids=position_ids,
            unknown_execution_count=unknown_count,
            candidates=candidates,
            executed=tuple(executed),
            comparison=comparison,
            heartbeat_at=now,
            next_full_cycle_in_sec=interval,
            blockers=tuple(sorted(set(blockers))),
        )

    @staticmethod
    def _coerce_candidate(value: SafetyCandidate | Mapping[str, Any]) -> SafetyCandidate:
        if isinstance(value, SafetyCandidate):
            return value
        return SafetyCandidate(
            action=str(value.get("action") or ""),
            position_id=int(value.get("position_id") or 0),
            reason=str(value.get("reason") or ""),
            controls=dict(value.get("controls") or {}),
            fingerprint=str(value.get("fingerprint") or ""),
        )

    @classmethod
    def _compare_candidates(
        cls,
        candidates: Iterable[SafetyCandidate],
        legacy: Iterable[SafetyCandidate | Mapping[str, Any]],
    ) -> dict[str, Any]:
        current_keys = {cls._candidate_key(item) for item in candidates}
        legacy_items = [cls._coerce_candidate(item) for item in legacy]
        legacy_keys = {cls._candidate_key(item) for item in legacy_items}
        return {
            "match": current_keys == legacy_keys,
            "v2_only": sorted(current_keys - legacy_keys),
            "legacy_only": sorted(legacy_keys - current_keys),
            "v2_count": len(current_keys),
            "legacy_count": len(legacy_keys),
        }

    @staticmethod
    def _candidate_key(candidate: SafetyCandidate) -> str:
        return candidate.fingerprint or f"{candidate.action}:{candidate.position_id}:{candidate.reason}"
