"""
tests/test_p24_arch5_strategy_re_export.py — ARCH-5 fix

引自 framework_audit_2026-06-04.md ARCH-5:
strategy/ vs strategies/ 两个目录混淆 (framework vs 具体策略)。
修复: strategy/__init__.py re-export strategies/*,
让 caller 写 'from strategy import X' 即可触发 @register 装饰器。
"""
import pytest


def test_arch5_strategy_package_re_exports_strategies():
    """ARCH-5: import strategy 应当触发 strategies/* 的 @register 装饰器"""
    from strategy import registry
    n = len(registry.list())
    assert n > 0, (
        f"ARCH-5 未生效: import strategy 后 registry.list() 仍是空"
    )


def test_arch5_strategy_module_exists_with_re_exports():
    """ARCH-5: strategy/__init__.py 应当 re-export strategies 子模块"""
    import strategy
    # 验证 strategy package 的 namespace 包含 strategies
    assert hasattr(strategy, "gold_momentum"), (
        "ARCH-5 未生效: strategy.gold_momentum 不存在 (re-export 失败)"
    )
    assert hasattr(strategy, "multi_factor_m15")
