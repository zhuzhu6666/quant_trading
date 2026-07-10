"""alpha/persistent_registry.py — 跨进程持久化 shadow/discovered 因子 (v2).

问题: RegistryAdapter.register_runtime 修改的 factor_registry._factors
只在当前进程有效, 进程退出就丢.

v2 (audit 2026-06-22): 从主状态库 lifecycle_events 表恢复,
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
                     adapter: "RegistryAdapter | None" = None,
                     preferred_names: set[str] | None = None,
                     discovered_budget: int | None = None) -> int:
    """从主状态库 lifecycle_events 表恢复所有 shadow / discovered 因子.

    降级到 JSONL 文件 (如果传了路径且文件存在).

    Returns: 恢复的因子数
    """
    latest_event: dict[str, dict] = {}

    def _apply_lifecycle_event(ev: dict) -> None:
        factor = ev.get("factor")
        if not factor:
            return
        event_type = str(ev.get("event") or "")
        state = latest_event.setdefault(
            factor,
            {
                "factor": factor,
                "event": "",
                "source": "",
                "description": "",
                "active": False,
                "score": 0.0,
            },
        )
        if event_type == "register":
            state["active"] = True
            state["source"] = ev.get("source") or state.get("source") or "shadow"
            state["description"] = ev.get("description") or state.get("description") or ""
        elif event_type == "promote":
            if state.get("active", False):
                state["source"] = ev.get("source") or "discovered"
        elif event_type in {"retire", "unregister"}:
            state["active"] = False
            state["source"] = ev.get("source") or "removed"
        elif event_type == "unretire":
            state["active"] = True
            state["source"] = ev.get("source") or state.get("source") or "discovered"
        else:
            return
        state["event"] = event_type
        state["timestamp"] = ev.get("timestamp", state.get("timestamp", 0.0))
        state["score"] = ev.get("score", state.get("score", 0.0))

    # 主路径: 从 PostgreSQL state store lifecycle_events 表读取
    try:
        from backend.core.db import get_state_pg_conn

        conn = get_state_pg_conn(read_only=True)
        rows = conn.execute(
            "SELECT factor, event, source, description, timestamp, score "
            "FROM lifecycle_events "
            "WHERE event IN ('register', 'promote', 'retire', 'unregister', 'unretire') "
            "ORDER BY timestamp ASC"
        ).fetchall()
        conn.close()
        for r in rows:
            _apply_lifecycle_event(dict(r))
        if verbose and latest_event:
            logger.info(f"[PersistentRegistry] 从 PostgreSQL state_v1 读取 {len(latest_event)} 个因子事件")
    except Exception as e:
        logger.debug(f"[PersistentRegistry] 主状态库读取失败: {e}")

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
                    if ev.get("event") in ("register", "promote", "retire", "unregister", "unretire"):
                        _apply_lifecycle_event(ev)
            if verbose and latest_event:
                logger.info(f"[PersistentRegistry] 从 JSONL 降级读取 {len(latest_event)} 个因子事件")

    if not latest_event:
        if verbose:
            logger.info("[PersistentRegistry] 无生命周期事件, 无需恢复")
        return 0

    if adapter is None:
        adapter = RegistryAdapter.shared()
    preferred_names = {str(name) for name in (preferred_names or set())}
    if discovered_budget is None and preferred_names:
        try:
            import os

            discovered_budget = max(1, min(int(os.getenv("QUANT_RUNTIME_DISCOVERED_FACTOR_BUDGET", "24")), 128))
        except Exception:
            discovered_budget = 24

    candidates = [
        (name, ev)
        for name, ev in latest_event.items()
        if ev.get("active", False)
        and ev.get("source", "") in ("shadow", "discovered")
        and ev.get("description", "")
    ]
    candidates.sort(
        key=lambda item: (
            0 if item[0] in preferred_names else 1,
            0 if item[1].get("source") == "discovered" else 1,
            -float(item[1].get("score") or 0.0),
            -float(item[1].get("timestamp") or 0.0),
            item[0],
        )
    )
    if discovered_budget is not None:
        preferred = [item for item in candidates if item[0] in preferred_names]
        cold = [
            item for item in candidates
            if item[0] not in preferred_names and item[1].get("source") == "discovered"
        ][:max(0, int(discovered_budget))]
        candidates = preferred + cold

    restored = 0
    skipped_invalid = 0
    skipped_artifact = 0
    for name, ev in candidates:
        event_type = ev.get("event")
        source = ev.get("source", "")
        description = ev.get("description", "")
        if not ev.get("active", False):
            continue
        if source not in ("shadow", "discovered"):
            # builtin 已经被 @register 加载, 跳过
            continue
        if not description:
            # 没有表达式描述, 不能恢复
            continue
        desc_l = str(description).strip().lower()
        if desc_l.startswith("pca component") or desc_l.startswith("model artifact"):
            skipped_artifact += 1
            if verbose:
                logger.info(
                    "[PersistentRegistry] 跳过不可恢复 artifact: %s (%s)",
                    name,
                    description[:80],
                )
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
        if skipped_artifact:
            logger.info(f"[PersistentRegistry] 跳过 {skipped_artifact} 个 artifact 因子")
        if skipped_invalid:
            logger.info(f"[PersistentRegistry] 跳过 {skipped_invalid} 个无效描述因子")
        logger.info(f"[PersistentRegistry] 总共恢复 {restored} 因子 (从 {len(latest_event)} 事件)")
    return restored

