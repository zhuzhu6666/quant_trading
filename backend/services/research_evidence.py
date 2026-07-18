"""Fail-closed trust boundary for research and replay evidence.

Research output is descriptive until it proves that it used the same causal
inputs and governed components as the live path.  This module intentionally
does not perform a promotion or deployment; it only answers whether an
evidence payload is even eligible to be presented to those services.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any, Mapping


LEGACY_BACKTEST_ENGINE = "legacy_indicator_sweep"
LEGACY_BACKTEST_EVIDENCE_CLASS = "diagnostic_only"
PARITY_REPLAY_ENGINE = "live_parity_replay_v1"
PARITY_REPLAY_EVIDENCE_CLASS = "live_parity"
PARITY_REPLAY_REPORT_SCHEMA = "parity_replay_report.v1"
PARITY_REPLAY_CONTRACT = "parity_replay_contract.v1"
RESEARCH_EVIDENCE_POLICY_VERSION = "research_evidence_policy.v1"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_BINDING_HASHES = (
    "config_hash",
    "data_hash",
    "code_hash",
    "artifact_hash",
    "binding_hash",
)
_REQUIRED_EXPECTED_BINDINGS = _REQUIRED_BINDING_HASHES[:-1]
_REQUIRED_PARITY_COMPONENTS = (
    "factor_frame",
    "runtime_selector",
    "streaming_factor_engine",
    "normalizer",
    "compositor",
    "execution_gate",
    "risk_policy",
    "position_path_metrics",
    "safety_arbitration",
    "supervisor",
    "trailing",
    "protection_planner",
    "cost_model",
    "lifecycle",
)


@dataclass(frozen=True)
class ResearchEvidenceVerdict:
    allowed: bool
    reason: str
    engine: str
    evidence_class: str
    live_parity: bool
    governance_eligible: bool
    deployable_candidate: bool
    blockers: tuple[str, ...]
    policy_version: str = RESEARCH_EVIDENCE_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blockers"] = list(self.blockers)
        return payload


class ResearchEvidenceRejected(ValueError):
    """Raised when diagnostic evidence reaches an executable control path."""

    def __init__(self, verdict: ResearchEvidenceVerdict):
        self.verdict = verdict
        super().__init__(f"research_evidence_rejected:{verdict.reason}")


def legacy_backtest_contract() -> dict[str, Any]:
    """Return the immutable public trust label for the legacy sweep."""

    return {
        "engine": LEGACY_BACKTEST_ENGINE,
        "evidence_class": LEGACY_BACKTEST_EVIDENCE_CLASS,
        "live_parity": False,
        "governance_eligible": False,
        "deployable_candidate": False,
    }


def enforce_legacy_backtest_contract(payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Force legacy labels after merging so callers cannot override them."""

    return {**dict(payload or {}), **legacy_backtest_contract()}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _research_payload(evidence: Mapping[str, Any] | None) -> Mapping[str, Any]:
    item = _mapping(evidence)
    nested = _mapping(item.get("research_evidence"))
    return nested or item


