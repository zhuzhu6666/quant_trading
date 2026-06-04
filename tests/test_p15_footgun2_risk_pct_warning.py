"""
tests/test_p15_footgun2_risk_pct_warning.py — FOOTGUN-2 fix

引自 framework_audit_20260604.md FOOTGUN-2:
PaperTrader.__init__ 默认 risk_per_trade_pct=0.0 静默禁用动态仓位,
caller 可能误以为 "0% 风险"。修复: 构造时 logger.warning 提醒。
"""
import logging
import pytest
from unittest.mock import MagicMock

from execution.paper_trader import PaperTrader


def test_footgun2_warns_when_risk_pct_is_zero(caplog):
    """FOOTGUN-2: risk_per_trade_pct=0.0 应当 WARNING 日志"""
    strategy = MagicMock()
    with caplog.at_level(logging.WARNING, logger="execution.paper_trader"):
        PaperTrader(strategy, initial_balance=500.0, risk_per_trade_pct=0.0)
    assert any("FOOTGUN-2" in r.message and "DISABLED" in r.message
               for r in caplog.records), (
        f"FOOTGUN-2 修复未生效: 没看到 WARNING, caplog: {[r.message for r in caplog.records]}"
    )


def test_footgun2_no_warning_when_risk_pct_positive(caplog):
    """FOOTGUN-2: 传 >0 时不应 warning"""
    strategy = MagicMock()
    with caplog.at_level(logging.WARNING, logger="execution.paper_trader"):
        PaperTrader(strategy, initial_balance=500.0, risk_per_trade_pct=2.0)
    assert not any("FOOTGUN-2" in r.message for r in caplog.records)
