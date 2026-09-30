from __future__ import annotations

import json
import time

import pytest

from alpha.decision_policy import WeightDecision
from backend.core.db import STATE_DB_DDL, connect_sqlite
from backend.services.autonomous_evolution_runner import AutonomousEvolutionNurseryRunner
from backend.services.factor_weight_change import FactorWeightChangeService
from backend.services.release_control import ReleaseControlService
from backend.services import replay_harness as replay_harness_module
from backend.services.replay_harness import ReplayHarnessService


class _LargeWeightReductionPolicy:
    def decide(self, **_kwargs):
        return {
            "alpha_x": WeightDecision(
                factor="alpha_x",
                old_weight=1.0,
                new_weight=0.7,
                reason="test governed reduction",
                confidence=0.9,
            )
        }

    fast_decide = decide


def _currentcode_version() -> str:
    return replay_harness_module.code_version()


def _init_state(db_path, *, config_hash: str = "cfg-current") -> None:
    conn = connect_sqlite(db_path)
    try:
        conn.executescript(STATE_DB_DDL)
        conn.execute(
            """
            INSERT INTO runtime_config_snapshot
            (config_hash, source, config_json, run_id, created_at)
            VALUES (?, 'test', '{}', 'snapshot-test', ?)
            """,
            (config_hash, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _insert_replay(
    db_path,
    *,
    run_id: str,
    kind: str,
    created_at: float,
    config_hash: str = "cfg-current",
    grade: str = "A",
    status: str = "completed",
    code_version: str | None = None,
    dataset_hash: str = "dataset-hash",
    artifact_hash: str = "artifact-hash",
) -> None:
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO replay_report
            (replay_run_id, scope_json, input_dataset_hash, runtime_config_hash,
             code_version, decision_count, matched_live_count, mismatch_count,
             metric_summary_json, replay_error, evidence_grade, artifact_path,
             artifact_hash, status, created_at)
            VALUES (?, ?, ?, ?, ?, 1, 1, 0, '{}', '', ?,
                    '/tmp/replay.json', ?, ?, ?)
            """,
            (
                run_id,
                json.dumps({"schema_version": "replay_scope.v1", "kind": kind}),
                dataset_hash,
                config_hash,
                _currentcode_version() if code_version is None else code_version,
                grade,
                artifact_hash,
                status,
                created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_governance_replay_status_uses_full_evidence_not_newer_freshness(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    now = time.time()
    _insert_replay(
        db_path,
        run_id="full-evidence",
        kind="bar_replay_evidence",
        created_at=now - 10,
    )
    _insert_replay(
        db_path,
        run_id="newer-freshness",
        kind="bar_replay_freshness",
        created_at=now,
        grade="B",
    )

    result = ReplayHarnessService(db_path).status()

    assert result["ok"] is True
    assert result["status"] == "fresh"
    assert result["latest_report"]["replay_run_id"] == "full-evidence"
    assert result["latest_report"]["scope"]["kind"] == "bar_replay_evidence"


def test_governance_replay_status_rejects_old_runtime_config_binding(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path, config_hash="cfg-current")
    _insert_replay(
        db_path,
        run_id="old-config-evidence",
        kind="bar_replay_evidence",
        created_at=time.time(),
        config_hash="cfg-old",
    )

    result = ReplayHarnessService(db_path).status()

    assert result["ok"] is False
    assert result["status"] == "degraded"
    assert "runtime_config_hash_mismatch" in result["blockers"]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"dataset_hash": ""}, "input_dataset_hash_missing"),
        ({"artifact_hash": ""}, "artifact_hash_missing"),
        ({"code_version": ""}, "code_version_missing"),
        ({"code_version": "different-head"}, "code_version_mismatch"),
    ],
)
def test_governance_replay_status_rejects_incomplete_bindings(
    tmp_path,
    override,
    reason,
):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="incomplete-binding",
        kind="bar_replay_evidence",
        created_at=time.time(),
        **override,
    )

    result = ReplayHarnessService(db_path).status()

    assert result["ok"] is False
    assert reason in result["blockers"]


def test_release_checklist_rejects_freshness_report_as_governance_evidence(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="freshness-only",
        kind="bar_replay_freshness",
        created_at=time.time(),
        grade="B",
    )

    checklist = ReleaseControlService(db_path).build_checklist(
        readiness={"ready_for_release": True},
    )

    assert checklist["ok"] is False
    assert checklist["replay"]["ok"] is False
    assert checklist["replay"]["scope_kind"] == "bar_replay_evidence"


def test_nursery_light_readiness_reuses_normalized_replay_status(tmp_path):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="full-evidence",
        kind="bar_replay_evidence",
        created_at=time.time(),
    )

    readiness = AutonomousEvolutionNurseryRunner(db_path).build_light_readiness()

    assert readiness["replay"]["ok"] is True
    assert readiness["replay"]["latest_report"]["replay_run_id"] == "full-evidence"


def test_factor_weight_execute_rejects_freshness_as_replay_evidence(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "state.db"
    _init_state(db_path)
    _insert_replay(
        db_path,
        run_id="freshness-only",
        kind="bar_replay_freshness",
        created_at=time.time(),
        grade="B",
    )
    service = FactorWeightChangeService(db_path)
    monkeypatch.setattr(
        service.admission,
        "evaluate",
        lambda **_kwargs: {"allowed": True, "status": "admitted"},
    )
    monkeypatch.setattr(
        "backend.services.factor_weight_change.ExperiencePriorService.priors",
        lambda _self: {},
    )

    result = service.execute(
        source="test_governed_weight",
        producer="test",
        run_id="run-freshness-only",
        actor="system:test",
        reason="replay contract test",
        factor_configs={"alpha_x": {"role": "alpha"}},
        current_weights={"alpha_x": 1.0},
        decision_policy=_LargeWeightReductionPolicy(),
        risk_check=lambda _plan: {"allowed": True},
    )

    assert result["status"] == "blocked_by_replay"
    assert result["applications"] == {}
    conn = connect_sqlite(db_path, read_only=True)
    try:
        assert conn.execute("SELECT COUNT(*) FROM learning_application_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM runtime_config_overlay").fetchone()[0] == 0
    finally:
        conn.close()


def test_live_state_only_denial_is_not_a_replay_disagreement():
    """A live denial from a live-only gate is missing state, not divergence.

    Counting the supervisor reentry cooldown / entry-cluster same-direction
    cooldown / session loss streak / learned
    entry threshold denials as risk-policy disagreements forced every
    governance replay to grade C, which left replay admission blocked and
    every approved factor weight suggestion unapplied (`blocked_by_replay`).
    """
    from backend.services.replay_harness import (
        ReplayHarnessService,
        _is_live_state_only_denial,
        _verdict_signature,
    )

    live = _verdict_signature({"allowed": False, "reason": "supervisor_reentry_cooldown"})
    replay = _verdict_signature({"allowed": True, "reason": "ok"})
    assert _is_live_state_only_denial(live, replay) is True
    assert _is_live_state_only_denial(
        _verdict_signature({"allowed": False, "reason": "learning_same_direction_cooldown"}),
        replay,
    ) is True

    # A genuine divergence still counts as a disagreement, in both directions.
    assert _is_live_state_only_denial(
        _verdict_signature({"allowed": True, "reason": "ok"}),
        _verdict_signature({"allowed": False, "reason": "max_exposure"}),
    ) is False
    assert _is_live_state_only_denial(
        _verdict_signature({"allowed": False, "reason": "unknown_live_gate"}),
        _verdict_signature({"allowed": True, "reason": "ok"}),
    ) is False

    grade = ReplayHarnessService._p1_replay_grade(
        "A",
        {"bar_window_coverage": 1.0, "stale_bar_alignment_count": 0},
        {"factor_frame_coverage": 1.0},
        {"disagreement_count": 0, "error_count": 0},
        {"disagreement_count": 0, "error_count": 0},
        80,
    )
    assert grade == "A"


def test_every_live_only_allow_trade_gate_is_classified():
    """A gate added to allow_trade must declare the runtime input it reads.

    The 2026-09-13 fix enumerated four cooldown/threshold reasons; the next
    live-only gate to fire (``daily_trade_limit``, 2026-09-30) was missed and
    put the replay back to grade C. Driving the real ``allow_trade`` with one
    tripped field per gate makes an unclassified gate fail here instead of
    silently degrading every governance replay.
    """
    from backend.services.replay_harness import _is_live_state_only_denial
    from risk.governor import GovernorState, RiskGovernor, live_state_only_denial_input

    def governor() -> RiskGovernor:
        # Thresholds no default field can reach, so each case trips one gate.
        return RiskGovernor(
            max_drawdown_pct=99.0,
            max_consecutive_losses=99,
            max_daily_loss_pct=99.0,
            max_daily_trades=99,
            min_bridge_uptake=True,
            data_lag_max_seconds=99.0,
            loss_cooldown_after_losses=0,
            loss_cooldown_bars=0,
            circuit_breaker_bypass=False,
        )

    cases = {
        "circuit_broken": {"circuit_broken": True},
        "loop_not_running": {"loop_running": False},
        "bridge_disconnected": {"bridge_connected": False},
        "drawdown_too_high": {"drawdown_pct": 100.0},
        "consecutive_losses": {"consecutive_losses": 100},
        "loss_cooldown_active": {
            "consecutive_losses": 100,
            "timeframe_seconds": 300,
            "seconds_since_last_trade": 10,
            "extra": {"loss_cooldown_after_losses": 2, "loss_cooldown_bars": 3},
        },
        "daily_loss_limit": {"daily_loss_pct": 100.0},
        "daily_trade_limit": {"daily_trades": 100},
        "data_lag": {"data_lag_seconds": 100000.0},
        "disk_space_critical": {
            "extra": {
                "runtime_health": {
                    "system_health": {"component_status": {"disk_space": "critical"}}
                }
            }
        },
    }
    for expected_reason, fields in cases.items():
        verdict = governor().allow_trade(GovernorState(**fields))
        assert verdict.allowed is False, expected_reason
        assert verdict.reason == expected_reason
        assert live_state_only_denial_input(verdict.reason) is not None, expected_reason
        assert _is_live_state_only_denial(
            (False, verdict.reason), (True, "ok")
        ) is True, expected_reason

    # A config-only denial is not live state: the replay sees the same config.
    dry = governor()
    dry.set_dry_run(True)
    forced = dry.allow_trade(GovernorState())
    assert forced.reason == "force_dry_run"
    assert live_state_only_denial_input(forced.reason) is None
    assert _is_live_state_only_denial((False, forced.reason), (True, "ok")) is False

    # Dynamic reason from the loss-streak ladder is classified by prefix.
    assert live_state_only_denial_input("loss_streak_session_locked") is not None
    assert live_state_only_denial_input("max_exposure") is None


def test_unreconstructible_volume_denial_is_not_a_replay_disagreement():
    """Both sides deny but the recompute never reached a gate.

    SKIP / gate-denied rows persist no requested volume, so the rebuilt
    context carries 0.0 and RiskPolicyService short-circuits on
    non_positive_requested_volume before any gate; the gate states behind
    the live denial are passed empty as well. The outcome agrees, so this
    is an input gap, not evidence of divergence. Counting it as a
    disagreement forces grade C on any slice containing one such row,
    which blocks every >=0.10 weight change.
    """
    from backend.services.replay_harness import (
        _is_unreconstructible_volume_denial,
        _verdict_signature,
    )

    assert _is_unreconstructible_volume_denial(
        _verdict_signature({"allowed": False, "reason": "supervisor_reentry_cooldown"}),
        _verdict_signature({"allowed": False, "reason": "non_positive_requested_volume"}),
    ) is True
    assert _is_unreconstructible_volume_denial(
        _verdict_signature({"allowed": False, "reason": "learning_weak_signal_threshold"}),
        _verdict_signature({"allowed": False, "reason": "non_positive_requested_volume"}),
    ) is True

    # Genuine divergences still count: an allow/deny flip in either direction.
    assert _is_unreconstructible_volume_denial(
        _verdict_signature({"allowed": True, "reason": "ok"}),
        _verdict_signature({"allowed": False, "reason": "non_positive_requested_volume"}),
    ) is False
    # ... and two real gate denials that simply differ.
    assert _is_unreconstructible_volume_denial(
        _verdict_signature({"allowed": False, "reason": "supervisor_reentry_cooldown"}),
        _verdict_signature({"allowed": False, "reason": "max_exposure"}),
    ) is False

    # The current slice shape (no true disagreements, B-level coverage)
    # grades B once the misclassification is fixed.
    grade = ReplayHarnessService._p1_replay_grade(
        "B",
        {"bar_window_coverage": 1.0, "stale_bar_alignment_count": 0},
        {"factor_frame_coverage": 1.0},
        {"disagreement_count": 0, "error_count": 0},
        {"disagreement_count": 0, "error_count": 0},
        80,
    )
    assert grade == "B"


def test_risk_recompute_routes_volume_shortcircuit_to_input_gap(monkeypatch):
    """Wire-level guard for the volume-gap branch (not just the classifier).

    A SKIP row whose live verdict is a genuine gate denial but which
    persisted no requested volume must land in input_gap_count, never in
    disagreement_count; a genuine allow/deny flip must still disagree.
    """
    from backend.services.replay_harness import ReplayHarnessService
    import risk.policy_service as risk_policy_service

    class _Verdict:
        def __init__(self, allowed, reason):
            self._d = {"allowed": allowed, "reason": reason}

        def to_dict(self):
            return dict(self._d)

    class _Policy:
        def __init__(self, verdict):
            self._verdict = verdict

        def evaluate(self, _action, _context):
            return self._verdict

    risk_inputs = {
        "schema_version": "open_trade_risk_replay_inputs.v1",
        "max_position_count": 3,
        "max_position_api_volume": 300.0,
        "pyramid_enabled": False,
        "loss_cooldown_after_losses": 2,
        "loss_cooldown_bars": 12,
        "block_on_disk_critical": True,
        "runtime_incident_mode": "",
        "autonomy_mode": "",
        "live_autonomy_unlocked": False,
        "live_autonomy_unlock_id": "",
        "risk_limits": {
            "schema_version": "risk_limit_snapshot.v1",
            "source": "test",
            "max_drawdown_pct": 5.0,
            "max_consecutive_losses": 3,
            "max_daily_loss_pct": 3.0,
            "max_daily_trades": 10,
            "data_lag_max_seconds": 30,
            "loss_cooldown_after_losses": 2,
            "loss_cooldown_bars": 12,
            "block_on_disk_critical": True,
            "var_threshold_pct": 2.0,
            "cvar_threshold_pct": 3.0,
            "circuit_breaker_bypass": False,
        },
        "var": {"enabled": False, "threshold_pct": 0.0, "cvar_threshold_pct": 0.0},
    }

    def _row(decision_id, live_reason, replay_verdict):
        row = {
            "decision_id": decision_id,
            "action_json": json.dumps(
                {
                    "score": 0.5,
                    "risk_verdict": {
                        "allowed": False,
                        "reason": live_reason,
                        "audit_payload": {"temporal_context": {"timeframe_seconds": 300}},
                    },
                    "execution_context": {"requested_volume": 0.0},
                    "risk_replay_inputs": risk_inputs,
                }
            ),
            "risk_state_json": json.dumps({}),
            "portfolio_state_json": json.dumps({"balance": 10000.0, "equity": 10000.0}),
        }
        monkeypatch.setattr(
            risk_policy_service.RiskPolicyService,
            "shared",
            classmethod(lambda _cls: _Policy(replay_verdict)),
        )
        return ReplayHarnessService()._evaluate_risk_policy_recompute([row])["metrics"]

    gap = _row(
        "dec_gap",
        "supervisor_reentry_cooldown",
        _Verdict(False, "non_positive_requested_volume"),
    )
    assert gap["attempted_count"] == 1
    assert gap["disagreement_count"] == 0
    assert gap["input_gap_count"] == 1

    flip = _row(
        "dec_flip",
        "max_exposure",
        _Verdict(True, "ok"),
    )
    assert flip["disagreement_count"] == 1
    assert flip["input_gap_count"] == 0


def _pre_policy_report(tmp_path, monkeypatch, skip_action: dict):
    import json as _json

    from backend.services import replay_harness as rh

    monkeypatch.setattr(
        rh, "current_runtime_config_snapshot", lambda **_kw: {"config_hash": "cfg_hash"}
    )
    service = object.__new__(rh.ReplayHarnessService)
    service.db_path = tmp_path / "state.db"
    verdict = {"allowed": True, "reason": "ok"}
    rows = [
        {
            "decision_id": "dec_evaluated",
            "event_type": "open",
            "decision_ts": 1.0,
            "factor_snapshot_count": 1,
            "action_json": _json.dumps(
                {"gate_passed": True, "gate_reason": "pass", "risk_verdict": verdict}
            ),
            "risk_state_json": _json.dumps({"policy_verdict": verdict}),
            "portfolio_state_json": "{}",
        },
        {
            "decision_id": "dec_skip",
            "event_type": "skip",
            "decision_ts": 2.0,
            "factor_snapshot_count": 1,
            "action_json": _json.dumps(skip_action),
            "risk_state_json": "{}",
            "portfolio_state_json": "{}",
        },
    ]
    return service._build_report(
        run_id="replay_pre_policy",
        scope={},
        rows=rows,
        created_at=0.0,
        replay_error="",
    )


def test_pre_policy_skip_has_no_verdict_to_cover(tmp_path, monkeypatch):
    """A row recording RiskPolicy as never reached is neither gap nor mismatch.

    `build_skip_ledger_payload` persists a pre-candidate admission blocker
    without a risk verdict on purpose, and marks it with the explicit
    stage/boolean. Counting those rows as mismatches capped every report
    containing one at grade C, and they also dragged verdict coverage below
    the 0.80 gate - which, since the release gate was removed, is the only
    admission left for a >=0.10 weight change.
    """
    report = _pre_policy_report(
        tmp_path,
        monkeypatch,
        {
            "gate_passed": True,
            "gate_reason": "pass",
            "skip_stage": "before_candidate",
            "risk_stage": "not_reached",
            "risk_policy_reached": False,
        },
    )
    metrics = report["metric_summary"]

    assert report["decision_count"] == 2
    assert report["mismatch_count"] == 0
    assert report["evidence_grade"] == "A"
    assert metrics["pre_policy_skip_count"] == 1
    assert metrics["risk_verdict_decision_count"] == 1
    assert metrics["risk_verdict_coverage"] == 1.0


def test_skip_without_explicit_marker_still_counts_as_mismatch(tmp_path, monkeypatch):
    """The exemption is bound to the explicit authority, not to `event_type`.

    A skip row that simply carries no verdict is an evidence gap and must keep
    counting, otherwise any unwritten verdict would silently pass grading.
    """
    report = _pre_policy_report(
        tmp_path,
        monkeypatch,
        {"gate_passed": True, "gate_reason": "pass"},
    )
    metrics = report["metric_summary"]

    assert report["mismatch_count"] == 1
    assert metrics["pre_policy_skip_count"] == 0
    assert metrics["risk_verdict_coverage"] == 0.5
    assert report["evidence_grade"] == "C"
