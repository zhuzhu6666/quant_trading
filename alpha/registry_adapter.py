"""alpha/registry_adapter.py — 因子注册表适配器 (T14.2, 2026-06-02)

L1 因子生命周期第 2 步. 包装 alpha.registry.factor_registry, 加:
- 运行时 register/unregister (绕过 @decorator)
- register_with_status (记录 source: builtin / discovered / shadow)
- get_active(min_score) 配合 FactorHealth 用
- lifecycle_log 事件流 (jsonl 落盘)

跟现有 alpha/registry.factor_registry 100% 兼容 (用了同一个 _factors dict).
"""
from __future__ import annotations

import json
import logging
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from alpha.registry import factor_registry
from alpha.factor_health import FactorHealth, FactorHealthStatus, ACTIVE_IC_THRESHOLD

logger = logging.getLogger(__name__)


# ── 因子来源枚举 ─────────────────────────────────────
SOURCE_BUILTIN = "builtin"          # 写代码时 @register
SOURCE_DISCOVERED = "discovered"    # DSL 搜索出来, 通过 shadow 升 ACTIVE
SOURCE_SHADOW = "shadow"            # DSL 搜索出来, 还在 shadow 阶段
SOURCE_REMOVED = "removed"          # 已被淘汰, 历史记录保留


@dataclass
class FactorLifecycleEvent:
    """一个 register/unregister 事件"""
    timestamp: float
    event: str                       # "register" | "unregister" | "update"
    factor: str
    source: str                      # builtin / discovered / shadow / removed
    description: str = ""
    score: float = 0.0
    status: str = "UNKNOWN"
    reason: str = ""                  # 触发原因


