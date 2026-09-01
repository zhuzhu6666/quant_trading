"""re-export shim — 原 slippage 已合并至 paper_execution，仅保留兼容导入"""
from execution.paper_execution import DynamicSlippageModel, GOLD_TICK_USD
__all__ = ["DynamicSlippageModel", "GOLD_TICK_USD"]
