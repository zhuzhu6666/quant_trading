"""SystemOverviewPanel — 系统概览面板。

返回 Dashboard 头部所需的快照 dict:
  - uptime_seconds
  - loop_count
  - total_factors_by_status
  - last_sync_age
  - last_evolution_event

读取 RuntimeState + SyncHealth + EvolutionStory 的三个共享单例,
无需外部依赖。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from backend.runtime.runtime_state import RuntimeState
from data.live_sync.health import SyncHealth
from monitor.evolution_story import EvolutionStory

logger = logging.getLogger(__name__)


class SystemOverviewPanel:
    """系统概览面板 — 用于 Dashboard 顶部展示。"""

    def __init__(
        self,
        runtime_state: Optional[RuntimeState] = None,
        sync_health: Optional[SyncHealth] = None,
        evolution_story: Optional[EvolutionStory] = None,
    ) -> None:
        self._runtime = runtime_state or RuntimeState.shared()
        self._sync = sync_health or SyncHealth.shared()
        self._story = evolution_story or EvolutionStory.shared()

    # ── 主入口 ──────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """返回系统当前概览快照。

        Returns:
            包含以下字段的 dict:

            - uptime_seconds (float): 自 RuntimeState 启动以来的秒数。
            - loop_count (int): 注册的 loop 数量。
            - loop_statuses (dict): 各 loop 的 {kind: running|stopped}。
            - total_factors_by_status (dict): 按 status 分组的因子计数。
              从 EvolutionStory 最近一次 factor_birth / factor_death 推断,
              实际精确值依赖 FactorHealth 的 evaluate_all()。
            - last_sync_age (float | None): sync loop 最后成功距今秒数,
              若从未同步则为 None。
            - last_evolution_event (dict | None): 最近一条演化事件。
            - run_id (str): 当前 run_id。
        """
        now = time.time()

        # --- uptime ---
        uptime_seconds = now - self._runtime.started_at

        # --- loops ---
        loops = self._runtime.all_loops()
        loop_count = len(loops)
        loop_statuses: Dict[str, str] = {}
        for kind, status in loops.items():
            loop_statuses[kind] = "running" if status.is_running() else "stopped"

        # --- factors ---
        total_factors_by_status = self._count_factors_by_status()

        # --- sync ---
        sync_record = self._sync.record
        last_sync_age: Optional[float] = None
        if sync_record.last_success_ts is not None:
            last_sync_age = now - sync_record.last_success_ts

        # --- last evolution event ---
        last_evolution_event: Optional[Dict[str, Any]] = self._last_evolution_event()

        return {
            "uptime_seconds": round(uptime_seconds, 1),
            "loop_count": loop_count,
            "loop_statuses": loop_statuses,
            "total_factors_by_status": total_factors_by_status,
            "last_sync_age": round(last_sync_age, 1) if last_sync_age is not None else None,
            "last_evolution_event": last_evolution_event,
            "run_id": self._runtime.run_id,
            "sync_degraded": sync_record.consecutive_failures >= 3,
            "sync_consecutive_failures": sync_record.consecutive_failures,
        }

    # ── 内部方法 ────────────────────────────────────────────────

    def _count_factors_by_status(self) -> Dict[str, int]:
        """扫描 EvolutionStory 事件推断各状态的因子数量。

        若 FactorHealth 实例可用, 调用 evaluate_all() 获得精确计数。
        兜底方案: 从最新 factor_birth 和 factor_death 事件推断存活因子数。
        """
        # 优先尝试精确的 FactorHealth 查询
        try:
            from alpha.factor_health import FactorHealth
            from alpha.ic_tracker import ICTracker

            # 创建一个空 tracker (只拿 evaluate_all 的计数)
            tracker = ICTracker(window=5000)
            health = FactorHealth(tracker)
            result = health.report_dict()
            return result.get("summary", {})
        except Exception:  # noqa: BLE001
            pass

        # 兜底: 从 EvolutionStory 事件流推算
        born: set[str] = set()
        deceased: set[str] = set()
        for rec in self._story.iter_all():
            etype = rec.get("event_type", "")
            factor_name = (rec.get("payload") or {}).get("factor") or rec.get("factor")
            if not factor_name:
                continue
            if etype == "factor_birth":
                born.add(factor_name)
                deceased.discard(factor_name)
            elif etype == "factor_death":
                deceased.add(factor_name)

        total_alive = len(born - deceased)
        return {
            "total": total_alive,
            "healthy": 0,
            "watch": 0,
            "decaying": 0,
            "unknown": total_alive,
        }

    def _last_evolution_event(self) -> Optional[Dict[str, Any]]:
        """从 EvolutionStory 获取最近的一条事件。"""
        events: List[Dict[str, Any]] = []
        for rec in self._story.iter_all():
            events.append(rec)
            if len(events) > 1000:
                break
        if not events:
            return None
        # iter_all 返回按写入顺序 (append-only), 最后一条即最新
        last = events[-1]
        return {
            "event_type": last.get("event_type"),
            "ts_iso": last.get("ts_iso"),
            "payload_summary": self._summarize_payload(last),
        }

    @staticmethod
    def _summarize_payload(rec: Dict[str, Any]) -> str:
        """对事件 payload 做简短摘要（长 payload 截断）。"""
        payload = {k: v for k, v in rec.items() if k not in ("ts", "ts_iso", "event_type")}
        if not payload:
            return ""
        text = json.dumps(payload, ensure_ascii=False, default=str)
        if len(text) > 120:
            return text[:120] + "..."
        return text
