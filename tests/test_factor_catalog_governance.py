import sqlite3
from types import SimpleNamespace

import backend.services.factor_catalog as factor_catalog
from backend.services.factor_catalog import build_factor_catalog
from config import runtime_config
from config.runtime_config import RuntimeConfig


def test_shadow_perf_catalog_read_uses_one_batch_query(monkeypatch):
    class FakeConnection:
        def __init__(self):
            self.queries = []

        def execute(self, sql, params=()):
            self.queries.append((sql, tuple(params)))
            return self

        def fetchall(self):
            return [
                {
                    "factor": "shadow_a",
                    "source": "shadow",
                    "symbol": "XAUUSD+",
                    "timeframe": "M5",
                    "oos_bars": 12,
                    "cumulative_pnl": 0.25,
                    "hit_rate": 0.5,
                    "max_drawdown": 0.1,
                    "last_signal": 0.2,
                    "metrics_json": '{"n_valid": 11, "n_active": 10}',
                }
            ]

        def close(self):
            return None

    conn = FakeConnection()
    monkeypatch.setattr(
        factor_catalog,
        "_connect_state",
        lambda _db_path, *, read_only=False: conn,
    )

    result = factor_catalog._shadow_perf_by_factor(
        ["shadow_a", "shadow_b", "shadow_a"],
        factor_catalog.STATE_DB,
    )

    assert list(result) == ["shadow_a"]
    assert result["shadow_a"]["n_valid"] == 11
    assert len(conn.queries) == 1
    assert conn.queries[0][1] == ("shadow_a", "shadow_b")


def test_factor_catalog_uses_runtime_selection_snapshot_roles_and_weights(
    tmp_path,
    monkeypatch,
):
    runtime_config.reset_for_tests()
    runtime_config.replace(
        RuntimeConfig(
            factor_signal_config={
                "alpha_a": {
                    "enabled": True,
                    "lifecycle_status": "ACTIVE",
                    "role": "context",
                }
            },
            factor_portfolio_weights={"alpha_a": 0.0},
        )
    )
    monkeypatch.setattr(
        factor_catalog,
        "select_runtime_factors",
        lambda _config: SimpleNamespace(
            selected_factor_ids=["alpha_a"],
            excluded_factor_ids=[],
            reason_excluded={},
        ),
    )
    monkeypatch.setattr(
        "backend.services.runtime_factor_selection_projection."
        "RuntimeFactorSelectionProjectionService.latest",
        lambda _self: {
            "ok": True,
            "selected_factor_ids": ["alpha_a"],
            "selected_factor_roles": {"alpha_a": "alpha"},
            "selected_factor_weights": {"alpha_a": 0.5},
            "reason_excluded": {},
        },
    )
    try:
        item = {
            row["factor_id"]: row
            for row in build_factor_catalog(tmp_path / "catalog.sqlite")
        }["alpha_a"]
    finally:
        runtime_config.reset_for_tests()

    assert item["role"] == "alpha"
    assert item["weight"] == 0.5
    assert item["explicit_weight"] is True
    assert item["eligible_for_live"] is True
    assert item["used_in_score"] is True
    assert item["runtime_selection_source"] == "live_runtime_projection"


