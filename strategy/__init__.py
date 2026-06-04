"""strategy package — framework code (base, registry, router, ...)

ARCH-5 (audit 2026-06-04): strategy/ vs strategies/ 两个目录混淆。
本 __init__ 显式 re-export strategies/* 让 caller 一行 import 即可。
"""

# Re-export strategies/* 触发 @register 装饰器, 等价于 'import strategies'
from strategies import (  # noqa: F401
    gold_momentum,
    macd_bb,
    multi_factor_m15,
    trend_following,
    mean_reversion,
    breakout,
    ma_cross_h4,
)

# ARCH-5 follow-up: re-export 常用 framework 工具, 让 'from strategy import X' 工作
from strategy.registry import strategy_registry as registry  # noqa: F401
