"""Serial, fail-closed safety-plane orchestration for the live loop.

The module never owns a broker thread and never exposes entry-order methods.
Callers provide reconciliation, candidate generation and risk-reducing
execution callbacks from the single live-loop thread.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping

from backend.services.live_reconciliation import LIVE_SAFETY_FRESHNESS_SEC


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
    effective_mode: str
    forced_shadow: bool
    forced_shadow_reason: str
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


def _reconcile_success(value: Any, *, now: float) -> bool:
    state = str(_read(value, "state", _read(value, "status", "")) or "").lower()
    # Safety decisions require a full, fresh broker snapshot.  Explicit
    # reconcile contracts intentionally report cache/event as successful
    # reads, but those projections are not authoritative enough to prove an
    # empty account or verify a broker mutation.
    if state != "fresh":
        return False
    try:
        observed_at = float(_read(value, "observed_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if observed_at <= 0.0:
        return False
    age = float(now) - observed_at
    return -1.0 <= age <= LIVE_SAFETY_FRESHNESS_SEC


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
        self._last_comparison: dict[str, Any] = {}
        # A comparison failure is a one-way transition for this plane
        # instance.  Continuing to enforce V2 on a later cycle would turn a
        # transient mismatch into a mixed-authority broker mutation stream.
        # A governed latch clear plus a new plane generation is required to
        # restore V2 authority.
        self._forced_shadow = False
        self._forced_shadow_reason = ""

    @property
    def forced_shadow(self) -> bool:
        return bool(self._forced_shadow)

    @property
    def forced_shadow_reason(self) -> str:
        return str(self._forced_shadow_reason or "")

    @property
    def effective_mode(self) -> str:
        if self.mode == "enforce" and self._forced_shadow:
            return "shadow"
        return self.mode

    def force_shadow(self, reason: str) -> None:
        """Permanently remove V2 mutation authority from this plane.

        This method is intentionally idempotent.  The first failure reason is
        retained because it identifies the broker-authority transition that
        must be reviewed before a later generation can enforce V2 again.
        """

        if self.mode != "enforce":
            return
        if not self._forced_shadow:
            self._forced_shadow = True
            self._forced_shadow_reason = str(reason or "safety_candidate_comparison_failed")

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

    def remember_comparison(self, comparison: Mapping[str, Any]) -> None:
        """Persist the final three-way shadow result across heartbeat-only cycles."""

        self._last_comparison = dict(comparison or {})

    def run_cycle(
        self,
        *,
        reconcile_result: Any,
        unknown_execution_count: int,
        candidate_provider: Callable[[list[Any]], Iterable[SafetyCandidate | Mapping[str, Any]]],
        executor: Callable[[SafetyCandidate], Mapping[str, Any]],
        legacy_candidates: Iterable[SafetyCandidate | Mapping[str, Any]] | None = None,
        comparison_independent: bool = False,
        require_candidate_match: bool = False,
        force_full_cycle: bool = False,
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

        reconcile_fresh = _reconcile_success(reconcile_result, now=now)
        if not reconcile_fresh:
            # A stale/event projection can never authorize an empty account or
            # a new order, but known position IDs remain useful for idempotent
            # close/reduce/tighten attempts.  Continue the protection planner
            # below while retaining the fail-closed blocker.
            blockers.append("positions_reconciliation_failed")
        if unknown_count:
            blockers.append("unknown_execution")

        interval = self.full_cycle_interval(
            has_positions=bool(position_ids),
            unknown_execution_count=unknown_count,
        )
        elapsed = now - self._last_full_cycle_at if self._last_full_cycle_at > 0 else interval
        if elapsed < interval and not force_full_cycle:
            comparison = dict(self._last_comparison) if require_candidate_match else {}
            if require_candidate_match:
                if bool(comparison.get("duplicate")):
                    blockers.append("safety_candidate_duplicate")
                elif bool(comparison.get("position_conflict")):
                    blockers.append("safety_candidate_position_conflict")
                elif not bool(comparison.get("independent")):
                    blockers.append("safety_candidate_comparison_not_independent")
                elif not bool(comparison.get("match")):
                    blockers.append("safety_candidate_mismatch")
            if self.forced_shadow:
                blockers.append("safety_v2_forced_shadow")
            return SafetyCycleResult(
                mode=self.mode,
                effective_mode=self.effective_mode,
                forced_shadow=self.forced_shadow,
                forced_shadow_reason=self.forced_shadow_reason,
                status="heartbeat",
                accepting_new_risk=not blockers,
                reconciliation_state=reconcile_state,
                reconcile_id=reconcile_id,
                position_ids=position_ids,
                unknown_execution_count=unknown_count,
                candidates=(),
                executed=(),
                comparison=comparison,
                heartbeat_at=now,
                next_full_cycle_in_sec=max(0.0, interval - elapsed),
                blockers=tuple(blockers),
            )

        self._last_full_cycle_at = now
        comparison_error = ""
        try:
            candidates = tuple(
                self._coerce_candidate(item) for item in candidate_provider(raw_positions)
            )
            comparison = self._compare_candidates(
                candidates,
                legacy_candidates or (),
                independent=bool(comparison_independent),
            )
        except Exception as exc:
            # Candidate coercion and set comparison are part of the authority
            # decision, not execution.  Any exception therefore removes V2
            # broker authority before an executor can be called.
            candidates = ()
            comparison_error = f"{type(exc).__name__}: {exc}"
            comparison = {
                "independent": False,
                "match": False,
                "enforce_eligible": False,
                "fingerprint": "",
                "diff": {"v2_only": [], "legacy_only": []},
                "v2_only": [],
                "legacy_only": [],
                "v2_count": 0,
                "legacy_count": 0,
                "error": comparison_error,
            }
        self._last_comparison = dict(comparison)
        if require_candidate_match:
            if comparison_error:
                blockers.append("safety_candidate_comparison_error")
            elif bool(comparison.get("duplicate")):
                blockers.append("safety_candidate_duplicate")
            elif bool(comparison.get("position_conflict")):
                blockers.append("safety_candidate_position_conflict")
            elif not bool(comparison.get("independent")):
                blockers.append("safety_candidate_comparison_not_independent")
            elif not bool(comparison.get("match")):
                blockers.append("safety_candidate_mismatch")
        comparison_ready = bool(
            not require_candidate_match or comparison.get("enforce_eligible")
        )
        if self.mode == "enforce" and not comparison_ready:
            if comparison_error:
                force_reason = "safety_candidate_comparison_error"
            elif bool(comparison.get("duplicate")):
                force_reason = "safety_candidate_duplicate"
            elif bool(comparison.get("position_conflict")):
                force_reason = "safety_candidate_position_conflict"
            elif not bool(comparison.get("independent")):
                force_reason = "safety_candidate_comparison_not_independent"
            else:
                force_reason = "safety_candidate_mismatch"
            self.force_shadow(force_reason)
        if self.forced_shadow:
            blockers.append("safety_v2_forced_shadow")
        executed: list[Mapping[str, Any]] = []
        status = "off" if self.mode == "off" else "shadow"
        if self.forced_shadow:
            status = "forced_shadow"
        elif self.mode == "enforce":
            status = "completed" if reconcile_fresh else "reconciliation_failed"
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
                status = "partial" if reconcile_fresh else "reconciliation_failed_partial"
                blockers.append("safety_action_failed")

        return SafetyCycleResult(
            mode=self.mode,
            effective_mode=self.effective_mode,
            forced_shadow=self.forced_shadow,
            forced_shadow_reason=self.forced_shadow_reason,
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
    def compare_candidate_sets(
        cls,
        candidates: Iterable[SafetyCandidate | Mapping[str, Any]],
        legacy: Iterable[SafetyCandidate | Mapping[str, Any]],
        *,
        independent: bool,
    ) -> dict[str, Any]:
        return cls._compare_candidates(
            (cls._coerce_candidate(item) for item in candidates),
            legacy,
            independent=independent,
        )

    @classmethod
    def _compare_candidates(
        cls,
        candidates: Iterable[SafetyCandidate],
        legacy: Iterable[SafetyCandidate | Mapping[str, Any]],
        *,
        independent: bool = False,
    ) -> dict[str, Any]:
        current_items = list(candidates)
        current_keys = [cls._candidate_key(item) for item in current_items]
        legacy_items = [cls._coerce_candidate(item) for item in legacy]
        legacy_keys = [cls._candidate_key(item) for item in legacy_items]
        current_counts = Counter(current_keys)
        legacy_counts = Counter(legacy_keys)
        match = current_counts == legacy_counts
        duplicate_v2 = sorted(key for key, count in current_counts.items() if count > 1)
        duplicate_legacy = sorted(key for key, count in legacy_counts.items() if count > 1)
        current_position_counts = Counter(int(item.position_id) for item in current_items)
        legacy_position_counts = Counter(int(item.position_id) for item in legacy_items)
        conflicting_v2_position_ids = sorted(
            pid for pid, count in current_position_counts.items() if count > 1
        )
        conflicting_legacy_position_ids = sorted(
            pid for pid, count in legacy_position_counts.items() if count > 1
        )
        duplicate = bool(duplicate_v2 or duplicate_legacy)
        position_conflict = bool(
            conflicting_v2_position_ids or conflicting_legacy_position_ids
        )
        fingerprint_payload = {
            "v2": sorted(current_keys),
            "legacy": sorted(legacy_keys),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        v2_only = sorted((current_counts - legacy_counts).elements())
        legacy_only = sorted((legacy_counts - current_counts).elements())
        return {
            "independent": bool(independent),
            "match": match,
            "enforce_eligible": bool(independent) and match and not duplicate and not position_conflict,
            "fingerprint": fingerprint,
            "diff": {"v2_only": v2_only, "legacy_only": legacy_only},
            "v2_only": v2_only,
            "legacy_only": legacy_only,
            "v2_count": len(current_keys),
            "legacy_count": len(legacy_keys),
            "duplicate": duplicate,
            "duplicate_v2": duplicate_v2,
            "duplicate_legacy": duplicate_legacy,
            "position_conflict": position_conflict,
            "conflicting_v2_position_ids": conflicting_v2_position_ids,
            "conflicting_legacy_position_ids": conflicting_legacy_position_ids,
        }

    @staticmethod
    def _candidate_key(candidate: SafetyCandidate) -> str:
        return candidate.fingerprint or f"{candidate.action}:{candidate.position_id}:{candidate.reason}"
