"""
tests/test_p20_bug22_lstsq_rcond.py — BUG-22 fix

引自 framework_audit_20260604.md BUG-22:
alpha/factor_attribution.py marginal_ic 用 np.linalg.lstsq(rcond=None)
解共线 factor 回归, 系数和 IC 都失真。
修复: rcond=None → rcond=1e-10, 截掉接近零的奇异值。
"""
import inspect

import pytest


def test_bug22_lstsq_uses_rcond_threshold():
    """BUG-22: marginal_ic lstsq 应当用 rcond=1e-10, 不再用 None"""
    from alpha import factor_attribution
    src = inspect.getsource(factor_attribution.FactorAttribution.marginal_ic)
    # 修复后: 至少一处用 rcond=1e-10, 不再全是 rcond=None
    assert "rcond=None" not in src, (
        f"BUG-22 复发: marginal_ic 仍用 rcond=None, src: {src[:500]}"
    )
    assert "rcond=1e-10" in src, (
        f"BUG-22 修复未生效: 没看到 rcond=1e-10, src: {src[:500]}"
    )


def test_bug22_marginal_ic_handles_collinear_factors():
    """BUG-22: 共线 factor 不再炸 beta"""
    from alpha.factor_attribution import FactorAttribution
    import numpy as np
    import pandas as pd

    # 构造共线数据: f1 == f2
    n = 100
    rng = np.random.default_rng(42)
    f1 = rng.normal(0, 1, n)
    f2 = f1.copy()  # 完全共线
    f3 = rng.normal(0, 1, n)
    y = f1 + f3 * 0.5 + rng.normal(0, 0.1, n)

    fa = FactorAttribution(
        factor_names=["f1", "f2", "f3"],
        factor_returns=np.column_stack([f1, f2, f3]),
        forward_returns=y,
    )
    # 修复后: 不抛异常, 返回的 marginal_ic 在合理范围
    df = fa.marginal_ic()
    assert len(df) == 3
    # f1 和 f2 高度共线, marginal 不应无穷大
    marg_f1 = df.loc[df.factor == "f1", "marginal_ic"].iloc[0]
    marg_f2 = df.loc[df.factor == "f2", "marginal_ic"].iloc[0]
    assert abs(marg_f1) < 1.0, f"marginal_ic 共线炸了: {marg_f1}"
    assert abs(marg_f2) < 1.0, f"marginal_ic 共线炸了: {marg_f2}"