class RegistryAdapter:
    """
    因子注册表适配器 — 动态 register/unregister + 事件流

    用法:
        adapter = RegistryAdapter()
        adapter.register_runtime(
            name="dsl_factor_001",
            func=lambda df: my_ast.evaluate(df),
            source=SOURCE_DISCOVERED,
            description="ts_corr(close, volume, 20) - rank(close)"
        )
        # 健康评估后:
        adapter.unregister("dsl_factor_001", reason="DECAYING 14 days")
    """

    def __init__(self, log_path: str = "data/charts/factor_lifecycle_log.jsonl"):
        # 用现有 factor_registry._factors 直接操作 (不另开 dict)
        self._log_path = Path(log_path)
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        # 因子的元数据 (source / register_time / description)
        # key: factor_name, value: dict
        self._meta: dict[str, dict] = {}
        # Phase 2.4: 生命周期状态 ("ACTIVE" / "DEAD")
        self._lifecycle_statuses: dict[str, str] = {}
        # 退役前状态 (用于 unretire 恢复)
        self._prev_statuses: dict[str, str] = {}
        # 启动时: 现有 22 builtin 因子自动注册为 BUILTIN source
        for name in factor_registry.list():
            self._meta[name] = {
                "source": SOURCE_BUILTIN,
                "register_time": _time.time(),
                "description": getattr(factor_registry.get(name), "_factor_desc", ""),
            }
            self._lifecycle_statuses[name] = "ACTIVE"

    # ── 运行时 register / unregister ────────────────────────────

    def register_runtime(
        self,
        name: str,
        func: Callable,
        source: str = SOURCE_DISCOVERED,
        description: str = "",
    ) -> bool:
        """
        运行时注册一个因子函数. 返回 True 成功, False 失败 (已存在).

        Args:
            name: 因子名 (unique)
            func: 因子函数, 签名 (df: pd.DataFrame) -> np.ndarray
            source: builtin / discovered / shadow
            description: 表达式或描述
        """
        if name in factor_registry:
            logger.warning(f"[RegistryAdapter] {name} 已存在, 跳过 register")
            return False
        factor_registry._factors[name] = func
        func._factor_name = name
        func._factor_desc = description
        self._meta[name] = {
            "source": source,
            "register_time": _time.time(),
            "description": description,
        }
        self._log_event(FactorLifecycleEvent(
            timestamp=_time.time(), event="register", factor=name,
            source=source, description=description,
        ))
        logger.info(f"[RegistryAdapter] register {name} ({source})")
        return True

    def unregister(self, name: str, reason: str = "") -> bool:
        """
        动态移除一个因子. 不删 metadata (历史保留).

        注意: builtin 因子默认不删除 (保护). 用 force=True 强制删.
        """
        if name not in factor_registry:
            logger.warning(f"[RegistryAdapter] {name} 不在 registry, 跳过")
            return False
        meta = self._meta.get(name, {})
        source = meta.get("source", SOURCE_BUILTIN)
        if source == SOURCE_BUILTIN:
            logger.warning(f"[RegistryAdapter] {name} 是 builtin, 默认不删 (要 force=True)")
            return False

        del factor_registry._factors[name]
        self._meta[name]["source"] = SOURCE_REMOVED
        self._meta[name]["removed_time"] = _time.time()
        self._log_event(FactorLifecycleEvent(
            timestamp=_time.time(), event="unregister", factor=name,
            source=SOURCE_REMOVED, reason=reason,
        ))
        logger.info(f"[RegistryAdapter] unregister {name} (reason: {reason})")
        return True

    def promote(self, name: str, new_source: str = SOURCE_DISCOVERED,
                reason: str = "") -> bool:
        """
        晋升因子来源: shadow -> discovered (or other). 不改函数, 只改 meta + 落 event.
        用于 cron 化的 shadow -> ACTIVE 升级检查.
        """
        if name not in self._meta:
            logger.warning(f"[RegistryAdapter] promote: {name} 不在 meta, 跳过")
            return False
        old_source = self._meta[name].get("source", SOURCE_BUILTIN)
        if old_source == new_source:
            return False
        if old_source == SOURCE_REMOVED:
            logger.warning(f"[RegistryAdapter] promote: {name} 已被移除, 跳过")
            return False
        if old_source == SOURCE_BUILTIN:
            logger.warning(f"[RegistryAdapter] promote: {name} 是 builtin, 不可降级/升级")
            return False
        self._meta[name]["source"] = new_source
        self._meta[name]["promote_time"] = _time.time()
        self._meta[name]["prev_source"] = old_source
        self._log_event(FactorLifecycleEvent(
            timestamp=_time.time(), event="promote", factor=name,
            source=new_source, reason=reason or f"{old_source} -> {new_source}",
        ))
        logger.info(f"[RegistryAdapter] promote {name}: {old_source} -> {new_source}")
        return True

    def force_unregister(self, name: str, reason: str = "") -> bool:
        """强制删除 (包括 builtin)"""
        if name not in factor_registry:
            return False
        del factor_registry._factors[name]
        self._meta[name]["source"] = SOURCE_REMOVED
        self._meta[name]["removed_time"] = _time.time()
        self._log_event(FactorLifecycleEvent(
            timestamp=_time.time(), event="unregister", factor=name,
            source=SOURCE_REMOVED, reason=reason + " (forced)",
        ))
        logger.info(f"[RegistryAdapter] force unregister {name} (reason: {reason})")
        return True

    # ── 退役 / 恢复 (Phase 2.4) ────────────────────────────────────────

    def retire(self, name: str, reason: str = "") -> bool:
        """将因子标记为 DEAD 状态.

        因子从 registry 中移除, 元数据保留, 旧状态保存以便 `unretire`.
        幂等: 对已 DEAD 因子再次调用返回 False.

        Args:
            name: 因子名
            reason: 退役原因 (如 "severe_decay")

        Returns:
            True 成功, False 失败 (不存在 / 已 DEAD)
        """
        if name not in factor_registry and name not in self._meta:
            logger.warning(f"[RegistryAdapter] retire: {name} 不存在")
            return False
        if self._lifecycle_statuses.get(name) == "DEAD":
            logger.warning(f"[RegistryAdapter] retire: {name} 已 DEAD, 跳过")
            return False

        # 保存当前状态以便恢复
        prev_status = self._lifecycle_statuses.get(name, "UNKNOWN")
        self._prev_statuses[name] = prev_status
        self._lifecycle_statuses[name] = "DEAD"

        # 从 registry 中移除函数 (但保留 meta)
        if name in factor_registry:
            old_source = self._meta.get(name, {}).get("source", SOURCE_BUILTIN)
            if old_source != SOURCE_BUILTIN:
                del factor_registry._factors[name]
            self._meta.setdefault(name, {})["source"] = SOURCE_REMOVED

        self._log_event(FactorLifecycleEvent(
            timestamp=_time.time(),
            event="retire",
            factor=name,
            source=self._meta.get(name, {}).get("source", SOURCE_REMOVED),
            reason=reason,
            status="DEAD",
        ))
        logger.info(f"[RegistryAdapter] retire {name} (reason: {reason})")
        return True

    def unretire(self, name: str, reason: str = "") -> bool:
        """将因子从 DEAD 恢复到退役前的状态.

        幂等: 对 ACTIVE 因子再次调用返回 False.

        Args:
            name: 因子名
            reason: 恢复原因 (如 "re-evaluated HEALTHY")

        Returns:
            True 成功, False 失败 (不存在 / 不是 DEAD 状态)
        """
        if name not in self._meta:
            logger.warning(f"[RegistryAdapter] unretire: {name} 不在 meta, 跳过")
            return False
        if self._lifecycle_statuses.get(name) != "DEAD":
            logger.warning(f"[RegistryAdapter] unretire: {name} 不是 DEAD 状态, 跳过")
            return False

        prev_status = self._prev_statuses.pop(name, "ACTIVE")
        self._lifecycle_statuses[name] = prev_status

        # 恢复 source (如果之前是 builtin 则不删除)
        old_source = self._meta.get(name, {}).get("source", SOURCE_REMOVED)
        if old_source == SOURCE_REMOVED:
            self._meta[name]["source"] = SOURCE_DISCOVERED
        # 函数本身不自动恢复 (由调用方决定是否重新注册)

        self._log_event(FactorLifecycleEvent(
            timestamp=_time.time(),
            event="unretire",
            factor=name,
            source=self._meta.get(name, {}).get("source", SOURCE_DISCOVERED),
            reason=reason,
            status=prev_status,
        ))
        logger.info(f"[RegistryAdapter] unretire {name} -> {prev_status} (reason: {reason})")
        return True

    # ── 列表 / 查询 ─────────────────────────────────────────────────────

    def list_active(self, health: FactorHealth, min_score: float = 70.0) -> list[str]:
        """返回 HEALTHY 因子列表 (动态 filter)"""
        all_st = health.evaluate_all()
        return [
            s.factor for s in all_st
            if s.score >= min_score and s.n_obs >= 100
        ]

    def list_by_source(self, source: str) -> list[str]:
        """按 source 过滤 (如所有 discovered)"""
        return [n for n, m in self._meta.items() if m.get("source") == source]

    def get_meta(self, name: str) -> dict:
        return dict(self._meta.get(name, {}))

    # ── 事件日志 ───────────────────────────────────────────────

    def _log_event(self, event: FactorLifecycleEvent):
        """落盘 jsonl 事件流 (append-only) + 联动 EvolutionStory / Metrics"""
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"[RegistryAdapter] 写事件日志失败: {e}")
        # Phase 2.0 接入层:同步广播到 EvolutionStory + RuntimeState(可观测,失败不抛)
        try:
            from monitor.evolution_story import EvolutionStory

            EvolutionStory.shared().append(
                event.event,
                {
                    "factor": event.factor,
                    "source": event.source,
                    "description": event.description,
                    "score": event.score,
                    "status": event.status,
                    "reason": event.reason,
                },
            )
        except Exception:  # noqa: BLE001
            logger.debug("EvolutionStory.append skipped", exc_info=True)
        try:
            from backend.runtime.runtime_state import RuntimeState

            RuntimeState.shared().emit_metric(
                "factor_lifecycle_events_total",
                {"event": event.event, "source": event.source or "unknown"},
            )
        except Exception:  # noqa: BLE001
            logger.debug("RuntimeState.emit_metric skipped", exc_info=True)

    def read_events(self, n: int = 100) -> list[dict]:
        """读最近 n 条事件 (从 jsonl)"""
        if not self._log_path.exists():
            return []
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            events = [json.loads(l) for l in lines[-n:] if l.strip()]
            return events
        except Exception as e:
            logger.warning(f"[RegistryAdapter] 读事件日志失败: {e}")
            return []

    def stats(self) -> dict:
        """统计当前 registry 状态"""
        all_factors = factor_registry.list()
        by_source = {}
        for name in all_factors:
            src = self._meta.get(name, {}).get("source", "unknown")
            by_source[src] = by_source.get(src, 0) + 1
        return {
            "total_active": len(all_factors),
            "by_source": by_source,
            "log_path": str(self._log_path),
        }
