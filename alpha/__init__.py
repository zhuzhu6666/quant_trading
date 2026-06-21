"""
alpha 包 — 因子分析 + IC 追踪

导出:
    ICTracker              滚动 IC 追踪器
    StreamingFactorEngine  流式因子计算引擎（v4 新版，生产路径）
    SignalNormalizer       三域归一化引擎
    PortfolioCompositor    分层组合引擎
    AttributionEngine      因子归因引擎
    ExecutionGate          执行门控
    GPClassifier           GP因子 AST 类型标签分类器
    classify_expr          快捷分类函数
    factor_registry        因子注册表

已弃用 (仍可导入但仅用于批量离线分析脚本):
    FactorEngine → alpha/factor_engine.py (batch-only, 离线分析用;
                    生产路径请用 StreamingFactorEngine)
"""

from .ic_tracker import ICTracker
from .factor_engine import FactorEngine  # deprecated — batch-only offline analysis
from .streaming_factor_engine import StreamingFactorEngine
from .signal_normalizer import SignalNormalizer
from .portfolio_compositor import PortfolioCompositor, CompositeSignal
from .attribution_engine import AttributionEngine, TradeAttribution, FactorAttributionStats
from .execution_gate import ExecutionGate, GateResult
from .gp_classifier import GPClassifier, classify_expr
from .registry import factor_registry

__all__ = [
    "ICTracker",
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
    # FactorEngine intentionally excluded — batch-only, 离线分析脚本直接
    # from alpha.factor_engine import FactorEngine 显式导入
]
