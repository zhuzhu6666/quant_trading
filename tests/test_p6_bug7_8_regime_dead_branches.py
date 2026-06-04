"""
tests/test_p6_bug7_8_regime_dead_branches.py — P6 fix: regime 死分支

引自 framework_audit_20260604.md BUG-7 + BUG-8:
  BUG-7: HIGH_VOL_ATR_PCTILE=100.0 数学上不可达
         (percentile 公式最大 N/(N+1)*100 < 100)
  BUG-8: _dxy_driven 用 levels 做 corrcoef, 趋势序列相关恒 ±1,
         DXY_DRIVEN 几乎永远误报

修复:
  BUG-7: HIGH_VOL_ATR_PCTILE 改 95.0
  BUG-8: _dxy_driven 改用 log returns 而不是 levels

本文件 4 case:
  - BUG-7: percentile 95 实际可达
  - BUG-7: 100 不可达 (验证原 bug)
  - BUG-8: 随机游走的 log returns 几乎不相关
  - BUG-8: 真正的反向 USD-gold 在 log returns 上相关
"""
import numpy as np
import pytest


def test_pctile_100_mathematically_unreachable():
    """P6 BUG-7 复现: (searchsorted+1)/(N+1)*100 最大 N/(N+1)*100 < 100

    验证 buggy 常量 HIGH_VOL_ATR_PCTILE=100 永远不可能触发。
    """
    N = 200  # ATR_PERCENTILE_WINDOW
    valid_atr = np.random.RandomState(42).randn(N).cumsum() + 100
    cur_atr = valid_atr.max()  # 当前 ATR 取历史最大
    # 跟原代码同样的 percentile 公式
    pctile = (np.searchsorted(np.sort(valid_atr), cur_atr) + 1) \
             / (valid_atr.size + 1) * 100.0
    # 最大值永远 < 100
    assert pctile < 100.0
    # 但大于 95 (修复后能触发)
    assert pctile > 95.0


def test_high_vol_atr_pctile_threshold_is_95_in_regime():
    """P6 BUG-7 修复: HIGH_VOL_ATR_PCTILE 应当是 95, 不是 100"""
    from risk.regime import HIGH_VOL_ATR_PCTILE
    assert HIGH_VOL_ATR_PCTILE == 95.0, (
        f"BUG-7 未修: HIGH_VOL_ATR_PCTILE={HIGH_VOL_ATR_PCTILE}, 应为 95.0"
    )


def test_dxy_corr_helper_uses_log_returns():
    """P6 BUG-8 修复: _dxy_corr 用 log returns 替代 levels

    直接测 _dxy_corr: 同样的输入, 修复后用 log returns
    应当能让独立 random walk 返回 False (|corr| < 0.7),
    而 buggy 版本用 levels 会返回 True (|corr| > 0.7)。
    """
    from risk.regime import _dxy_corr
    np.random.seed(42)
    n = 30
    dxy = np.cumsum(np.random.randn(n)) + 100
    xau = np.cumsum(np.random.randn(n)) + 2000

    # 修复后: 独立 random walk 的 log returns 几乎不相关 -> False
    result = _dxy_corr(dxy, xau)
    assert result is False, (
        f"BUG-8 复发: _dxy_corr 对独立 random walk 返回 {result}, 应为 False"
    )


def test_dxy_corr_detects_real_inverse_relationship():
    """P6 BUG-8 修复: 真正的反向 USD-gold, _dxy_corr 应返回 True"""
    from risk.regime import _dxy_corr
    np.random.seed(0)
    n = 30
    base = np.cumsum(np.random.randn(n) * 0.01)  # 共同 noise
    dxy_ret = base + np.random.RandomState(1).randn(n) * 0.002
    xau_ret = -base + np.random.RandomState(2).randn(n) * 0.002  # 反向

    dxy = np.exp(np.cumsum(dxy_ret)) * 100
    xau = np.exp(np.cumsum(xau_ret)) * 2000

    result = _dxy_corr(dxy, xau)
    assert result is True, (
        f"BUG-8 修复未生效: 真反向 USD-gold _dxy_corr 返回 {result}, 应为 True"
    )
