import threading
from types import SimpleNamespace

from backend.services.live_position_lifecycle import (
    build_pending_supervisor_reentry_block_payload,
    build_supervisor_reentry_block_payload,
    payload_get,
    position_direction_from_payload,
    position_symbol_value,
    supervisor_reentry_block_view,
    supervisor_reentry_key,
)
from backend.services.live_reentry_guard import (
    ReentryGuardRuntime,
    active_supervisor_reentry_block,
    pending_supervisor_reentry_block_from_positions,
    recent_review_reentry_block,
    remember_supervisor_reentry_block,
)


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.closed = False

    def execute(self, _sql, _params):
        return self

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True


def _runtime(*, now=1_000.0, rows=(), connection_factory=None, warnings=None):
    blocks = {}
    warnings = warnings if warnings is not None else []
    factory = connection_factory or (lambda **_kwargs: _Connection(list(rows)))
    return ReentryGuardRuntime(
        blocks=blocks,
        blocks_lock=threading.Lock(),
        reentry_key=supervisor_reentry_key,
        build_block_payload=build_supervisor_reentry_block_payload,
        block_view=supervisor_reentry_block_view,
        direction_from_position=position_direction_from_payload,
        position_symbol=position_symbol_value,
        payload_get=payload_get,
        cooldown_seconds=lambda _cfg: 300.0,
        build_pending_payload=build_pending_supervisor_reentry_block_payload,
        state_connection_factory=factory,
        warning=lambda message, exc: warnings.append((message, exc)),
        now=lambda: now,
    )


def _review(review_id, *, created_at, outcome="bad_loss", tags=None, direction=1):
    return {
        "review_id": review_id,
        "position_id": int(review_id),
        "outcome_label": outcome,
        "failure_tags_json": tags or ["factor_conflict"],
        "review_json": {"direction": direction},
        "created_at": created_at,
    }


def test_remember_and_active_block_expiry():
    runtime = _runtime(now=1_000.0)
    cfg = SimpleNamespace()

    remember_supervisor_reentry_block(
        position={"position_id": 7, "symbol": "XAUUSD+", "direction": 1},
        action="close",
        reason="thesis_broken",
        cfg=cfg,
        runtime=runtime,
        current_price=2_400.0,
        tick=3,
    )

    assert runtime.blocks["XAUUSD:1"]["position_id"] == 7
    assert active_supervisor_reentry_block(
        symbol="XAUUSD", direction=1, runtime=runtime
    )["remaining_seconds"] == 300.0

    expired_runtime = _runtime(now=1_301.0)
    expired_runtime.blocks.update(runtime.blocks)
    assert active_supervisor_reentry_block(
        symbol="XAUUSD", direction=1, runtime=expired_runtime
    ) is None
    assert expired_runtime.blocks == {}


def test_recent_review_requires_two_consecutive_conflict_losses(monkeypatch):
    runtime = _runtime(
        now=1_000.0,
        rows=[_review("2", created_at=950.0), _review("1", created_at=900.0)],
    )
    # The canonical review stream is the only path now; the reader returns
    # legacy-shaped rows directly.
    monkeypatch.setattr(
        "backend.services.live_reentry_guard.iter_review_rows",
        lambda _conn, limit=0: [
            _review("2", created_at=950.0),
            _review("1", created_at=900.0),
        ],
    )

    block = recent_review_reentry_block(
        symbol="XAUUSD", direction=1, runtime=runtime
    )

    assert block["review_ids"] == ["2", "1"]
    assert block["remaining_seconds"] == 3_550.0


def test_recent_review_clean_matching_row_breaks_streak(monkeypatch):
    runtime = _runtime(
        now=1_000.0,
        rows=[
            _review("3", created_at=950.0),
            _review("2", created_at=925.0, outcome="good_win"),
            _review("1", created_at=900.0),
        ],
    )
    monkeypatch.setattr(
        "backend.services.live_reentry_guard.iter_review_rows",
        lambda _conn, limit=0: [
            _review("3", created_at=950.0),
            _review("2", created_at=925.0, outcome="good_win"),
            _review("1", created_at=900.0),
        ],
    )

    assert recent_review_reentry_block(
        symbol="XAUUSD", direction=1, runtime=runtime
    ) is None


def test_recent_review_database_failure_is_advisory():
    warnings = []

    def unavailable(**_kwargs):
        raise RuntimeError("postgres unavailable")

    runtime = _runtime(connection_factory=unavailable, warnings=warnings)

    assert recent_review_reentry_block(
        symbol="XAUUSD", direction=1, runtime=runtime
    ) is None
    assert str(warnings[0][1]) == "postgres unavailable"


def test_pending_close_reduce_or_broken_thesis_blocks_reentry():
    runtime = _runtime()
    cfg = SimpleNamespace(risk_supervisor_reentry_block_reduce=True)
    cases = [
        {"action": "close", "evidence": {}},
        {"action": "reduce", "evidence": {}},
        {"action": "hold", "evidence": {"thesis_status": "broken"}},
    ]

    for index, supervisor in enumerate(cases, start=1):
        block = pending_supervisor_reentry_block_from_positions(
            [
                {
                    "position_id": index,
                    "symbol": "XAUUSD+",
                    "direction": 1,
                    "supervisor": supervisor,
                }
            ],
            symbol="XAUUSD",
            direction=1,
            cfg=cfg,
            runtime=runtime,
        )
        assert block["position_id"] == index
        assert block["source"] == "pending_position_supervisor"
