"""alpha/persistent_registry.py — 跨进程持久化 shadow 因子 (T15.5 v2, 2026-06-02)

问题: RegistryAdapter.register_runtime 修改的 factor_registry._factors
只在当前进程有效, 进程退出就丢.

解决: 启动时从 lifecycle.jsonl 读 register 事件, 重新 evaluate_dsl 出函数,
重新 register_runtime.

用法:
    import alpha.persistent_registry
    # 在 main.py / scripts 入口 import 后立即调
    alpha.persistent_registry.restore_from_log("data/charts/factor_lifecycle_log.jsonl")

注: 这只是 v1 临时方案, v2 应该把 DSL 表达式 + 编译结果存 json, 不每次 parse.
"""
from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Optional

from alpha.registry_adapter import RegistryAdapter
from alpha.factor_dsl import evaluate_dsl

logger = logging.getLogger(__name__)


def restore_from_log(lifecycle_log_path: str = "data/charts/factor_lifecycle_log.jsonl",
                      verbose: bool = True,
                      adapter: "RegistryAdapter | None" = None) -> int:
    """
    从 lifecycle log 恢复所有 shadow / discovered 因子.

    Returns: 恢复的因子数
    """
    log_path = Path(lifecycle_log_path)
    if not log_path.exists():
        if verbose:
            logger.info(f"[PersistentRegistry] {log_path} 不存在, 无需恢复")
        return 0

    # 读所有事件, 按 factor 名取最后一个事件 (register 或 unregister)
    latest_event: dict[str, dict] = {}
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            factor = ev.get("factor")
            if not factor:
                continue
            # 最后一个事件决定状态
            if ev.get("event") in ("register", "unregister"):
                latest_event[factor] = ev

    if not latest_event:
        return 0

    if adapter is None:
        adapter = RegistryAdapter()
    restored = 0
    for name, ev in latest_event.items():
        event_type = ev.get("event")
        source = ev.get("source", "")
        description = ev.get("description", "")
        # unregister 事件: 跳过 (已经被 unregister)
        if event_type == "unregister":
            continue
        if source not in ("shadow", "discovered"):
            # builtin 已经被 @register 加载, 跳过
            continue
        if not description:
            # 没有表达式描述, 不能恢复
            continue
        # 重建函数
        def make_func(expr: str):
            return lambda df: evaluate_dsl(expr, df)
        ok = adapter.register_runtime(
            name=name,
            func=make_func(description),
            source=source,
            description=description,
            log_event=False,
        )
        if ok:
            restored += 1
            if verbose:
                logger.info(f"[PersistentRegistry] 恢复: {name} ({source})")
    if verbose:
        logger.info(f"[PersistentRegistry] 总共恢复 {restored} 因子 (从 {len(latest_event)} 事件)")
    return restored
