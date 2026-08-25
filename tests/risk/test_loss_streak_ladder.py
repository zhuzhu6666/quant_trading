"""Loss-streak probation ladder (2026-08-24 design, user-approved).

Ladder: daily-limit trip locks the current broker session; the NEXT formal
session opens a bounded probation (probe budget + entry-threshold addon);
the first probation loss or an exhausted budget re-locks until the broker
day ends.  A forced review statement (tighten/no_change) is produced by the
learning loop; a 90-min grace window keeps the lock from depending on
downstream health.
"""

import json
import sqlite3
import time
from datetime import datetime, timezone

import pytest

from backend.core.db_helpers import execute as db_execute
from risk.loss_streak import (
    LadderFacts,
    evaluate_ladder,
)
from risk.governor import RiskGovernor, GovernorState


class TestLadderTransitions:
    def test_not_tripped_is_noop(self):
        v = evaluate_ladder(LadderFacts(now_ts=1_000.0))
        assert v.allowed and v.state == "none" and v.reason == "ok"

    def test_tripped_locks_until_next_session_open(self):
        v = evaluate_ladder(
            LadderFacts(
                now_ts=1_000.0,
                tripped_at=900.0,
                next_session_open_ts=2_000.0,
            )
        )
        assert not v.allowed
        assert v.reason == "awaiting_next_session_open"
        assert v.state == "locked_session"
        assert v.details["resume_at"] == 2_000.0

    def test_probation_allows_with_threshold_addon(self):
        v = evaluate_ladder(
            LadderFacts(
                now_ts=2_500.0,
                tripped_at=900.0,
                next_session_open_ts=2_000.0,
                broker_day_end_ts=5_000.0,
            )
        )
        assert v.allowed
        assert v.state == "probation"
        assert 0.0 < v.threshold_addon <= 0.30
        assert v.details["probe_budget_usd"] > 0

    def test_first_probation_loss_relocks_until_broker_day_end(self):
        v = evaluate_ladder(
            LadderFacts(
                now_ts=3_000.0,
                tripped_at=900.0,
                next_session_open_ts=2_000.0,
                broker_day_end_ts=5_000.0,
                probation_pnl=-15.0,
                probation_trade_count=1,
            )
        )
        assert not v.allowed
        assert v.reason == "probation_first_loss"
        assert v.details["resume_at"] == 5_000.0

    def test_net_negative_probe_cluster_relocks_even_before_budget(self):
        # Two probes: +12 then -20 -> net -8 < 0 -> relocked even though the
        # nominal 25 USD budget is not exhausted; first-negative-loss rule
        # dominates (single source of truth, no dead budget branch).
        v = evaluate_ladder(
            LadderFacts(
                now_ts=3_000.0,
                tripped_at=900.0,
                next_session_open_ts=2_000.0,
                broker_day_end_ts=5_000.0,
                probation_pnl=-8.0,
                probation_trade_count=2,
            )
        )
        assert not v.allowed
        assert v.reason == "probation_first_loss"

    def test_profitable_probe_keeps_probation_alive(self):
        v = evaluate_ladder(
            LadderFacts(
                now_ts=3_500.0,
                tripped_at=900.0,
                next_session_open_ts=2_000.0,
                broker_day_end_ts=5_000.0,
                probation_pnl=12.0,
                probation_trade_count=1,
            )
        )
        assert v.allowed and v.state == "probation"

    def test_broker_day_rollover_clears_the_ladder(self):
        # Day ended without further trips -> fresh start next day.
        v = evaluate_ladder(
            LadderFacts(
                now_ts=6_000.0,
                tripped_at=900.0,
                next_session_open_ts=2_000.0,
                broker_day_end_ts=5_000.0,
                probation_pnl=-15.0,
                probation_trade_count=1,
            )
        )
        assert v.allowed and v.state == "none"

    def test_escalation_raises_threshold_addon(self):
        first = evaluate_ladder(
            LadderFacts(
                now_ts=2_500.0,
                tripped_at=900.0,
                next_session_open_ts=2_000.0,
                broker_day_end_ts=5_000.0,
                consecutive_tripped_days=1,
            )
        )
        second = evaluate_ladder(
            LadderFacts(
                now_ts=2_500.0,
                tripped_at=900.0,
                next_session_open_ts=2_000.0,
                broker_day_end_ts=5_000.0,
                consecutive_tripped_days=2,
            )
        )
        assert second.threshold_addon > first.threshold_addon >= 0.0
        assert second.threshold_addon <= 0.30


