"""test_sync_health — SyncHealth 的 fresh/stale/degraded 判定。"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from data.live_sync.health import SyncHealth


@pytest.fixture
def health(tmp_path: Path) -> SyncHealth:
    SyncHealth.reset_singleton()
    return SyncHealth(state_path=str(tmp_path / "sync_health.json"))


def test_initial_state_is_neither_fresh_nor_degraded(health: SyncHealth) -> None:
    # 刚初始化:无任何成功记录
    assert health.is_fresh() is False
    assert health.is_stale() is True  # stale 是基于有 last_success_ts 但超时
    assert health.is_degraded() is False


def test_record_success_makes_fresh(health: SyncHealth) -> None:
    health.record_success(last_bar_ts_by_tf={"M15": time.time()})
    assert health.is_fresh() is True
    assert health.is_degraded() is False


def test_record_failure_increments_consecutive(health: SyncHealth) -> None:
    health.record_failure("err1")
    health.record_failure("err2")
    assert health.record.consecutive_failures == 2
    assert health.is_degraded() is False  # 阈值默认 3
    health.record_failure("err3")
    assert health.is_degraded() is True


def test_consecutive_failures_reset_on_success(health: SyncHealth) -> None:
    health.record_failure("e1")
    health.record_failure("e2")
    health.record_success()
    assert health.record.consecutive_failures == 0


def test_last_bar_age_seconds(health: SyncHealth) -> None:
    old_ts = time.time() - 700  # 11 分钟前
    health.record_success(last_bar_ts_by_tf={"M15": old_ts})
    age = health.last_bar_age_seconds("M15")
    assert age is not None
    assert 695 <= age <= 710


def test_snapshot_includes_derived_flags(health: SyncHealth) -> None:
    health.record_success(last_bar_ts_by_tf={"M15": time.time()})
    snap = health.snapshot()
    assert snap["fresh"] is True
    assert snap["stale"] is False
    assert snap["degraded"] is False


def test_persistence_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "h.json"
    SyncHealth.reset_singleton()
    h1 = SyncHealth(state_path=str(p))
    h1.record_success(last_bar_ts_by_tf={"M15": time.time()})
    h1.record_failure("err1")
    # 重建
    SyncHealth.reset_singleton()
    h2 = SyncHealth(state_path=str(p))
    assert h2.record.consecutive_failures == 1
    assert h2.record.total_successes == 1
    assert "M15" in h2.record.last_bar_ts_by_tf
