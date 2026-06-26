"""alpha/persistent_registry.py — 跨进程持久化 shadow/discovered 因子 (v2).

问题: RegistryAdapter.register_runtime 修改的 factor_registry._factors
只在当前进程有效, 进程退出就丢.

v2 (audit 2026-06-22): 从 state.db lifecycle_events 表恢复,
不再依赖 JSONL 文件. JSONL 作为降级备份.

用法:
    import alpha.persistent_registry
    alpha.persistent_registry.restore_from_log()
"""
from __future__ import annotations

import logging
import json
from typing import Optional

from alpha.registry_adapter import RegistryAdapter
from alpha.factor_dsl import FactorParser, evaluate_dsl

logger = logging.getLogger(__name__)


def restore_from_log(lifecycle_log_path: str = "",
                     verbose: bool = True,
                     adapter: "RegistryAdapter | None" = None) -> int:
    """从 state.db lifecycle_events 表恢复所有 shadow / discovered 因子.

    降级到 JSONL 文件 (如果传了路径且文件存在).

    Returns: 恢复的因子数
    """
    latest_event: dict[str, dict] = {}

    # 主路径: 从 state.db lifecycle_events 表读取
    try:
        import sqlite3
        from backend.core.db import STATE_DB
        conn = sqlite3.connect(str(STATE_DB))
        conn.row_factory = sqlite3.Row
        # 按 factor 分组取最后一个 register/unregister 事件
        rows = conn.execute(
            "SELECT factor, event, source, description, timestamp "
            "FROM lifecycle_events "
            "WHERE event IN ('register', 'unregister') "
            "ORDER BY timestamp ASC"
        ).fetchall()
        conn.close()
        for r in rows:
            factor = r["factor"]
            if factor:
                latest_event[factor] = dict(r)
        if verbose and latest_event:
            logger.info(f"[PersistentRegistry] 从 state.db 读取 {len(latest_event)} 个因子事件")
    except Exception as e:
        logger.debug(f"[PersistentRegistry] state.db 读取失败: {e}")

    # 降级: JSONL 文件 (如果传了路径且 DB 没读到)
    if not latest_event and lifecycle_log_path:
        from pathlib import Path
        log_path = Path(lifecycle_log_path)
        if log_path.exists():
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
                    if ev.get("event") in ("register", "unregister"):
                        latest_event[factor] = ev
            if verbose and latest_event:
                logger.info(f"[PersistentRegistry] 从 JSONL 降级读取 {len(latest_event)} 个因子事件")

    if not latest_event:
        if verbose:
            logger.info("[PersistentRegistry] 无生命周期事件, 无需恢复")
        return 0

    if adapter is None:
        adapter = RegistryAdapter.shared()
    restored = 0
    skipped_invalid = 0
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
        try:
            FactorParser(description).parse()
        except Exception as e:
            skipped_invalid += 1
            if verbose:
                logger.warning(
                    "[PersistentRegistry] 跳过无效因子恢复: %s (%s): %s",
                    name,
                    source,
                    e,
                )
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
        if skipped_invalid:
            logger.info(f"[PersistentRegistry] 跳过 {skipped_invalid} 个无效描述因子")
        logger.info(f"[PersistentRegistry] 总共恢复 {restored} 因子 (从 {len(latest_event)} 事件)")
    return restored
