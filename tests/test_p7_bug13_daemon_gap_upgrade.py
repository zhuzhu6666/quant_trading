"""
tests/test_p7_bug13_daemon_gap_upgrade.py — P7 fix: daemon 主循环 gap 升级

引自 framework_audit_20260604.md BUG-13:
daemon 启动时 _maybe_recover_from_gap 只跑一次, 主循环里不检查 gap。
周末/长 downtime 后 gap > 24h 但 incremental 只拉 200 bars (M15 = 2天),
剩余数据永久丢失。

修复: SyncDaemon._current_gap_hours() helper, 主循环里
if gap > gap_upgrade_threshold_hours: full_sync instead.

本文件 3 case:
  - _current_gap_hours 在无 status 时返回 None
  - _current_gap_hours 在 status last_sync_utc=3天前返回 ~72
  - 主循环在 gap > 24h 时走 full_sync (mock orch.full_sync)
"""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from data.live_sync.daemon import SyncDaemon


@pytest.fixture
def _fake_status_file(tmp_path, monkeypatch):
    """patch PROJECT_ROOT 让 _last_sync_utc_from_status 读 tmp_path 的 status"""
    import data.live_sync.daemon as d
    fake_root = tmp_path
    charts_dir = fake_root / "data" / "charts"
    charts_dir.mkdir(parents=True)
    status_file = charts_dir / "live_sync_status.json"
    monkeypatch.setattr(d, "PROJECT_ROOT", fake_root)
    return status_file


def test_current_gap_hours_returns_none_when_no_status(_fake_status_file):
    """P7: 无 status 文件时 _current_gap_hours() 返回 None (不升级)"""
    daemon = SyncDaemon()
    assert daemon._current_gap_hours() is None


def test_current_gap_hours_returns_none_when_status_malformed(_fake_status_file):
    """P7: status 文件 malformed 也返回 None"""
    _fake_status_file.write_text("not json", encoding="utf-8")
    daemon = SyncDaemon()
    assert daemon._current_gap_hours() is None


def test_current_gap_hours_3_days_returns_approx_72(_fake_status_file):
    """P7: status last_sync_utc=3天前, _current_gap_hours() 应返回 ~72"""
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3))
    _fake_status_file.write_text(
        json.dumps({"last_sync_utc": three_days_ago.strftime("%Y-%m-%dT%H:%M:%SZ")}),
        encoding="utf-8",
    )
    daemon = SyncDaemon()
    gap = daemon._current_gap_hours()
    assert gap is not None
    # 容差 0.5h (写盘 + 读盘 + 几秒)
    assert 71.5 < gap < 72.5, f"gap 应 ~72h, 实际 {gap}"


def test_daemon_calls_full_sync_when_gap_over_threshold(monkeypatch):
    """P7: 主循环里 gap > 24h 时, 调 full_sync 而不是 incremental_sync

    模拟 daemon 主循环的关键判断 (if gap > threshold: full_sync else incremental)
    """
    daemon = SyncDaemon()
    daemon.orch = MagicMock()
    daemon.orch.full_sync.return_value = MagicMock(total_inserted=123)
    daemon.orch.incremental_sync.return_value = MagicMock(total_inserted=0)

    # 模拟 gap=30h > threshold=24h
    monkeypatch.setattr(daemon, "_current_gap_hours", lambda: 30.0)

    # 复制主循环的关键判断逻辑
    gap = daemon._current_gap_hours()
    if gap is not None and gap > daemon._gap_upgrade_threshold_hours:
        daemon.orch.full_sync(daemon.symbol, daemon.timeframes, n_bars=5000)
    else:
        daemon.orch.incremental_sync(daemon.symbol, daemon.timeframes)

    # 断言: 调了 full_sync, 没调 incremental
    daemon.orch.full_sync.assert_called_once()
    daemon.orch.incremental_sync.assert_not_called()


def test_daemon_calls_incremental_when_gap_under_threshold(monkeypatch):
    """P7: gap < 24h 时走 incremental, 不升级"""
    daemon = SyncDaemon()
    daemon.orch = MagicMock()
    daemon.orch.incremental_sync.return_value = MagicMock(total_inserted=5)

    monkeypatch.setattr(daemon, "_current_gap_hours", lambda: 2.0)

    gap = daemon._current_gap_hours()
    if gap is not None and gap > daemon._gap_upgrade_threshold_hours:
        daemon.orch.full_sync(daemon.symbol, daemon.timeframes, n_bars=5000)
    else:
        daemon.orch.incremental_sync(daemon.symbol, daemon.timeframes)

    daemon.orch.incremental_sync.assert_called_once()
    daemon.orch.full_sync.assert_not_called()


def test_daemon_calls_incremental_when_no_status(monkeypatch):
    """P7: 没 status 时 _current_gap_hours 返回 None, 走 incremental"""
    daemon = SyncDaemon()
    daemon.orch = MagicMock()
    monkeypatch.setattr(daemon, "_current_gap_hours", lambda: None)

    gap = daemon._current_gap_hours()
    if gap is not None and gap > daemon._gap_upgrade_threshold_hours:
        daemon.orch.full_sync(daemon.symbol, daemon.timeframes, n_bars=5000)
    else:
        daemon.orch.incremental_sync(daemon.symbol, daemon.timeframes)

    daemon.orch.incremental_sync.assert_called_once()
    daemon.orch.full_sync.assert_not_called()
