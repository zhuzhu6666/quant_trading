"""strategy package — framework code (base, registry, router, ...)

ARCH-5 (audit 2026-06-04): strategy/ vs strategies/ 两个目录混淆。
本 __init__ 显式 re-export strategies/* 让 caller 一行 import 即可。

注意: strategies/ 下所有策略文件已清理（Factor Takeover v4 取代旧策略系统）。
保留 re-export 为空不破坏导入链。
"""

# ARCH-5 follow-up: re-export 常用 framework 工具, 让 'from strategy import X' 工作
from strategy.registry import strategy_registry as registry  # noqa: F401
