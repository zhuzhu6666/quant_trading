"""
tests/test_p23_feat3_gp_max_runtime.py — FEAT-3 fix

引自 framework_audit_20260604.md FEAT-3:
alpha/factor_search_gp.py run() 没有 wall-clock 时间预算,
一次搜索可跑 1h+。修复: 加 max_runtime_sec 参数, 每代检查,
超过则 break 提前退出。
"""
from unittest.mock import MagicMock
import pytest


def test_feat3_max_runtime_default_600():
    """FEAT-3: 默认 600s (10 min) 时间预算"""
    from alpha.factor_search_gp import FactorSearchGP
    import inspect
    sig = inspect.signature(FactorSearchGP.run)
    assert "max_runtime_sec" in sig.parameters
    assert sig.parameters["max_runtime_sec"].default == 600.0


def test_feat3_rejects_zero_or_negative():
    """FEAT-3: max_runtime_sec <= 0 应当 raise"""
    from alpha.factor_search_gp import FactorSearchGP
    gp = FactorSearchGP(evaluator=MagicMock())
    with pytest.raises(ValueError, match="max_runtime_sec must be > 0"):
        gp.run(pop_size=10, init_max_depth=2, n_generations=1, max_runtime_sec=0)
