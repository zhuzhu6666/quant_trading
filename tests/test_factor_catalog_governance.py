import sqlite3

from backend.services.factor_catalog import build_factor_catalog


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
            ],
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
