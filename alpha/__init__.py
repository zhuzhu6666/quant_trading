"""
alpha 包 — 因子分析 + IC 追踪

导出:
    ICTracker              滚动 IC 追踪器
    FactorEngine           流式因子计算引擎（batch 模式，旧版）
    StreamingFactorEngine  流式因子计算引擎（streaming 模式，v4 新版）
    GPClassifier           GP因子 AST 类型标签分类器
    classify_expr          快捷分类函数
    factor_registry        因子注册表
"""

from .ic_tracker import ICTracker
from .factor_engine import FactorEngine
from .streaming_factor_engine import StreamingFactorEngine
from .signal_normalizer import SignalNormalizer
from .portfolio_compositor import PortfolioCompositor, CompositeSignal
from .attribution_engine import AttributionEngine, TradeAttribution, FactorAttributionStats
from .execution_gate import ExecutionGate, GateResult
from .gp_classifier import GPClassifier, classify_expr
from .registry import factor_registry

__all__ = [
    "ICTracker",
    "FactorEngine",
    "StreamingFactorEngine",
    "SignalNormalizer",
    "PortfolioCompositor",
    "CompositeSignal",
    "ExecutionGate",
    "GateResult",
    "AttributionEngine",
    "TradeAttribution",
    "FactorAttributionStats",
    "GPClassifier",
    "classify_expr",
    "factor_registry",
]
