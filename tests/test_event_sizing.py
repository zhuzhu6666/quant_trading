"""tests/test_event_sizing.py — EventSizing 单元测试"""
import sqlite3

import pytest
from datetime import datetime, timezone, timedelta

from execution.event_sizing import DEFAULT_TIERS, EVENT_SIZING_SCHEMA_VERSION, EventSizing, EventRecord, EventTier


# ── 辅助: 构造带预加载事件的 EventSizing ──

def _make_sizer(events: list[EventRecord],
                tiers: dict[int, list[EventTier]] | None = None) -> EventSizing:
    """绕过 DB 加载, 直接注入事件列表"""
    es = EventSizing(enabled=False)
    es.enabled = True
    es._events = events
    es.tiers = tiers or DEFAULT_TIERS
    es._min_multiplier = min(
        tier.multiplier
        for tier_list in es.tiers.values()
        for tier in tier_list
    )
    return es


def _event_dt(date_str: str, hours: int = 13, minutes: int = 30) -> datetime:
    """构造 UTC datetime"""
    y, m, d = map(int, date_str.split("-"))
    return datetime(y, m, d, hours, minutes, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════
# 1. 初始化与加载
# ═══════════════════════════════════════════════════════════

class TestEventSizingInit:
    def test_disabled_returns_1_0(self):
        es = EventSizing(enabled=False)
        assert es.get_multiplier(0.0) == 1.0
        assert es.get_multiplier(1e9) == 1.0

    def test_empty_db_returns_1_0(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE events "
            "(date TEXT, type TEXT, description TEXT, importance INTEGER)"
        )
        conn.commit()
        conn.close()
        es = EventSizing(db_path=str(db))
        assert es.get_multiplier(0.0) == 1.0

    def test_nonexistent_db_disables(self, tmp_path):
        es = EventSizing(db_path=str(tmp_path / "nope.db"))
        assert es.enabled is False
        assert es.get_multiplier(0.0) == 1.0

    def test_loads_importance_2_and_3(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE events "
            "(date TEXT, type TEXT, description TEXT, importance INTEGER)"
        )
        conn.execute(
            "INSERT INTO events VALUES ('2024-06-15', 'FOMC', 'FOMC', 3)"
        )
        conn.execute(
            "INSERT INTO events VALUES ('2024-06-20', 'NFP', 'NFP', 3)"
        )
        conn.execute(
            "INSERT INTO events VALUES ('2024-06-25', 'PCE', 'PCE', 2)"
        )
        conn.execute(
            "INSERT INTO events VALUES ('2024-06-30', 'MINOR', 'minor', 1)"
        )
        conn.commit()
        conn.close()
        es = EventSizing(db_path=str(db))
        assert len(es._events) == 3  # importance=1 被排除


# ═══════════════════════════════════════════════════════════
# 2. HIGH 事件 (importance=3) 乘数
# ═══════════════════════════════════════════════════════════

class TestHighImportance:
    """FOMC 19:00 UTC, importance=3"""

    def test_100h_before_returns_1_0(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt - timedelta(hours=100)).timestamp()
        assert es.get_multiplier(bar_time) == 1.0

    def test_10m_before_returns_0_5(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt - timedelta(minutes=10)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(0.5)

    def test_45m_before_returns_0_8(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt - timedelta(minutes=45)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(0.8)

    def test_2h_before_returns_1_0(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt - timedelta(hours=2)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════
# 3. MEDIUM 事件 (importance=2) 乘数
# ═══════════════════════════════════════════════════════════

class TestMediumImportance:
    """PCE 13:30 UTC, importance=2"""

    def test_2h_before_returns_1_0(self):
        evt = _event_dt("2024-06-28", 13, 30)
        es = _make_sizer([EventRecord(dt=evt, event_type="PCE", importance=2)])
        bar_time = (evt - timedelta(hours=2)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(1.0)

    def test_12h_before_returns_1_0(self):
        evt = _event_dt("2024-06-28", 13, 30)
        es = _make_sizer([EventRecord(dt=evt, event_type="PCE", importance=2)])
        bar_time = (evt - timedelta(hours=12)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(1.0)

    def test_30h_before_returns_1_0(self):
        """MEDIUM 只保留短观察窗口, 30h 超出窗口 → 1.0"""
        evt = _event_dt("2024-06-28", 13, 30)
        es = _make_sizer([EventRecord(dt=evt, event_type="PCE", importance=2)])
        bar_time = (evt - timedelta(hours=30)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════
# 4. 多事件: 取最小乘数
# ═══════════════════════════════════════════════════════════

class TestMultipleEvents:
    def test_nearest_event_wins(self):
        fomc = _event_dt("2024-06-15", 19, 0)
        nfp = _event_dt("2024-06-16", 13, 30)
        es = _make_sizer([
            EventRecord(dt=fomc, event_type="FOMC", importance=3),
            EventRecord(dt=nfp, event_type="NFP", importance=3),
        ])
        # FOMC 前 10m → 0.5x (NFP 还在窗口外)
        bar_time = (fomc - timedelta(minutes=10)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(0.5)

    def test_two_close_events_take_min(self):
        fomc = _event_dt("2024-06-15", 19, 0)
        nfp = _event_dt("2024-06-16", 13, 30)
        es = _make_sizer([
            EventRecord(dt=fomc, event_type="FOMC", importance=3),
            EventRecord(dt=nfp, event_type="NFP", importance=3),
        ])
        # FOMC 前 20h、NFP 前约 38.5h 都在短窗口外 → 1.0
        bar_time = (fomc - timedelta(hours=20)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(1.0)

    def test_context_identifies_causal_event_and_window_bucket(self):
        medium = _event_dt("2024-06-15", 13, 30)
        nfp = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([
            EventRecord(dt=medium, event_type="Average Hourly Earnings m/m", importance=2),
            EventRecord(dt=nfp, event_type="NFP", importance=3, description="Non-Farm Employment Change"),
        ])

        ctx = es.get_context((nfp - timedelta(minutes=10)).timestamp())

        assert ctx["multiplier"] == pytest.approx(0.5)
        assert ctx["schema_version"] == EVENT_SIZING_SCHEMA_VERSION
        assert ctx["event_type"] == "NFP"
        assert ctx["event"] == "Non-Farm Employment Change"
        assert ctx["event_importance"] == 3
        assert ctx["window_bucket"] == "pre_0_15m"

    def test_past_event_lookback(self):
        """事件后 3 分钟在 post-event 窗口内 (5 min) → 还有 multiplier"""
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt + timedelta(minutes=3)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(0.5)

    def test_past_event_outside_lookback(self):
        """事件后 10 分钟超出 post-event 窗口 (5 min) → 恢复 1.0"""
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt + timedelta(minutes=10)).timestamp()
        assert es.get_multiplier(bar_time) == 1.0


# ═══════════════════════════════════════════════════════════
# 5. 边界值
# ═══════════════════════════════════════════════════════════

class TestBoundaryValues:
    def test_exactly_15m_high_returns_0_5(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt - timedelta(minutes=15)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(0.5)

    def test_14m_high_returns_0_5(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt - timedelta(minutes=14)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(0.5)

    def test_16m_high_returns_0_8(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt - timedelta(minutes=16)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(0.8)

    def test_invalid_timestamp_returns_1_0(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        assert es.get_multiplier(-1.0) == 1.0
        assert es.get_multiplier(99999999999.0) == 1.0


# ═══════════════════════════════════════════════════════════
# 6. is_event_near
# ═══════════════════════════════════════════════════════════

class TestIsEventNear:
    def test_returns_true_when_near(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt - timedelta(hours=12)).timestamp()
        is_near, desc = es.is_event_near(bar_time, hours_threshold=24.0)
        assert is_near is True
        assert "FOMC" in desc

    def test_returns_false_when_far(self):
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer([EventRecord(dt=evt, event_type="FOMC", importance=3)])
        bar_time = (evt - timedelta(hours=100)).timestamp()
        is_near, desc = es.is_event_near(bar_time, hours_threshold=72.0)
        assert is_near is False
        assert desc is None

    def test_disabled_returns_false(self):
        es = EventSizing(enabled=False)
        is_near, desc = es.is_event_near(1e9)
        assert is_near is False


# ═══════════════════════════════════════════════════════════
# 7. 自定义 tier
# ═══════════════════════════════════════════════════════════

class TestCustomTiers:
    def test_custom_tiers_override(self):
        custom = {3: [EventTier(max_hours_before=2.0, multiplier=0.1)]}
        evt = _event_dt("2024-06-15", 19, 0)
        es = _make_sizer(
            [EventRecord(dt=evt, event_type="FOMC", importance=3)],
            tiers=custom,
        )
        bar_time = (evt - timedelta(hours=1)).timestamp()
        assert es.get_multiplier(bar_time) == pytest.approx(0.1)

    def test_unlisted_importance_returns_1_0(self):
        custom = {3: [EventTier(max_hours_before=24.0, multiplier=0.5)]}
        evt = _event_dt("2024-06-28", 13, 30)
        es = _make_sizer(
            [EventRecord(dt=evt, event_type="PCE", importance=2)],
            tiers=custom,
        )
        bar_time = (evt - timedelta(hours=2)).timestamp()
        assert es.get_multiplier(bar_time) == 1.0


# ═══════════════════════════════════════════════════════════
# 8. PaperEngine 集成
# ═══════════════════════════════════════════════════════════

class TestPaperEngineIntegration:
    def test_engine_accepts_event_sizing(self):
        from execution.paper_engine import PaperExecutionEngine
        es = EventSizing(enabled=False)
        engine = PaperExecutionEngine(event_sizing=es)
        assert engine.event_sizing is es

    def test_engine_works_without_event_sizing(self):
        from execution.paper_engine import PaperExecutionEngine
        engine = PaperExecutionEngine()
        assert engine.event_sizing is None


# ═══════════════════════════════════════════════════════════
# 9. stats
# ═══════════════════════════════════════════════════════════

class TestStats:
    def test_disabled_stats(self):
        es = EventSizing(enabled=False)
        s = es.stats()
        assert s["enabled"] is False
        assert s["total_events"] == 0

    def test_loaded_stats(self, tmp_path):
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE events "
            "(date TEXT, type TEXT, description TEXT, importance INTEGER)"
        )
        conn.execute(
            "INSERT INTO events VALUES ('2024-06-15', 'FOMC', 'FOMC', 3)"
        )
        conn.execute(
            "INSERT INTO events VALUES ('2024-06-28', 'PCE', 'PCE', 2)"
        )
        conn.commit()
        conn.close()
        es = EventSizing(db_path=str(db))
        s = es.stats()
        assert s["enabled"] is True
        assert s["schema_version"] == EVENT_SIZING_SCHEMA_VERSION
        assert s["total_events"] == 2
        assert s["min_multiplier"] == pytest.approx(0.5)
        assert s["max_pre_window_hours"] == pytest.approx(1.0)


class TestLiveServiceAuditContext:
    def test_event_sizing_context_exposes_multiplier_and_event(self):
        from backend.services import live_service

        class _Sizing:
            enabled = True

            def get_multiplier(self, ts):
                return 0.5

            def is_event_near(self, ts):
                return True, "FOMC decision"

            def stats(self):
                return {"events_loaded": 1}

        ctx = live_service._event_sizing_context(_Sizing(), 123.0)

        assert ctx["enabled"] is True
        assert ctx["multiplier"] == 0.5
        assert ctx["event_near"] is True
        assert ctx["event"] == "FOMC decision"
        assert ctx["stats"]["events_loaded"] == 1

    def test_event_sizing_context_is_in_open_trade_risk_context(self, monkeypatch):
        from types import SimpleNamespace
        from backend.services import live_service

        monkeypatch.setattr(live_service, "_live_state_get", lambda key, default=None, clone=False: default)

        ctx = live_service._build_open_trade_risk_context(
            cfg=SimpleNamespace(
                timeframe="M5",
                var_enabled=False,
                var_cvar_threshold=0.02,
                max_position_count=3,
                max_position_api_volume=1000.0,
                pyramid_enabled=True,
                risk_loss_cooldown_after_losses=0,
                risk_loss_cooldown_bars=0,
                risk_block_on_disk_critical=True,
                risk_require_l2_depth=False,
            ),
            bridge=SimpleNamespace(is_connected=True),
            acct={"equity": 1000.0},
            positions=[],
            requested_api_volume=5.0,
            signal_score=0.6,
            event_sizing_context={"enabled": True, "multiplier": 0.2, "event": "NFP"},
        )

        assert ctx["event_sizing"]["enabled"] is True
        assert ctx["event_sizing"]["multiplier"] == 0.2
        assert ctx["event_sizing"]["event"] == "NFP"