def evaluate_research_evidence(
    evidence: Mapping[str, Any] | None,
    *,
    executable_use: str = "governance",
) -> ResearchEvidenceVerdict:
    """Evaluate whether research evidence may reach an executable path.

    Missing trust metadata is rejected when this function is called.  Callers
    that do not consume research evidence do not need to invoke it.  A parity
    replay is accepted only when every hash binding and every causal/live
    component assertion is explicit; optimistic defaults are forbidden.
    """

    envelope = _mapping(evidence)
    item = _research_payload(evidence)
    engine = str(item.get("engine") or "").strip()
    evidence_class = str(item.get("evidence_class") or "").strip()
    live_parity = _bool(item.get("live_parity"))
    governance_eligible = _bool(item.get("governance_eligible"))
    deployable_candidate = _bool(item.get("deployable_candidate"))
    bindings = _mapping(item.get("bindings"))
    causality = _mapping(item.get("causality"))
    components = _mapping(item.get("components"))

    blockers: list[str] = []
    # An outer legacy envelope cannot be hidden by attaching optimistic nested
    # metadata.  Promotion services often receive an artifact containing a
    # nested research_evidence object, so both layers are part of the boundary.
    if str(envelope.get("engine") or "").strip() == LEGACY_BACKTEST_ENGINE:
        blockers.append("legacy_indicator_sweep_diagnostic_only")
    if not engine:
        blockers.append("engine_missing")
    elif engine == LEGACY_BACKTEST_ENGINE:
        blockers.append("legacy_indicator_sweep_diagnostic_only")
    elif engine != PARITY_REPLAY_ENGINE:
        blockers.append(f"engine_{engine}_not_executable")
    if str(item.get("schema_version") or "") != PARITY_REPLAY_REPORT_SCHEMA:
        blockers.append("parity_report_schema_unverified")
    if str(item.get("contract") or "") != PARITY_REPLAY_CONTRACT:
        blockers.append("parity_contract_unverified")
    if str(item.get("status") or "") != "parity_verified":
        blockers.append("parity_status_unverified")
    if evidence_class != PARITY_REPLAY_EVIDENCE_CLASS:
        blockers.append(f"evidence_class_{evidence_class or 'missing'}")
    if not live_parity:
        blockers.append("live_parity_not_verified")
    if not governance_eligible:
        blockers.append("governance_eligible_false")
    if not deployable_candidate:
        blockers.append("deployable_candidate_false")

    for name in _REQUIRED_BINDING_HASHES:
        value = str(bindings.get(name) or "").strip().lower()
        if not _HASH_RE.fullmatch(value):
            blockers.append(f"binding_{name}_missing_or_invalid")
    binding_inputs = {
        name: str(bindings.get(name) or "").strip().lower()
        for name in _REQUIRED_BINDING_HASHES
        if name != "binding_hash"
    }
    expected_binding_hash = hashlib.sha256(
        json.dumps(
            binding_inputs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if str(bindings.get("binding_hash") or "").strip().lower() != expected_binding_hash:
        blockers.append("binding_hash_integrity_failed")
    binding_verification = _mapping(item.get("binding_verification"))
    if not _bool(binding_verification.get("verified")) or list(
        binding_verification.get("mismatches") or []
    ):
        blockers.append("binding_verification_failed")
    missing_expected = list(binding_verification.get("missing_expected") or [])
    if missing_expected:
        blockers.append("binding_expected_preconditions_missing")
    expected_bindings = _mapping(binding_verification.get("expected"))
    required_expected_names = {
        str(value) for value in binding_verification.get("required_expected_names") or []
    }
    if not set(_REQUIRED_EXPECTED_BINDINGS).issubset(required_expected_names):
        blockers.append("binding_expected_contract_incomplete")
    for name in _REQUIRED_EXPECTED_BINDINGS:
        expected = str(expected_bindings.get(name) or "").strip().lower()
        actual = str(bindings.get(name) or "").strip().lower()
        if expected != actual or not _HASH_RE.fullmatch(expected):
            blockers.append(f"binding_expected_{name}_unverified")

    data_source = _mapping(item.get("data_source"))
    if str(data_source.get("source") or "") != "monthly_pit_bars":
        blockers.append("monthly_pit_data_source_unverified")
    if not _bool(data_source.get("point_in_time")):
        blockers.append("monthly_pit_point_in_time_unverified")

    if not _bool(causality.get("closed_bar_only")):
        blockers.append("closed_bar_causality_unverified")
    if not _bool(causality.get("next_bar_execution")):
        blockers.append("next_bar_execution_unverified")
    if not _bool(causality.get("native_bid_ask")):
        blockers.append("native_bid_ask_unverified")

    for name in _REQUIRED_PARITY_COMPONENTS:
        component = _mapping(components.get(name))
        if component.get("reuse") != "exact" or not _bool(component.get("verified")):
            blockers.append(f"component_{name}_not_exact")
    if list(item.get("diagnostic_reasons") or []):
        blockers.append("parity_diagnostic_blockers_present")

    allowed = not blockers
    reason = "executable_research_evidence_verified" if allowed else blockers[0]
    return ResearchEvidenceVerdict(
        allowed=allowed,
        reason=reason,
        engine=engine,
        evidence_class=evidence_class,
        live_parity=live_parity,
        governance_eligible=governance_eligible,
        deployable_candidate=deployable_candidate,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def require_executable_research_evidence(
    evidence: Mapping[str, Any] | None,
    *,
    executable_use: str = "governance",
) -> ResearchEvidenceVerdict:
    verdict = evaluate_research_evidence(evidence, executable_use=executable_use)
    if not verdict.allowed:
        raise ResearchEvidenceRejected(verdict)
    return verdict


def has_research_trust_metadata(value: Mapping[str, Any] | None) -> bool:
    envelope = _mapping(value)
    item = _research_payload(value)
    return any(
        key in item or key in envelope
        for key in (
            "engine",
            "evidence_class",
            "live_parity",
            "governance_eligible",
            "deployable_candidate",
        )
    )


__all__ = [
    "LEGACY_BACKTEST_ENGINE",
    "LEGACY_BACKTEST_EVIDENCE_CLASS",
    "PARITY_REPLAY_ENGINE",
    "PARITY_REPLAY_EVIDENCE_CLASS",
    "PARITY_REPLAY_REPORT_SCHEMA",
    "PARITY_REPLAY_CONTRACT",
    "RESEARCH_EVIDENCE_POLICY_VERSION",
    "ResearchEvidenceRejected",
    "ResearchEvidenceVerdict",
    "enforce_legacy_backtest_contract",
    "evaluate_research_evidence",
    "has_research_trust_metadata",
    "legacy_backtest_contract",
    "require_executable_research_evidence",
]