class TestGovernorWiring:
    @pytest.fixture(autouse=True)
    def _gov(self):
        RiskGovernor.reset()
        self.gov = RiskGovernor.shared()
        self.gov.update_config(max_daily_loss_pct=10.0)
        yield
        RiskGovernor.reset()

    def _state(self, extra):
        return GovernorState(daily_loss_pct=11.0, extra=extra)

    def test_locked_session_blocks(self):
        v = self.gov.allow_trade(
            self._state(
                {
                    "loss_streak_ladder": {
                        "now_ts": 1_000.0,
                        "tripped_at": 900.0,
                        "next_session_open_ts": 2_000.0,
                    }
                }
            )
        )
        assert not v.allowed
        assert v.reason.startswith("loss_streak_")

    def test_probation_passes_despite_daily_loss_breach(self):
        v = self.gov.allow_trade(
            self._state(
                {
                    "loss_streak_ladder": {
                        "now_ts": 2_500.0,
                        "tripped_at": 900.0,
                        "next_session_open_ts": 2_000.0,
                        "broker_day_end_ts": 5_000.0,
                    }
                }
            )
        )
        assert v.allowed and v.reason == "ok"

    def test_missing_facts_fail_closed_to_legacy_daily_limit(self):
        v = self.gov.allow_trade(self._state({}))
        assert not v.allowed
        assert v.reason == "daily_loss_limit"


class TestReviewStatement:
    def test_dominant_weak_entry_yields_tighten(self):
        from backend.services.loss_streak_review import build_loss_review_statement

        s = build_loss_review_statement(
            trip_date="2026-08-24",
            trade_pnls=[-12.3, -15.1, -19.9, -13.3],
            failure_tags=[],
            weak_entry_loss_count=4,
            total_loss_count=4,
        )
        assert s["action"] == "tighten"
        assert s["dominant_tag_share"] == pytest.approx(1.0)

    def test_mixed_losses_yield_no_change(self):
        from backend.services.loss_streak_review import build_loss_review_statement

        s = build_loss_review_statement(
            trip_date="2026-08-24",
            trade_pnls=[-12.3, -15.1],
            failure_tags=[],
            weak_entry_loss_count=0,
            total_loss_count=2,
        )
        assert s["action"] == "no_change"

    def test_statement_matches_trip_date_only(self):
        from backend.services.loss_streak_review import load_loss_review_statement

        kv = {"loss_streak_review_statement": {"trip_date": "2026-08-24"}}
        hit = load_loss_review_statement(
            trip_date="2026-08-24",
            kv_reader=lambda key: kv.get(key),
        )
        miss = load_loss_review_statement(
            trip_date="2026-08-25",
            kv_reader=lambda key: kv.get(key),
        )
        none = load_loss_review_statement(
            trip_date="2026-08-24", kv_reader=lambda key: None
        )
        assert hit and hit["trip_date"] == "2026-08-24"
        assert miss is None
        assert none is None

    def test_persist_statement_adapts_postgres_sql_and_closes_connection(self):
        from backend.services.loss_streak_review import persist_loss_review_statement

        class FakePostgresConnection:
            def __init__(self):
                self.calls = []
                self.committed = False
                self.closed = False

            def execute(self, sql, params=None):
                self.calls.append((sql, params))
                return self

            def commit(self):
                self.committed = True

            def close(self):
                self.closed = True

        FakePostgresConnection.__module__ = "psycopg"
        conn = FakePostgresConnection()

        written = persist_loss_review_statement(
            {"trip_date": "2026-08-25", "action": "no_change"},
            connection_factory=lambda: conn,
            state_execute=db_execute,
            now=123.0,
        )

        assert written is True
        assert conn.calls[0][0].count("%s") == 3
        assert "?" not in conn.calls[0][0]
        assert conn.calls[0][1][0] == "loss_streak_review_statement"
        assert conn.committed is True
        assert conn.closed is True

    def test_produce_statement_persists_review_and_marks_book(self, tmp_path):
        from backend.services.autonomous_learning import _produce_loss_streak_review_statement

        db_path = tmp_path / "loss_streak.sqlite"
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE runtime_kv (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE experience_memory (
                    outcome_label TEXT,
                    failure_tags_json TEXT,
                    created_at REAL
                );
                """
            )
            conn.execute(
                "INSERT INTO runtime_kv(key, value_json, updated_at) VALUES (?, ?, ?)",
                (
                    "loss_streak_book",
                    json.dumps({"trip_date": today}),
                    1.0,
                ),
            )
            conn.execute(
                "INSERT INTO runtime_kv(key, value_json, updated_at) VALUES (?, ?, ?)",
                (
                    f"live.session_state.{today}",
                    json.dumps({"session_trade_pnls": [-10.0, -12.0]}),
                    1.0,
                ),
            )
            conn.execute(
                "INSERT INTO experience_memory(outcome_label, failure_tags_json, created_at) VALUES (?, ?, ?)",
                ("loss", json.dumps(["weak_entry_loss"]), time.time()),
            )
            conn.commit()
        finally:
            conn.close()

        result = _produce_loss_streak_review_statement(db_path)

        assert result["status"] == "written"
        assert result["action"] == "tighten"
        conn = sqlite3.connect(str(db_path))
        try:
            rows = dict(conn.execute(
                "SELECT key, value_json FROM runtime_kv WHERE key IN (?, ?)",
                ("loss_streak_book", "loss_streak_review_statement"),
            ).fetchall())
        finally:
            conn.close()
        assert json.loads(rows["loss_streak_review_statement"])["action"] == "tighten"
        assert json.loads(rows["loss_streak_book"])["review_for_date"] == today
