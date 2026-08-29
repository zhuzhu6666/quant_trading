from __future__ import annotations

import time
from typing import Any


def _dig(payload: dict[str, Any], *keys: str) -> Any:
    cur: Any = payload
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _gate(name: str, ok: bool, *, evidence_ok: bool | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {
        "name": name,
        "ok": bool(ok),
        "details": details or {},
    }
    if evidence_ok is not None:
        result["evidence_ok"] = bool(evidence_ok)
    return result


class V15Phase0CompletionService:
    """Machine-readable V15 Phase 0 completion contract.

    The gate is read-only. It separates implementation completion from current
    operational evidence freshness so a missing replay run does not masquerade
    as missing code capability.
    """

    def build(self, *, readiness: dict[str, Any]) -> dict[str, Any]:
        v15 = readiness.get("v15") if isinstance(readiness.get("v15"), dict) else {}
        boundaries = v15.get("control_plane_boundaries") if isinstance(v15.get("control_plane_boundaries"), dict) else {}
        replay = readiness.get("replay") if isinstance(readiness.get("replay"), dict) else {}
        latest_replay = replay.get("latest_report") if isinstance(replay.get("latest_report"), dict) else {}
        autonomy = readiness.get("autonomy_health") if isinstance(readiness.get("autonomy_health"), dict) else {}
        incident = readiness.get("incident_control") if isinstance(readiness.get("incident_control"), dict) else {}
        release = readiness.get("release") if isinstance(readiness.get("release"), dict) else {}
        latest_release = release.get("latest_release") if isinstance(release.get("latest_release"), dict) else {}
        snapshot = v15.get("snapshot") if isinstance(v15.get("snapshot"), dict) else {}

        replay_capability_ok = (
            str(replay.get("schema_version") or "") == "replay_readiness.v1"
            and "status" in replay
            and str(replay.get("status") or "") != "error"
            and not replay.get("error")
        )
        replay_evidence_ok = bool(replay.get("ok")) and bool(latest_replay.get("replay_run_id"))
        release_capability_ok = (
            str(release.get("schema_version") or "") == "release_readiness.v1"
            and isinstance(latest_release, dict)
            and str(latest_release.get("status") or "") != "error"
        )
        release_evidence_ok = (
            release.get("ok") is True
            and bool(latest_release.get("run_id"))
            and str(latest_release.get("status") or "") == "completed"
        )
        gates = [
            _gate(
                "readiness_contract",
                readiness.get("schema_version") == "backend_readiness.v1"
                and v15.get("schema_version") == "v15_readiness_contract.v1",
                details={
                    "backend_schema": readiness.get("schema_version"),
                    "v15_schema": v15.get("schema_version"),
                },
            ),
            _gate(
                "control_plane_boundaries",
                all(
                    bool(boundaries.get(key))
                    for key in (
                        "runtime_overlay_is_source_of_truth",
                        "runtime_snapshot_required_for_rollback",
                        "risk_policy_service_required",
                        "decision_policy_required_for_weight_writes",
                        "models_shadow_or_advisory_only",
                    )
                ),
                details=boundaries,
            ),
            _gate(
                "runtime_snapshot_contract",
                isinstance(snapshot, dict) and "ok" in snapshot,
                evidence_ok=bool(snapshot.get("ok")),
                details={
                    "ok": snapshot.get("ok"),
                    "config_hash": snapshot.get("config_hash"),
                    "status": snapshot.get("status"),
                },
            ),
            _gate(
                "replay_harness_v1",
                replay_capability_ok,
                evidence_ok=replay_evidence_ok,
                details={
                    "status": replay.get("status"),
                    "latest_replay_run_id": latest_replay.get("replay_run_id"),
                    "evidence_grade": latest_replay.get("evidence_grade"),
                },
            ),
            _gate(
                "autonomy_health_v1",
                str(autonomy.get("schema_version") or "") == "autonomy_health.v1"
                and str(autonomy.get("posture") or "") in {"full", "constrained", "shadow_only", "frozen"}
                and not autonomy.get("error"),
                evidence_ok=bool(autonomy.get("read_only", True)),
                details={
                    "score": autonomy.get("score"),
                    "posture": autonomy.get("posture"),
                    "read_only": autonomy.get("read_only", True),
                },
            ),
            _gate(
                "incident_control_v1",
                str(incident.get("schema_version") or "") == "runtime_incident_control.v1"
                and str(incident.get("mode") or "") in {"normal", "shadow_only", "no_new_risk", "only_close", "frozen"}
                and not incident.get("error"),
                details={
                    "mode": incident.get("mode"),
                    "valid_modes": incident.get("valid_modes") or [],
                    "risk_policy_gate": True,
                },
            ),
            _gate(
                "release_run_ledger_v1",
                release_capability_ok,
                evidence_ok=release_evidence_ok,
                details={
                    "latest_run_id": latest_release.get("run_id"),
                    "latest_status": latest_release.get("status"),
                    "runtime_config_hash": latest_release.get("runtime_config_hash"),
                    "replay_run_id": latest_release.get("replay_run_id"),
                },
            ),
        ]
        implementation_complete = all(bool(gate.get("ok")) for gate in gates)
        operational_evidence = [gate for gate in gates if "evidence_ok" in gate]
        operationally_ready = implementation_complete and all(bool(gate.get("evidence_ok")) for gate in operational_evidence)
        blockers = [gate["name"] for gate in gates if not gate.get("ok")]
        evidence_gaps = [gate["name"] for gate in operational_evidence if not gate.get("evidence_ok")]
        return {
            "schema_version": "v15_phase0_completion.v1",
            "implementation_complete": implementation_complete,
            "operationally_ready": operationally_ready,
            "status": "complete" if implementation_complete else "incomplete",
            "operational_status": "ready" if operationally_ready else "needs_evidence",
            "blockers": blockers,
            "evidence_gaps": evidence_gaps,
            "gates": gates,
            "phase1_candidates": [
                "order_outcome_causality_replay",
                "autonomy_health_enforcement_binding",
                "incident_playbook_event_binding",
                "web_cockpit_pages",
            ],
            "read_only": True,
            "updated_at": time.time(),
        }
