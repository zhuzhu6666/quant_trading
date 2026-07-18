from __future__ import annotations

from backend.services.broker_execution_intent import BrokerExecutionIntentStore


def test_unresolved_count_binds_broker_account_and_symbol_once() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class _Connection:
        def execute(self, sql, params):
            calls.append((sql, tuple(params)))
            return self

        def fetchone(self):
            return {"n": 2}

        def close(self):
            return None

    store = BrokerExecutionIntentStore(
        connection_factory=lambda **_kwargs: _Connection()
    )

    assert store.unresolved_count(
        broker="ctrader",
        account_id="account-1",
        symbol="XAUUSD+",
    ) == 2

    sql, params = calls[0]
    assert sql.count("%s") == len(params) == 3
    assert sql.count("account_id=%s") == 1
    assert sql.count("symbol=%s") == 1
    assert params == ("ctrader", "account-1", "XAUUSD+")
