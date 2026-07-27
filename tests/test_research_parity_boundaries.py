from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from backend.services import parity_replay as parity_replay_module
from backend.services.governance_eligibility import evaluate_governance_eligibility
from backend.services.model_influence_governance import ModelInfluenceGovernanceService
from backend.services.parameter_template_validation import ParameterTemplateValidationService
from backend.services.parity_replay import ParityReplayRequest, ParityReplayRunner
from backend.services.research_evidence import (
    ResearchEvidenceRejected,
    legacy_backtest_contract,
    require_executable_research_evidence,
)


def _hash(value) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _legacy_evidence(**overrides):
    return {
        **legacy_backtest_contract(),
        **overrides,
    }


def _valid_parity_evidence() -> dict:
    binding_inputs = {
        "config_hash": "a" * 64,
        "data_hash": "b" * 64,
        "code_hash": "c" * 64,
        "artifact_hash": "d" * 64,
    }
    binding_hash = hashlib.sha256(
        json.dumps(
            binding_inputs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "parity_replay_report.v1",
        "contract": "parity_replay_contract.v1",
        "status": "parity_verified",
        "engine": "live_parity_replay_v1",
        "evidence_class": "live_parity",
        "live_parity": True,
        "governance_eligible": True,
        "deployable_candidate": True,
        "bindings": {**binding_inputs, "binding_hash": binding_hash},
        "binding_verification": {
            "expected": dict(binding_inputs),
            "required_expected_names": list(binding_inputs),
            "missing_expected": [],
            "verified": True,
            "mismatches": [],
        },
        "data_source": {"source": "monthly_pit_bars", "point_in_time": True},
        "causality": {
            "closed_bar_only": True,
            "next_bar_execution": True,
            "native_bid_ask": True,
        },
        "components": {
            name: {"reuse": "exact", "verified": True}
            for name in (
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
        },
        "diagnostic_reasons": [],
    }


def _bars(*, include_bid_ask: bool = True) -> pd.DataFrame:
    rows = [
        {"time": 1000.0, "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 1.0},
        {"time": 1900.0, "open": 101.0, "high": 101.6, "low": 100.4, "close": 101.2, "volume": 1.0},
        {"time": 2800.0, "open": 102.0, "high": 102.4, "low": 101.6, "close": 102.2, "volume": 1.0},
        {"time": 3700.0, "open": 102.1, "high": 102.5, "low": 101.8, "close": 102.3, "volume": 1.0},
    ]
    if include_bid_ask:
        quotes = [
            (99.9, 100.4, 99.4, 99.9, 100.1, 100.6, 99.6, 100.1),
            (100.9, 101.5, 100.3, 101.1, 101.1, 101.7, 100.5, 101.3),
            (101.9, 102.3, 101.5, 102.1, 102.1, 102.5, 101.7, 102.3),
            (102.0, 102.4, 101.7, 102.2, 102.2, 102.6, 101.9, 102.4),
        ]
        for row, values in zip(rows, quotes):
            for name, value in zip(
                (
                    "bid_open", "bid_high", "bid_low", "bid_close",
                    "ask_open", "ask_high", "ask_low", "ask_close",
                ),
                values,
            ):
                row[name] = value
    return pd.DataFrame(rows)


def _learning_report() -> dict:
    trades = []
    for index, pnl in enumerate((5.0, -2.0, 4.0, 3.0)):
        trades.append({
            "decision_index": index,
            "decision_ts": 1000.0 + index * 300,
            "entry_index": index + 1,
            "entry_ts": 1300.0 + index * 300,
            "exit_index": index + 2,
            "exit_ts": 1600.0 + index * 300,
            "direction": 1 if index % 2 == 0 else -1,
            "net_pnl": pnl,
            "decision_candidate": {
                "direction": 1 if index % 2 == 0 else -1,
                "score": 0.7,
                "signals": {"rsi_14": {"contribution": 0.4, "confidence": 0.8}},
                "factor_values": {},
            },
            "decision_bar": {
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "bid_close": 100.4,
                "ask_close": 100.6,
            },
        })
    return {
        "bindings": {"binding_hash": "a" * 64},
        "causality": {
            "closed_bar_only": True,
            "next_bar_execution": True,
            "native_bid_ask": True,
        },
        "data_source": {"point_in_time": True},
        "artifact_manifest": {"selected_factor_ids": ["rsi_14"]},
        "diagnostic_reasons": [],
        "trades": trades,
    }


def test_replay_learning_bundle_has_deterministic_ids_and_current_schemas():
    first = parity_replay_module._build_learning_bundle(_learning_report())
    second = parity_replay_module._build_learning_bundle(_learning_report())
    assert first["trainable"] is True
    assert first["feature_schemas"] == {
        "open": "pit.v2.open_lineage",
        "factor": "pit.v2.factor_rolling_lineage",
    }
    assert [item["sample_id"] for item in first["open_samples"]] == [
        item["sample_id"] for item in second["open_samples"]
    ]
    assert first["independent_trade_count"] == 4
    assert first["open_sample_count"] == 4
    assert first["factor_sample_count"] == 1


def test_replay_learning_blocker_prevents_training_rows():
    report = _learning_report()
    report["causality"]["closed_bar_only"] = False
    bundle = parity_replay_module._build_learning_bundle(report)
    assert bundle["trainable"] is False
    assert "closed_bar_contract_failed" in bundle["blockers"]
    assert bundle["open_samples"] == []
    assert bundle["factor_samples"] == []


def test_recorded_spread_builds_simulated_executable_quotes_without_claiming_native():
    bars = _bars(include_bid_ask=False)
    bars["spread"] = 0.2
    report = _runner(bars=bars)
    assert report["causality"]["native_bid_ask"] is False
    assert report["causality"]["executable_bid_ask"] is True
    assert report["causality"]["quote_model"] == "recorded_spread_around_ohlc_mid"
    assert report["metrics"]["independent_trade_count"] == 1


def test_unclosed_requested_bar_is_reported_and_cannot_train():
    bars = _bars()
    bars.loc[len(bars)] = {**bars.iloc[-1].to_dict(), "time": 4500.0}
    report = _runner(bars=bars)
    assert "unclosed_bar_present_in_requested_window" in report["diagnostic_reasons"]
    assert report["learning_bundle"]["trainable"] is False


def test_monthly_loader_caps_target_in_sql_and_loads_only_preceding_warmup(tmp_path):
    path = tmp_path / "bars_2026_07.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute(
        """
        CREATE TABLE bars AS
        SELECT
            'XAUUSD+'::VARCHAR AS symbol,
            'M5'::VARCHAR AS timeframe,
            (1000 + i * 300)::DOUBLE AS time,
            (100 + i)::DOUBLE AS open,
            (101 + i)::DOUBLE AS high,
            (99 + i)::DOUBLE AS low,
            (100.5 + i)::DOUBLE AS close,
            TRUE AS complete
        FROM range(10) AS t(i)
        """
    )
    conn.close()

    bars, metadata = parity_replay_module.MonthlyPITBarLoader(tmp_path).load(
        ParityReplayRequest(
            start=1000.0,
            end=5000.0,
            max_bars=3,
            warmup_bars=2,
        )
    )

    assert bars["time"].tolist() == [2500.0, 2800.0, 3100.0, 3400.0, 3700.0]
    assert metadata["target_bar_count"] == 3
    assert metadata["warmup_bar_count"] == 2
    assert metadata["target_start_ts"] == 3100.0


def test_replay_warmup_bars_prime_state_without_creating_trades():
    def provider(_history, _bar, _index):
        return {"direction": 1, "sl_distance": 5.0, "tp_distance": 1.0}

    report = _runner(
        bars=_bars(),
        provider=provider,
        data_source={
            "source": "monthly_pit_bars",
            "source_files": ["fixture"],
            "target_start_ts": 2800.0,
            "target_bar_count": 2,
            "warmup_bar_count": 2,
        },
    )

    decisions = [
        event for event in report["events"] if event["event"] == "closed_bar_decision"
    ]
    assert decisions[0]["bar_index"] == 2
    assert report["metrics"]["bar_count"] == 2
    assert report["metrics"]["warmup_bar_count"] == 2


def _runner(
    *,
    bars: pd.DataFrame,
    expected_bindings=None,
    provider=None,
    data_source=None,
) -> dict:
    config = SimpleNamespace(
        risk_max_holding_bars=288,
        position_supervisor_template_id="position_supervisor:default.v1",
    )
    request = ParityReplayRequest(
        timeframe="M15",
        as_of=5000.0,
        max_bars=100,
        warmup_bars=0,
        initial_equity=10_000.0,
        volume_lots=0.01,
        contract_size=100.0,
        commission_per_lot_round_turn=6.0,
        slippage_bps=0.0,
        persist_artifact=False,
        expected_bindings=expected_bindings or {},
    )
    seen: list[tuple[int, int, float]] = []

    def default_provider(history, bar, index):
        seen.append((len(history), index, float(history["time"].max())))
        if index == 0:
            return {"direction": 1, "sl_distance": 5.0, "tp_distance": 1.0}
        return {}

    report = ParityReplayRunner(
        request=request,
        config=config,
        # SimpleNamespace has no to_dict, so the bound payload is exactly {}.
        config_snapshot={"config_version": 7, "config_hash": _hash({}), "source": "test"},
        decision_provider=provider or default_provider,
        risk_evaluator=lambda _context: {"allowed": True, "reason": "test"},
        supervisor_evaluator=lambda _context: {"action": "hold", "reason": "test"},
    ).run(
        bars,
        data_source=(
            {"source": "monthly_pit_bars", "source_files": ["fixture"]}
            if data_source is None
            else data_source
        ),
    )
    report["_seen_history"] = seen
    return report


def test_parity_replay_risk_context_advances_from_last_completed_trade():
    risk_contexts: list[dict] = []

    def provider(_history, _bar, index):
        if index in {0, 3}:
            return {"direction": 1, "sl_distance": 5.0, "tp_distance": 1.0}
        return {}

    def evaluate_risk(context):
        risk_contexts.append(dict(context))
        return {"allowed": True, "reason": "test"}

    config = SimpleNamespace(
        risk_max_holding_bars=288,
        position_supervisor_template_id="position_supervisor:default.v1",
    )
    report = ParityReplayRunner(
        request=ParityReplayRequest(
            timeframe="M15",
            as_of=5000.0,
            max_bars=100,
            warmup_bars=0,
            initial_equity=10_000.0,
            volume_lots=0.01,
            contract_size=100.0,
            commission_per_lot_round_turn=6.0,
            slippage_bps=0.0,
            persist_artifact=False,
        ),
        config=config,
        config_snapshot={"config_version": 7, "config_hash": _hash({}), "source": "test"},
        decision_provider=provider,
        risk_evaluator=evaluate_risk,
        supervisor_evaluator=lambda _context: {"action": "hold", "reason": "test"},
    ).run(
        _bars(),
        data_source={"source": "monthly_pit_bars", "source_files": ["fixture"]},
    )

    assert report["metrics"]["independent_trade_count"] == 1
    assert [context["session_last_trade_ts"] for context in risk_contexts] == [
        0.0,
        2800.0,
    ]


def test_parity_replay_resets_session_trade_state_at_utc_day_boundary():
    bars = _bars()
    bars.loc[3, "time"] = 90_000.0
    risk_contexts: list[dict] = []

    def provider(_history, _bar, index):
        if index in {0, 3}:
            return {"direction": 1, "sl_distance": 5.0, "tp_distance": 1.0}
        return {}

    config = SimpleNamespace(
        risk_max_holding_bars=288,
        position_supervisor_template_id="position_supervisor:default.v1",
    )
    ParityReplayRunner(
        request=ParityReplayRequest(
            timeframe="M15",
            as_of=100_000.0,
            warmup_bars=0,
            persist_artifact=False,
        ),
        config=config,
        config_snapshot={"config_version": 7, "config_hash": _hash({}), "source": "test"},
        decision_provider=provider,
        risk_evaluator=lambda context: (
            risk_contexts.append(dict(context))
            or {"allowed": True, "reason": "test"}
        ),
        supervisor_evaluator=lambda _context: {"action": "hold", "reason": "test"},
    ).run(
        bars,
        data_source={"source": "monthly_pit_bars", "source_files": ["fixture"]},
    )

    assert risk_contexts[1]["session_last_trade_ts"] == 0.0
    assert risk_contexts[1]["session_state"]["trades"] == 0
    assert risk_contexts[1]["session_state"]["pnl"] == 0.0


def test_legacy_evidence_is_zero_weight_even_if_flags_are_spoofed():
    evidence = _legacy_evidence(
        live_parity=True,
        governance_eligible=True,
        deployable_candidate=True,
        matured=True,
        integrity="full",
        model_ready=True,
        executable_governance_allowed=True,
        lineage_complete=True,
        lineage_unique=True,
        lineage_ids=["legacy-run"],
    )
    eligibility = evaluate_governance_eligibility(evidence)
    assert eligibility.eligible is False
    assert eligibility.effective_weight == 0.0
    assert any("legacy_indicator_sweep" in reason for reason in eligibility.exclusion_reasons)
    with pytest.raises(ResearchEvidenceRejected):
        require_executable_research_evidence(evidence, executable_use="deploy")


def test_legacy_outer_envelope_cannot_be_hidden_by_nested_parity_claims():
    nested = {
        "schema_version": "parity_replay_report.v1",
        "contract": "parity_replay_contract.v1",
        "status": "parity_verified",
        "engine": "live_parity_replay_v1",
        "evidence_class": "live_parity",
        "live_parity": True,
        "governance_eligible": True,
        "deployable_candidate": True,
        "bindings": {name: "a" * 64 for name in (
            "config_hash", "data_hash", "code_hash", "artifact_hash", "binding_hash"
        )},
        "binding_verification": {"verified": True, "mismatches": []},
        "causality": {
            "closed_bar_only": True,
            "next_bar_execution": True,
            "native_bid_ask": True,
        },
        "components": {
            name: {"reuse": "exact", "verified": True}
            for name in (
                "factor_frame", "runtime_selector", "streaming_factor_engine",
                "normalizer", "compositor", "execution_gate", "risk_policy",
                "lifecycle", "supervisor",
            )
        },
    }
    evidence = {**_legacy_evidence(), "research_evidence": nested}
    with pytest.raises(ResearchEvidenceRejected) as exc:
        require_executable_research_evidence(evidence, executable_use="deploy")
    assert "legacy_indicator_sweep_diagnostic_only" in exc.value.verdict.blockers


def test_legacy_parameter_candidate_cannot_be_approved_or_deployed(tmp_path):
    service = ParameterTemplateValidationService(str(tmp_path / "state.db"))
    candidate = service.register_release_candidate(
        factor_id="rsi_14",
        template_id="template:legacy",
        regime_key="",
        boundary={},
        walk_forward={"passed": True},
        validation_report_path=str(tmp_path / "report.json"),
        research_evidence=_legacy_evidence(),
    )
    assert candidate["status"] == "diagnostic_only"
    assert candidate["validation_summary"]["research_evidence_verdict"]["allowed"] is False
    with pytest.raises(ResearchEvidenceRejected):
        service.review_release_candidate(candidate_id=candidate["candidate_id"], status="approved")
    with pytest.raises(ResearchEvidenceRejected):
        service.deploy_release_candidate(candidate_id=candidate["candidate_id"])
    assert service.list_release_candidates(status="approved") == []
    assert service.list_release_candidates(status="deployed") == []


def test_parameter_candidate_missing_research_metadata_is_quarantined(tmp_path):
    service = ParameterTemplateValidationService(str(tmp_path / "state.db"))
    candidate = service.register_release_candidate(
        factor_id="rsi_14",
        template_id="template:missing-evidence",
        regime_key="",
        boundary={},
        walk_forward={"passed": True},
        validation_report_path=str(tmp_path / "missing.json"),
    )

    assert candidate["status"] == "legacy_quarantined"
    assert candidate["legacy_quarantined"] is True
    assert candidate["require_revalidation"] is True
    assert candidate["research_evidence_gate"]["state"] == "require_revalidation"
    assert "engine_missing" in candidate["research_evidence_gate"]["blockers"]

    with pytest.raises(ResearchEvidenceRejected):
        service.review_release_candidate(
            candidate_id=candidate["candidate_id"],
            status="approved",
        )
    with pytest.raises(ResearchEvidenceRejected):
        service.deploy_release_candidate(candidate_id=candidate["candidate_id"])


def test_historical_approved_candidate_is_legacy_quarantined_and_cannot_deploy(tmp_path):
    db_path = str(tmp_path / "state.db")
    service = ParameterTemplateValidationService(db_path)
    now = 100.0
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO parameter_template_release_candidate
            (candidate_id, factor_id, template_id, regime_key, status,
             boundary_json, validation_summary_json, validation_report_path,
             created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ptrc_historical",
                "rsi_14",
                "template:historical",
                "",
                "approved",
                "{}",
                json.dumps({"research_evidence_verdict": {"allowed": True}}),
                str(tmp_path / "old.json"),
                now,
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    candidate = service.get_release_candidate("ptrc_historical")
    assert candidate is not None
    assert candidate["status"] == "legacy_quarantined"
    assert candidate["legacy_status"] == "approved"
    assert candidate["research_evidence_gate"]["legacy_record"] is True
    assert candidate["research_evidence_gate"]["reason"] == "candidate_research_policy_marker_missing"
    with pytest.raises(ResearchEvidenceRejected):
        service.deploy_release_candidate(candidate_id="ptrc_historical")

    persisted = service.get_release_candidate("ptrc_historical")
    assert persisted is not None
    assert persisted["status"] == "legacy_quarantined"
    assert persisted["validation_summary"]["require_revalidation"] is True


@pytest.mark.parametrize(
    "component_name",
    [
        "position_path_metrics",
        "safety_arbitration",
        "trailing",
        "protection_planner",
        "cost_model",
    ],
)
def test_executable_research_gate_requires_every_lifecycle_component(component_name):
    evidence = _valid_parity_evidence()
    evidence["components"].pop(component_name)

    with pytest.raises(ResearchEvidenceRejected) as exc:
        require_executable_research_evidence(evidence, executable_use="deploy")

    assert f"component_{component_name}_not_exact" in exc.value.verdict.blockers


def test_executable_research_gate_requires_explicit_expected_hash_preconditions():
    evidence = _valid_parity_evidence()
    evidence["binding_verification"]["missing_expected"] = ["artifact_hash"]
    evidence["binding_verification"]["expected"].pop("artifact_hash")

    with pytest.raises(ResearchEvidenceRejected) as exc:
        require_executable_research_evidence(evidence, executable_use="deploy")

    assert "binding_expected_preconditions_missing" in exc.value.verdict.blockers
    assert "binding_expected_artifact_hash_unverified" in exc.value.verdict.blockers


def test_verified_parity_evidence_allows_parameter_review_and_deploy(tmp_path, monkeypatch):
    service = ParameterTemplateValidationService(str(tmp_path / "state.db"))
    candidate = service.register_release_candidate(
        factor_id="rsi_14",
        template_id="template:verified",
        regime_key="",
        boundary={},
        walk_forward={"passed": True},
        validation_report_path=str(tmp_path / "verified.json"),
        research_evidence=_valid_parity_evidence(),
    )
    assert candidate["status"] == "pending_review"
    assert candidate["research_evidence_gate"]["state"] == "verified"
    assert candidate["legacy_quarantined"] is False

    reviewed = service.review_release_candidate(
        candidate_id=candidate["candidate_id"],
        status="approved",
    )
    assert reviewed["status"] == "approved"

    class FakeTemplateService:
        def get_template(self, *, template_id):
            return {"template_id": template_id}

        def get_active_template(self, *, factor_id, regime_key):
            return {"template_id": "template:old"}

        def create_switch_suggestion(self, **_kwargs):
            return {"suggestion_id": "suggestion_verified"}

        def activate_template(self, **kwargs):
            return {
                "ok": True,
                "blocked": False,
                "switch_id": "switch_verified",
                "new_template_id": kwargs["template_id"],
            }

    class FakeGovernor:
        def __init__(self, _db_path):
            pass

        def set_status(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(service, "_template_service", lambda: FakeTemplateService())
    monkeypatch.setattr(
        "backend.services.parameter_template_validation.RuleEvolutionGovernor",
        FakeGovernor,
    )
    deployed = service.deploy_release_candidate(candidate_id=candidate["candidate_id"])
    assert deployed["ok"] is True
    assert deployed["candidate"]["status"] == "deployed"


def test_model_promotion_gate_rejects_legacy_evidence(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text(
        json.dumps({
            "model_type": "open_quality_lightgbm",
            "research_evidence": _legacy_evidence(),
            "metrics": {},
        }),
        encoding="utf-8",
    )
    gate = ModelInfluenceGovernanceService(tmp_path / "state.db").evaluate_artifact(artifact)
    research_check = next(item for item in gate["checks"] if item["name"] == "research_evidence")
    assert research_check["passed"] is False
    assert "legacy_indicator_sweep" in research_check["actual"]
    assert gate["passed"] is False


def test_parity_replay_is_closed_bar_causal_and_uses_next_bar_bid_ask_costs():
    report = _runner(bars=_bars())
    assert report["status"] == "diagnostic_only"
    assert report["live_parity"] is False
    assert report["governance_eligible"] is False
    assert report["deployable_candidate"] is False
    assert report["causality"]["closed_bar_only"] is True
    assert report["causality"]["next_bar_execution"] is True
    assert report["causality"]["native_bid_ask"] is True
    assert report["_seen_history"] == [
        (1, 0, 1000.0),
        (2, 1, 1900.0),
        (3, 2, 2800.0),
        (4, 3, 3700.0),
    ]
    trade = report["trades"][0]
    assert trade["decision_ts"] == 1000.0
    assert trade["entry_ts"] == 1900.0
    assert trade["entry_price"] == pytest.approx(101.1)
    assert trade["raw_exit_price"] == pytest.approx(102.1)
    assert trade["gross_pnl"] == pytest.approx(1.2)
    assert trade["spread_cost"] == pytest.approx(0.2)
    assert trade["commission_cost"] == pytest.approx(0.06)
    assert trade["net_pnl"] == pytest.approx(0.94)
    assert report["metrics"]["legacy_governance_candidate_count"] == 0
    assert all(len(report["bindings"][name]) == 64 for name in (
        "config_hash", "data_hash", "code_hash", "artifact_hash", "binding_hash"
    ))


def test_parity_replay_uses_the_same_portfolio_wiring_as_live(monkeypatch):
    from backend.services.live_factor_wiring import merge_portfolio_configs
    from backend.services.parity_replay import _portfolio_config

    config = SimpleNamespace(
        factor_signal_config={
            "builtin": {"role": "alpha", "enabled": True},
            "context": {"role": "context", "enabled": True},
        },
        factor_portfolio_weights={"builtin": 0.7, "context": {"weight": 0.2}},
        factor_tactical_alpha=0.65,
        factor_signal_threshold=0.31,
    )

    class Selection:
        selected_factor_ids = ("builtin", "context")

    monkeypatch.setattr(
        "alpha.runtime_factor_selection.select_runtime_factors",
        lambda _config: Selection(),
    )

    expected = merge_portfolio_configs(
        config.factor_signal_config,
        config.factor_portfolio_weights,
        config.factor_tactical_alpha,
        config.factor_signal_threshold,
    )
    assert _portfolio_config(config) == expected


def test_parity_replay_uses_live_supervisor_context_builder_shape():
    config = SimpleNamespace(
        risk_max_holding_bars=10,
        position_supervisor_template_id="position_supervisor:default.v1",
    )
    runner = ParityReplayRunner(
        request=ParityReplayRequest(
            timeframe="M15",
            initial_equity=10_000.0,
            volume_lots=0.01,
            persist_artifact=False,
        ),
        config=config,
        config_snapshot={},
        decision_provider=lambda *_args: {},
        risk_evaluator=lambda _context: {"allowed": False},
        supervisor_evaluator=lambda _context: {"action": "hold"},
    )
    position = {
        "direction": 1,
        "decision_index": 0,
        "entry_index": 1,
        "entry_ts": 1_900.0,
        "entry_price": 101.1,
        "sl": 99.0,
        "tp": 105.0,
        "remaining_fraction": 1.0,
        "mfe_price": 101.1,
        "mae_price": 101.1,
    }
    row = {
        "bid_close": 102.0,
        "ask_close": 102.2,
        "bid_high": 102.4,
        "bid_low": 100.8,
        "ask_high": 102.6,
        "ask_low": 101.0,
    }

    context = runner._supervisor_context(
        position,
        row=row,
        index=2,
        ts=2_800.0,
    )

    assert context["position"]["position_id"] == "replay:1"
    assert context["position"]["opened_at"] == 1_900.0
    assert context["runtime"]["account"]["balance"] == 10_000.0
    assert context["risk"]["max_holding_bars"] == 10
    assert context["temporal_context"]["holding_seconds"] == 900.0
    assert context["replay_read_only"] is True
    assert context["historical_context"] == "reconstructed"
    assert context["path_metrics_implementation"] == (
        "backend.services.position_metrics.update_position_path_metrics"
    )
    assert context["risk"]["mfe"] == pytest.approx(0.9)
    assert context["risk"]["profit_capture_ratio"] == pytest.approx(1.0)


def test_parity_replay_binds_live_lifecycle_primitives_without_claiming_exactness():
    report = _runner(bars=_bars())

    for name in (
        "position_path_metrics",
        "safety_arbitration",
        "trailing",
        "protection_planner",
    ):
        assert report["components"][name]["reuse"] == "exact"
        assert report["components"][name]["verified"] is False
        assert f"component_{name}_not_live_exact" in report["diagnostic_reasons"]

    assert report["components"]["lifecycle"]["reuse"] == "modeled"
    assert report["components"]["cost_model"]["reuse"] == "modeled"
    assert report["components"]["runtime_selector"]["verified"] is False
    assert (
        "historical_runtime_factor_projection_ack_health_and_registry_generation_"
        "are_unavailable"
    ) in report["diagnostic_reasons"]
    assert "historical_five_second_safety_cadence_and_awe_conviction_are_unavailable" in (
        report["diagnostic_reasons"]
    )
    assert "broker_order_receipt_position_reconcile_partial_fill_and_intrabar_tick_path_are_unavailable" in (
        report["diagnostic_reasons"]
    )
    assert "backend/services/live_safety_planner.py" in report["code_binding"]["paths"]
    assert "backend/services/live_position_lifecycle.py" in report["code_binding"]["paths"]
    assert "backend/services/position_metrics.py" in report["code_binding"]["paths"]
    assert report["live_parity"] is False
    assert report["governance_eligible"] is False


def test_parity_task_freezes_bindings_without_caller_preconditions():
    first = _runner(bars=_bars())
    assert first["binding_verification"]["verified"] is True
    assert first["binding_verification"]["missing_expected"] == []

    expected = {
        name: first["bindings"][name]
        for name in ("config_hash", "data_hash", "code_hash", "artifact_hash")
    }
    verified = _runner(bars=_bars(), expected_bindings=expected)
    assert verified["binding_verification"]["verified"] is True
    assert verified["binding_verification"]["missing_expected"] == []
    assert verified["binding_verification"]["mismatches"] == []
    # Hash preconditions are necessary but cannot certify modeled lifecycle.
    assert verified["status"] == "diagnostic_only"
    assert verified["governance_eligible"] is False


def test_modeled_runner_report_cannot_self_promote_by_spoofing_public_flags():
    discovery = _runner(bars=_bars())
    report = _runner(
        bars=_bars(),
        expected_bindings={
            name: discovery["bindings"][name]
            for name in ("config_hash", "data_hash", "code_hash", "artifact_hash")
        },
    )
    report.update({
        "status": "parity_verified",
        "evidence_class": "live_parity",
        "live_parity": True,
        "governance_eligible": True,
        "deployable_candidate": True,
    })

    with pytest.raises(ResearchEvidenceRejected) as exc:
        require_executable_research_evidence(report, executable_use="deploy")

    assert "component_safety_arbitration_not_exact" in exc.value.verdict.blockers
    assert "component_lifecycle_not_exact" in exc.value.verdict.blockers
    assert "parity_diagnostic_blockers_present" in exc.value.verdict.blockers


def test_monthly_pit_source_manifest_accepts_only_versioned_month_file(tmp_path):
    monthly = tmp_path / "bars_2026_07.duckdb"
    monthly.write_bytes(b"snapshot")

    manifest, blockers = parity_replay_module._source_file_manifest([str(monthly)])

    assert blockers == []
    assert manifest == [
        {
            "path": str(monthly.resolve()),
            "file_name": "bars_2026_07.duckdb",
            "exists": True,
            "monthly_name_valid": True,
            "size_bytes": len(b"snapshot"),
            "mtime_ns": monthly.stat().st_mtime_ns,
        }
    ]


def test_selected_discovered_factor_requires_stable_artifact_lineage():
    class Provider:
        selection = SimpleNamespace(selected_factor_ids=["disc_alpha"])

        def __call__(self, *_args):
            return {}

    config = SimpleNamespace(
        risk_max_holding_bars=288,
        position_supervisor_template_id="position_supervisor:default.v1",
        factor_signal_config={
            "disc_alpha": {
                "source": "discovered",
                "enabled": True,
                "weight": 0.5,
                "lifecycle_status": "ACTIVE",
                "factor_id": "dsl:not-a-sha",
                "expression": "close / sma(close, 5)",
                "definition_fingerprint": "",
                "artifact_hash": "",
                "committed_mutation_id": "mutation-1",
            }
        },
    )
    report = ParityReplayRunner(
        request=ParityReplayRequest(
            timeframe="M15",
            as_of=5_000.0,
            persist_artifact=False,
        ),
        config=config,
        config_snapshot={"config_version": 1, "config_hash": _hash({})},
        decision_provider=Provider(),
        risk_evaluator=lambda _context: {"allowed": False},
        supervisor_evaluator=lambda _context: {"action": "hold"},
    ).run(
        _bars(),
        data_source={"source": "monthly_pit_bars", "source_files": ["fixture"]},
    )

    assert "selected_factor_identity_missing:disc_alpha" in report["diagnostic_reasons"]
    assert "selected_factor_artifact_missing:disc_alpha" in report["diagnostic_reasons"]
    assert report["artifact_manifest"]["selected_factor_artifacts"]["disc_alpha"][
        "binding_mode"
    ] == "declared_factor_artifact"
    assert report["live_parity"] is False
    assert report["governance_eligible"] is False


def test_selected_discovered_factor_definition_fingerprint_must_match_canonical_ast():
    from alpha.factor_identity import canonical_factor_id

    expression = "close / ts_mean(close, 5)"

    class Provider:
        selection = SimpleNamespace(
            selected_factor_ids=["disc_alpha"],
            excluded_factor_ids=[],
            reason_excluded={},
        )

        def __call__(self, *_args):
            return {}

    config = SimpleNamespace(
        risk_max_holding_bars=288,
        position_supervisor_template_id="position_supervisor:default.v1",
        factor_signal_config={
            "disc_alpha": {
                "source": "discovered",
                "enabled": True,
                "weight": 0.5,
                "lifecycle_status": "ACTIVE",
                "factor_id": canonical_factor_id(expression),
                "expression": expression,
                "definition_fingerprint": "a" * 64,
                "artifact_hash": "b" * 64,
                "committed_mutation_id": "mutation-1",
            }
        },
    )
    report = ParityReplayRunner(
        request=ParityReplayRequest(
            timeframe="M15",
            as_of=5_000.0,
            persist_artifact=False,
        ),
        config=config,
        config_snapshot={"config_version": 1, "config_hash": _hash({})},
        decision_provider=Provider(),
        risk_evaluator=lambda _context: {"allowed": False},
        supervisor_evaluator=lambda _context: {"action": "hold"},
    ).run(
        _bars(),
        data_source={"source": "monthly_pit_bars", "source_files": ["fixture"]},
    )

    assert (
        "selected_factor_definition_fingerprint_mismatch:disc_alpha"
        in report["diagnostic_reasons"]
    )


def test_parity_replay_uses_live_timeout_arbitration_but_models_broker_close(monkeypatch):
    from risk.policy_service import RiskPolicyService

    class AllowReductions:
        def evaluate(self, _action, _payload):
            return {"allowed": True, "reason": "test"}

    monkeypatch.setattr(RiskPolicyService, "shared", lambda: AllowReductions())
    config = SimpleNamespace(
        risk_max_holding_bars=1,
        position_supervisor_template_id="position_supervisor:default.v1",
    )

    def provider(_history, _bar, index):
        if index == 0:
            return {
                "direction": 1,
                "score": 0.8,
                "atr_price": 1.0,
                "sl_distance": 50.0,
                "tp_distance": 50.0,
            }
        return {}

    report = ParityReplayRunner(
        request=ParityReplayRequest(
            timeframe="M15",
            as_of=5_000.0,
            warmup_bars=0,
            persist_artifact=False,
        ),
        config=config,
        config_snapshot={"config_version": 1, "config_hash": _hash({})},
        decision_provider=provider,
        risk_evaluator=lambda _context: {"allowed": True, "reason": "test"},
        supervisor_evaluator=lambda _context: {"action": "hold"},
    ).run(
        _bars(),
        data_source={"source": "monthly_pit_bars", "source_files": ["fixture"]},
    )

    timeout_plan = next(
        event
        for event in report["events"]
        if event["event"] == "live_safety_plan"
        and any(item["action"] == "timeout" for item in event["candidates"])
    )
    assert timeout_plan["bar_index"] == 2
    assert report["trades"][0]["reason"] == "holding_timeout"
    assert report["trades"][0]["exit_index"] == 3
    assert report["components"]["lifecycle"]["verified"] is False


def test_parity_replay_preserves_supervisor_partial_close_lifecycle(monkeypatch):
    from risk.policy_service import RiskPolicyService

    class AllowReductions:
        def evaluate(self, _action, _payload):
            return {"allowed": True, "reason": "test"}

    monkeypatch.setattr(RiskPolicyService, "shared", lambda: AllowReductions())
    reduced = False

    def supervisor(_context):
        nonlocal reduced
        if reduced:
            return {"action": "hold"}
        reduced = True
        return {
            "action": "reduce",
            "recommended_controls": {
                "reduce_fraction": 0.5,
                "close_reason": "supervisor_reduce",
            },
        }

    def provider(_history, _bar, index):
        if index == 0:
            return {
                "direction": 1,
                "score": 0.5,
                "atr_price": 1.0,
                "sl_distance": 50.0,
                "tp_distance": 50.0,
            }
        return {}

    report = ParityReplayRunner(
        request=ParityReplayRequest(
            timeframe="M15",
            as_of=5_000.0,
            warmup_bars=0,
            persist_artifact=False,
        ),
        config=SimpleNamespace(
            risk_max_holding_bars=288,
            position_supervisor_template_id="position_supervisor:default.v1",
        ),
        config_snapshot={"config_version": 1, "config_hash": _hash({})},
        decision_provider=provider,
        risk_evaluator=lambda _context: {"allowed": True, "reason": "test"},
        supervisor_evaluator=supervisor,
    ).run(
        _bars(),
        data_source={"source": "monthly_pit_bars", "source_files": ["fixture"]},
    )

    reduce_plan = next(
        event
        for event in report["events"]
        if event["event"] == "live_safety_plan"
        and any(item["action"] == "reduce" for item in event["candidates"])
    )
    assert reduce_plan["bar_index"] == 1
    assert report["trades"][0]["closed_fraction"] == pytest.approx(0.5)
    assert report["trades"][0]["reason"] == "supervisor_reduce"
    assert any(
        event["event"] == "reduced" and event["remaining_fraction"] == pytest.approx(0.5)
        for event in report["events"]
    )
    assert report["metrics"]["open_position_at_end"] is True


def test_parity_replay_routes_trailing_through_live_candidate_and_protection_plan(monkeypatch):
    from risk.policy_service import RiskPolicyService

    class AllowReductions:
        def evaluate(self, _action, _payload):
            return {"allowed": True, "reason": "test"}

    monkeypatch.setattr(RiskPolicyService, "shared", lambda: AllowReductions())

    def provider(_history, _bar, index):
        if index == 0:
            return {
                "direction": 1,
                "score": 0.8,
                "atr_price": 0.1,
                "sl_distance": 50.0,
                "tp_distance": 50.0,
            }
        return {}

    report = ParityReplayRunner(
        request=ParityReplayRequest(
            timeframe="M15",
            as_of=5_000.0,
            warmup_bars=0,
            persist_artifact=False,
        ),
        config=SimpleNamespace(
            risk_max_holding_bars=288,
            position_supervisor_template_id="position_supervisor:default.v1",
        ),
        config_snapshot={"config_version": 1, "config_hash": _hash({})},
        decision_provider=provider,
        risk_evaluator=lambda _context: {"allowed": True, "reason": "test"},
        supervisor_evaluator=lambda _context: {"action": "hold"},
    ).run(
        _bars(),
        data_source={"source": "monthly_pit_bars", "source_files": ["fixture"]},
    )

    trailing_plan = next(
        event
        for event in report["events"]
        if event["event"] == "live_safety_plan"
        and any(item["action"] == "trailing" for item in event["candidates"])
    )
    applied = next(
        event
        for event in report["events"]
        if event["event"] == "protection_plan_applied_modeled"
        and event["action"] == "trailing"
    )
    assert trailing_plan["bar_index"] == 2
    assert applied["new_sl"] > applied["old_sl"]
    assert applied["broker_projection_ack"] is False
    assert report["components"]["trailing"]["verified"] is False


def test_quote_age_for_shared_protection_plan_is_replay_deterministic():
    from backend.services.live_position_lifecycle import (
        build_supervisor_tighten_execution_plan,
    )

    position = {
        "direction": 1,
        "entry_price": 100.0,
        "current_price": 103.0,
        "sl": 99.0,
        "tp": 110.0,
        "digits": 2,
    }
    controls = {"target_stop_loss": 102.0, "target_take_profit": 110.0}
    quote = {"bid": 103.0, "ask": 103.2, "mid": 103.1, "ts": 100.0}
    fresh = build_supervisor_tighten_execution_plan(
        position=position,
        controls=controls,
        quote=quote,
        policy={"quote_max_age_seconds": 10.0, "require_side_quote": True},
        evaluated_at_ts=105.0,
    )
    stale = build_supervisor_tighten_execution_plan(
        position=position,
        controls=controls,
        quote=quote,
        policy={"quote_max_age_seconds": 10.0, "require_side_quote": True},
        evaluated_at_ts=111.0,
    )

    assert fresh["sl_plan"]["allowed"] is True
    assert fresh["sl_plan"]["quote_age_seconds"] == 5.0
    assert stale["sl_plan"]["allowed"] is False
    assert stale["sl_plan"]["reason"] == "stale_quote"


def test_parity_replay_uses_live_open_trade_risk_context_builder(monkeypatch):
    from backend.services.parity_replay import _default_risk_evaluator
    from risk.policy_service import RiskPolicyService

    captured: dict[str, object] = {}

    class Policy:
        def evaluate(self, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            return {"allowed": True, "reason": "captured"}

    monkeypatch.setattr(RiskPolicyService, "shared", lambda: Policy())
    config = SimpleNamespace(
        autonomy_mode="demo_autonomous",
        runtime_incident_mode="normal",
        max_position_count=3,
        max_position_api_volume=1_000.0,
        risk_cooldown_bars=3,
    )
    evaluator = _default_risk_evaluator(
        config,
        ParityReplayRequest(
            symbol="XAUUSD+",
            timeframe="M15",
            initial_equity=10_000.0,
            volume_lots=0.01,
            persist_artifact=False,
        ),
    )

    verdict = evaluator({
        "symbol": "XAUUSD+",
        "timeframe": "M15",
        "direction": 1,
        "decision_ts": 1_900.0,
        "decision_bar_index": 7,
        "current_price": 2_400.0,
        "atr_price": 3.5,
        "account": {"balance": 9_990.0, "equity": 9_990.0},
        "session_state": {
            "pnl": -10.0,
            "start_balance": 10_000.0,
            "trades": 1,
            "consecutive_losses": 1,
            "drawdown_pct": 0.1,
            "circuit_breaker": False,
        },
        "session_last_trade_ts": 1_000.0,
        "candidate": {"score": 0.8},
    })

    assert verdict == {"allowed": True, "reason": "captured"}
    assert captured["action"] == "open_trade"
    payload = captured["payload"]
    assert payload["trade"] == {
        "symbol": "XAUUSD+",
        "direction": 1,
        "current_price": 2_400.0,
        "atr_price": 3.5,
    }
    assert payload["account"]["balance"] == 9_990.0
    assert payload["session"]["pnl"] == -10.0
    assert payload["risk_limits"]["schema_version"] == "risk_limit_snapshot.v1"
    assert payload["entry_cluster"]["schema_version"] == "entry_cluster_context.v1"
    assert payload["requested_api_volume"] == 100.0
    assert payload["temporal_context"]["decision_ts"] == 1_900.0
    assert payload["temporal_context"]["seconds_since_last_trade"] == 900.0
    assert payload["temporal_context"]["bars_since_last_trade"] == 1.0
    assert payload["decision_freshness"]["fresh"] is True
    assert payload["replay_read_only"] is True
    assert payload["historical_context"] == "reconstructed"


def test_parity_replay_freezes_closed_bar_returns_for_candidate_var(monkeypatch):
    from backend.services.parity_replay import _default_risk_evaluator
    from risk.policy_service import RiskPolicyService

    captured: dict[str, object] = {}

    class Policy:
        def evaluate(self, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            return {"allowed": True, "reason": "captured"}

    monkeypatch.setattr(RiskPolicyService, "shared", lambda: Policy())
    config = SimpleNamespace(
        autonomy_mode="demo_autonomous",
        runtime_incident_mode="normal",
        max_position_count=3,
        max_position_api_volume=1_000.0,
        risk_cooldown_bars=3,
        var_enabled=True,
        var_alpha=0.95,
        var_window=500,
        multi_symbol_config={"XAUUSD+": {"contract_size": 100}},
    )
    evaluator = _default_risk_evaluator(
        config,
        ParityReplayRequest(
            symbol="XAUUSD+",
            timeframe="M5",
            initial_equity=10_000.0,
            volume_lots=0.01,
            persist_artifact=False,
        ),
    )
    closes = [
        100.0,
        99.0,
        101.0,
        98.0,
        102.0,
        97.0,
        103.0,
        96.0,
        104.0,
        95.0,
        105.0,
        94.0,
    ]

    evaluator(
        {
            "symbol": "XAUUSD+",
            "timeframe": "M5",
            "direction": 1,
            "decision_ts": 1_900.0,
            "decision_bar_index": 11,
            "current_price": 2_000.0,
            "atr_price": 3.5,
            "account": {"balance": 10_000.0, "equity": 10_000.0},
            "session_state": {},
            "candidate": {"score": 0.8},
            "closed_bar_prices": closes,
            "closed_bar_timestamps": list(range(len(closes))),
        }
    )

    assert captured["action"] == "open_trade"
    payload = captured["payload"]
    assert payload["risk_snapshot"]["var"]["status"] == "known"
    assert (
        payload["risk_snapshot"]["var"]["candidate_notional_usd"]
        == 2_000.0
    )
    assert payload["risk_snapshot"]["var"]["sample_count"] == 11
    assert payload["risk_snapshot"]["var_shadow_99"]["alpha"] == 0.99
    assert "_forward_var_input" not in payload["risk_snapshot"]


def test_parity_hash_binding_detects_data_change_and_expected_hash_mismatch():
    first = _runner(bars=_bars())
    changed_bars = _bars()
    changed_bars.loc[2, "bid_high"] = 102.35
    changed = _runner(bars=changed_bars)
    assert first["bindings"]["data_hash"] != changed["bindings"]["data_hash"]

    failed = _runner(
        bars=_bars(),
        expected_bindings={"data_hash": "0" * 64},
    )
    assert failed["status"] == "failed_binding"
    assert failed["binding_verification"]["mismatches"] == ["data_hash"]
    assert failed["trades"] == []


def test_parity_replay_missing_native_bid_ask_fails_closed():
    report = _runner(bars=_bars(include_bid_ask=False))
    assert report["live_parity"] is False
    assert report["causality"]["native_bid_ask"] is False
    assert report["trades"] == []
    assert any(reason.startswith("native_bid_ask_missing") for reason in report["diagnostic_reasons"])


def test_parity_replay_partial_monthly_source_fails_closed():
    report = _runner(
        bars=_bars(),
        data_source={
            "source": "monthly_pit_bars",
            "source_files": ["bars_2026_07.duckdb"],
            "errors": ["bars_2026_06.duckdb:IOException:read failed"],
        },
    )

    assert report["status"] == "diagnostic_only"
    assert report["data_source"]["point_in_time"] is False
    assert "monthly_pit_source_partial_read_error" in report["diagnostic_reasons"]
    assert report["governance_eligible"] is False
    assert report["deployable_candidate"] is False


def test_parity_replay_missing_bound_code_path_fails_closed(monkeypatch):
    monkeypatch.setattr(
        parity_replay_module,
        "_CODE_BINDING_PATHS",
        (*parity_replay_module._CODE_BINDING_PATHS, "missing/live_primitive.py"),
    )

    report = _runner(bars=_bars())

    assert report["status"] == "diagnostic_only"
    assert report["code_binding"]["missing_paths"] == ["missing/live_primitive.py"]
    assert (
        "code_binding_path_missing:missing/live_primitive.py"
        in report["diagnostic_reasons"]
    )
    assert report["governance_eligible"] is False
    assert report["deployable_candidate"] is False


def test_parity_backtest_has_one_guarded_path_into_promotion_architecture():
    root = Path(__file__).resolve().parent.parent
    sources = {}
    for path in (root / "backend" / "services").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "run_backtest(" in text and path.name not in {"backtest_service.py"}:
            sources[path.name] = text
    assert set(sources) == {"parameter_template_validation.py"}
    validation_source = sources["parameter_template_validation.py"]
    assert "research_evidence=backtest_result" in validation_source
    assert validation_source.count("_require_candidate_executable_research_evidence(") >= 3
    assert "if evidence:\n            require_executable_research_evidence(" not in validation_source
    model_source = (root / "backend" / "services" / "model_influence_governance.py").read_text(encoding="utf-8")
    assert "evaluate_research_evidence(" in model_source
