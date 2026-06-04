"""
tests/test_p14_verify_previously_fixed.py — 验证 audit 误报, 实际已修

引自 framework_audit_20260604.md:
  BUG-15: data/store.py time 过滤用 string comparison (audit 误报)
  BUG-17: factor_engine.ic_analysis 接受 forward_periods 但只算 1-bar
         (audit 误报, 实际已实现多周期 [1,5,10,20])

本文件 3 case 验证源码已合规, 不再误报。
"""
import inspect

import pytest


def test_bug15_store_load_bars_uses_int_epoch_not_string():
    """BUG-15 verify: data/store.py load_bars time 过滤应使用 int epoch + 参数化"""
    from data import store
    src = inspect.getsource(store.DataStore.load_bars)
    # 修复后: 用 int(pd.Timestamp(start).timestamp()) + 参数化, 不应有 f-string 拼
    assert "int(pd.Timestamp(start).timestamp())" in src or "int(pd.Timestamp" in src, (
        f"BUG-15 实际存在: store.load_bars 没用 int epoch, src: {src[:500]}"
    )
    # 防止 f-string 拼 time (老 bug)
    assert "f\"AND time >=" not in src.replace(" ", ""), (
        f"BUG-15 复发: store 用 f-string 拼 SQL"
    )


def test_bug17_factor_engine_ic_analysis_uses_forward_periods():
    """BUG-17 verify: ic_analysis 应当实现多 forward_periods [1,5,10,20]"""
    from alpha import factor_engine
    src = inspect.getsource(factor_engine.FactorEngine.ic_analysis)
    # 修复后: 应有 forward_periods 循环
    assert "for fp in forward_periods" in src, (
        f"BUG-17 实际存在: ic_analysis 没循环 forward_periods, src: {src[:500]}"
    )
    assert "[1, 5, 10, 20]" in src or "forward_periods = forward_periods or" in src, (
        f"BUG-17 实际存在: ic_analysis 默认不是 [1,5,10,20]"
    )


def test_p3_alerter_already_singular():
    """verify: monitor.alerter.py 是 canonical, monitor.alerts.py 是 legacy

    P13 已确认: 留 alerter, 不删 alerts.py (mab_paper_runner 真在用)
    """
    from monitor import alerter
    assert hasattr(alerter, "Alerter")
    assert hasattr(alerter, "LEVEL_ORDER")
