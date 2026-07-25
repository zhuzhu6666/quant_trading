import sqlite3

from backend.services import live_service


def test_risk_inputs_use_clean_reviews_and_position_notional(monkeypatch, tmp_path):
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE trade_outcome_review (
            position_id TEXT,
            pnl REAL,
            review_json TEXT,
            created_at REAL
        );
        INSERT INTO trade_outcome_review
            (position_id, pnl, review_json, created_at)
        VALUES ('position-1', 12.5, '{}', 100.0);
        """
    )
    conn.close()

    def connect():
        connection = sqlite3.connect(db_path)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(live_service, "_get_state_pg_conn", connect)
    clean_pnls, positions = live_service._risk_metric_inputs(
        [
            {
                "position_id": 7,
                "symbol": "XAUUSD+",
                "direction": 1,
                "current_price": 2_400.0,
                "volume": 100.0,
            }
        ],
    )

    assert clean_pnls == [12.5]
    assert positions == [
        {
            "position_id": 7,
            "symbol": "XAUUSD+",
            "direction": 1,
            "notional_usd": 2_400.0,
        }
    ]


def test_stale_broker_facts_replace_previous_known_snapshot(monkeypatch):
    writes = []
    previous = {
        "schema_version": "risk_metrics_snapshot.v2",
        "status": "known",
        "components": {"var": {"status": "known", "var_pct": 1.0}},
    }
    monkeypatch.setattr(
        live_service,
        "_runtime_kv_get",
        lambda *_args, **_kwargs: previous,
    )
    monkeypatch.setattr(
        live_service,
        "_runtime_kv_set",
        lambda _key, value: writes.append(value),
    )
    live_service._live_state_update(
        account_reconciled={"equity": 10_000.0},
        positions_reconciled=[],
        account_reconcile_id="account-old",
        positions_reconcile_id="positions-old",
        account_updated_at=1.0,
        positions_updated_at=1.0,
        account_reconcile_failed_at=0.0,
        positions_reconcile_failed_at=0.0,
    )

    live_service._update_live_loop_risk_metrics(
        tick=1,
        log=lambda _message: None,
    )

    assert writes[-1]["status"] == "stale"
    assert live_service._get_risk_state()["snapshot"]["status"] == "stale"
