"""deployment 包 — 金丝雀部署 (Phase 2.3)

导出:
    WeightPolicy    动态因子权重分配 (线性/softmax/阈值策略)
    CanaryDirector  金丝雀阶段晋升/回滚管理器
    RiskRebalancer  因子集变更时的仓位重平衡器
"""

from .weight_policy import WeightPolicy
from .canary import CanaryDirector
from .risk_rebalancer import RiskRebalancer

__all__ = [
    "WeightPolicy",
    "CanaryDirector",
    "RiskRebalancer",
]
