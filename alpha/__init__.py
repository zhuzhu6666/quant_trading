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

"""
from .ic_tracker import ICTracker
from .streaming_factor_engine import StreamingFactorEngine
from .signal_normalizer import SignalNormalizer
from .portfolio_compositor import PortfolioCompositor, CompositeSignal
from .attribution_engine import AttributionEngine, TradeAttribution, FactorAttributionStats
from .execution_gate import ExecutionGate, GateResult
from .gp_classifier import GPClassifier, classify_expr
from .registry import factor_registry
from .decision_policy import DecisionPolicy, WeightDecision
from .shadow_trader import ShadowPerf, evaluate_shadow_factors, load_shadow_perf
from .adaptive_weight_engine import AdaptiveWeightEngine

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
    "DecisionPolicy",
    "WeightDecision",
    "ShadowPerf",
    "evaluate_shadow_factors",
    "load_shadow_perf",
    "AdaptiveWeightEngine",
]
