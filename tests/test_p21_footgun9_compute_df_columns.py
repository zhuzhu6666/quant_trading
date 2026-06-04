"""
tests/test_p21_footgun9_compute_df_columns.py — FOOTGUN-9 fix

引自 framework_audit_20260604.md FOOTGUN-9:
alpha/factor_engine.py compute(name) 不验 df 有 'close' 列,
compute_all() 已有 check, compute() 没有, 不一致。
修复: compute() 加 same check, 缺列时 logger.warning + return None。
"""
import logging
import pandas as pd
import numpy as np
import pytest

from alpha.factor_engine import FactorEngine


def test_footgun9_compute_warns_when_close_missing(caplog):
    """FOOTGUN-9: df 缺 'close' 列时 compute() 不抛, return None + warn"""
    fe = FactorEngine(df=pd.DataFrame({"open": [1, 2], "high": [3, 4]}))  # 缺 close
    with caplog.at_level(logging.WARNING, logger="alpha.factor_engine"):
        result = fe.compute("any_factor")
    assert result is None
    assert any("missing 'close'" in r.message for r in caplog.records)


def test_footgun9_compute_returns_none_on_empty_df(caplog):
    """FOOTGUN-9: 空 df 时 compute() return None + warn"""
    fe = FactorEngine(df=pd.DataFrame({"close": []}))
    with caplog.at_level(logging.WARNING, logger="alpha.factor_engine"):
        result = fe.compute("any_factor")
    assert result is None


def test_footgun9_compute_works_on_valid_df():
    """FOOTGUN-9: 合法 df 时 compute() 正常返回"""
    df = pd.DataFrame({
        "open": [2000.0, 2001.0, 2002.0],
        "high": [2003.0, 2004.0, 2005.0],
        "low": [1999.0, 2000.0, 2001.0],
        "close": [2001.0, 2002.0, 2003.0],
        "volume": [100, 110, 120],
    })
    fe = FactorEngine(df=df)
    # 算一个简单因子 (注册名 rsi_14, 不是 factor_rsi_14)
    result = fe.compute("rsi_14")
    assert result is not None
    assert len(result) == 3
