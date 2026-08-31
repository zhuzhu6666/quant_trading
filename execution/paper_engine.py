"""re-export shim — 原 paper_engine 已合并至 paper_execution，仅保留兼容导入"""
from execution.paper_execution import PaperExecutionEngine, PaperTrade, PaperEngine, DynamicSlippageModel, GOLD_TICK_USD, FORCE_CLOSE_BASED_SLTP
__all__ = ["PaperExecutionEngine", "PaperTrade", "PaperEngine", "DynamicSlippageModel", "GOLD_TICK_USD", "FORCE_CLOSE_BASED_SLTP"]
