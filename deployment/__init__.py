"""deployment 包 — 金丝雀部署 (Phase 2.3)

导出:
    WeightPolicy    动态因子权重分配 (线性/softmax/阈值策略)
    CanaryDirector  金丝雀阶段晋升/回滚管理器
"""

from .weight_policy import WeightPolicy
from .canary import CanaryDirector

__all__ = [
    "WeightPolicy",
    "CanaryDirector",
]
