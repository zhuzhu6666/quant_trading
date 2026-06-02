"""
factors — 轻量级独立因子库

每个因子都是无状态的纯函数:
    input:  numpy 数组 (highs, lows, closes, [volumes])
    output: float    (最近一根 bar 的因子值, 长度不足返回 nan)

当前暴露的因子:
    - compute_aroon       (factors/aroon.py)
    - compute_cci         (factors/cci.py)
    - compute_mfi         (factors/mfi.py)
    - compute_williams_r  (factors/williams_r.py)
"""

from __future__ import annotations

from .aroon import compute_aroon
from .cci import compute_cci
from .mfi import compute_mfi
from .williams_r import compute_williams_r

__all__ = [
    "compute_aroon",
    "compute_cci",
    "compute_mfi",
    "compute_williams_r",
]