def test_factor_catalog_includes_factor_governance_shadow_audit(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE factor_governance_shadow_audit (
                inference_id TEXT PRIMARY KEY,
                model_type TEXT NOT NULL,
                model_version TEXT DEFAULT '',
                artifact_path TEXT DEFAULT '',
                review_id TEXT DEFAULT '',
                trade_id TEXT DEFAULT '',
                position_id TEXT DEFAULT '',
                factor TEXT DEFAULT '',
                mode TEXT DEFAULT 'shadow',
                positive_score REAL DEFAULT 0.0,
                weakness_score REAL DEFAULT 0.0,
                prediction INTEGER DEFAULT 0,
                payload_json TEXT DEFAULT '{}',
                result_json TEXT DEFAULT '{}',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE canary_state (
                factor_name TEXT,
                stage TEXT,
                oos_bars INTEGER,
                cumulative_pnl REAL,
                evidence_hash TEXT,
                dataset_hash TEXT,
                evidence_end_at TEXT,
                stage_evidence_hash TEXT,
                fresh_evidence_bars INTEGER,
                updated_at REAL
            )
            """
        )
        conn.executemany(
            """
            INSERT INTO factor_governance_shadow_audit
            (inference_id, model_type, model_version, factor, mode,
             positive_score, weakness_score, prediction, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("fg1", "factor_governance_lightgbm", "1.0", "rsi_14", "shadow", 0.2, 0.7, 0, 100.0),
                ("fg2", "factor_governance_lightgbm", "1.0", "rsi_14", "shadow", 0.1, 0.9, 0, 200.0),
                ("fg3", "factor_governance_lightgbm", "1.0", "audit_only_ghost", "shadow", 0.1, 0.9, 0, 300.0),
            ],
        )
        conn.execute(
            """
            INSERT INTO canary_state
            VALUES ('canary_only_ghost', 'SHADOW', 10, 0, '', '', '', '', 0, 300)
            """
        )
        conn.commit()
    finally:
        conn.close()

    catalog = {item["factor_id"]: item for item in build_factor_catalog(db_path)}

    shadow = catalog["rsi_14"]["factor_governance_shadow"]
    assert shadow["sample_count"] == 2
    assert shadow["weak_sample_count"] == 2
    assert shadow["latest_inference_id"] == "fg2"
    assert catalog["rsi_14"]["model_weakness_score"] == 0.9
    assert "audit_only_ghost" not in catalog
    assert "canary_only_ghost" not in catalog


def test_factor_catalog_prefers_canonical_lifecycle_state(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE factor_lifecycle_state (
                factor_id TEXT PRIMARY KEY,
                factor_name TEXT,
                origin TEXT,
                lifecycle_stage TEXT,
                runtime_admission TEXT,
                    mutation_id TEXT,
                    generation INTEGER,
                    definition_fingerprint TEXT,
                    artifact_hash TEXT,
                    metadata_json TEXT,
                    activated_at REAL,
                    retired_at REAL,
                    updated_at REAL
            )
            """
        )
        conn.execute(
            """
                INSERT INTO factor_lifecycle_state VALUES
                ('builtin:rsi_14', 'rsi_14', 'builtin', 'QUARANTINED',
                 'blocked', 'gmut_lifecycle', 2, 'rsi_14', 'artifact',
                 '{"expression":"rsi_14"}', 0, 100, 100)
            """
        )
        conn.commit()
    finally:
        conn.close()

    runtime_config.reset_for_tests()
    runtime_config.replace(
        RuntimeConfig(
            factor_signal_config={
                "rsi_14": {
                    "enabled": True,
                    "role": "alpha",
                    "lifecycle_status": "ACTIVE",
                }
            },
            factor_portfolio_weights={"rsi_14": 1.0},
        )
    )
    try:
        item = {
            row["factor_id"]: row for row in build_factor_catalog(db_path)
        }["rsi_14"]
    finally:
        runtime_config.reset_for_tests()

    assert item["lifecycle_factor_id"] == "builtin:rsi_14"
    assert item["lifecycle_status"] == "QUARANTINED"
    assert item["runtime_admission"] == "blocked"
    assert item["enabled"] is False
    assert item["eligible_for_live"] is False
    assert item["used_in_score"] is False


def test_factor_catalog_prefers_committed_mutation_over_older_suggestion(tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE policy_suggestion (
                scope_key TEXT,
                action TEXT,
                confidence REAL,
                status TEXT,
                created_at REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE governance_mutation_intent (
                scope_key TEXT,
                action TEXT,
                mutation_id TEXT,
                status TEXT,
                projection_status TEXT,
                control_surface TEXT,
                committed_at REAL,
                updated_at REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO policy_suggestion
            VALUES ('rsi_14', 'downweight', 0.9, 'approved', 100)
            """
        )
        conn.execute(
            """
            INSERT INTO governance_mutation_intent
            VALUES ('rsi_14', 'update_weight', 'gmut_applied', 'committed',
                    'current', 'factor_weight', 200, 200)
            """
        )
        conn.commit()
    finally:
        conn.close()

    item = {
        row["factor_id"]: row for row in build_factor_catalog(db_path)
    }["rsi_14"]

    assert item["governance_action"] == "update_weight"
    assert item["governance_status"] == "applied"
    assert item["governance_mutation_id"] == "gmut_applied"
    assert item["last_action_ts"] == 200.0


def test_factor_catalog_never_marks_prepared_builtin_live_eligible(tmp_path):
    runtime_config.reset_for_tests()
    runtime_config.replace(
        RuntimeConfig(
            factor_signal_config={
                "rsi_14": {
                    "enabled": True,
                    "role": "alpha",
                    "source": "builtin",
                    "lifecycle_status": "PROMOTION_PREPARED",
                }
            },
            factor_portfolio_weights={"rsi_14": 0.0},
        )
    )
    try:
        catalog = {
            item["factor_id"]: item
            for item in build_factor_catalog(tmp_path / "catalog.sqlite")
        }
    finally:
        runtime_config.reset_for_tests()

    assert catalog["rsi_14"]["enabled"] is True
    assert catalog["rsi_14"]["lifecycle_status"] == "PROMOTION_PREPARED"
    assert catalog["rsi_14"]["eligible_for_live"] is False
    assert catalog["rsi_14"]["used_in_score"] is False
