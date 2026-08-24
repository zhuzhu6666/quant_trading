"""Loss-streak probation ladder (2026-08-24 design, user-approved).

Ladder: daily-limit trip locks the current broker session; the NEXT formal
session opens a bounded probation (probe budget + entry-threshold addon);
the first probation loss or an exhausted budget re-locks until the broker
day ends.  A forced review statement (tighten/no_change) is produced by the
learning loop; a 90-min grace window keeps the lock from depending on
downstream health.
"""

import pytest

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
