"""re-export shim — 原 paper_trader 已合并至 paper_execution，仅保留兼容导入"""
from execution.paper_execution import PaperTrader, PaperReport, PaperExecutionEngine, PaperTrade, PaperEngine, DynamicSlippageModel
__all__ = ["PaperTrader", "PaperReport", "PaperExecutionEngine", "PaperTrade", "PaperEngine", "DynamicSlippageModel"]
