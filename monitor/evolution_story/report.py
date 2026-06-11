"""EvolutionReport — 日报 / 周报生成器。

从 EvolutionStory 的 JSONL 事件流中读取事件,
按日期或日期区间聚合为可读 / 可落盘的摘要 dict。

用法:
    report = EvolutionReport()
    daily = report.generate_daily(date="2026-06-10")
    weekly = report.generate_weekly(start_date="2026-06-08")
    report.save_report(daily, path="data/charts/evolution_daily_report.json")
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from monitor.evolution_story import EvolutionStory

logger = logging.getLogger(__name__)

# ── 事件类型分类 ──────────────────────────────────────────────────
_FACTOR_BIRTH_EVENTS = {"factor_birth"}
_FACTOR_PROMOTION_EVENTS = {"canary_promote"}
_FACTOR_ROLLBACK_EVENTS = {"canary_rollback"}
_FACTOR_RETIREMENT_EVENTS = {"retire_pending", "factor_death"}
_SYNC_EVENTS = {"sync_recovered"}
_PNL_EVENTS = {"pnl_attribution"}


class EvolutionReport:
    """从 EvolutionStory 事件流生成日报 / 周报。"""

    def __init__(self, story: Optional[EvolutionStory] = None) -> None:
        self._story = story or EvolutionStory.shared()

    # ── 生成日报 ────────────────────────────────────────────────

    def generate_daily(self, date: str) -> Dict[str, Any]:
        """生成单日报表。

        Args:
            date: 日期字符串, 格式 ``"YYYY-MM-DD"``。

        Returns:
            dict 包含当日各事件分类计数 + 事件明细。
        """
        events = self._query_events_for_date(date)
        return self._aggregate(events, date=date)

    # ── 生成周报 ────────────────────────────────────────────────

    def generate_weekly(self, start_date: str) -> Dict[str, Any]:
        """生成周报（7 天聚合）。

        Args:
            start_date: 起始日期 ``"YYYY-MM-DD"``, 包含该日。
                        结束为 ``start_date + 7 天``（不包含）。

        Returns:
            dict 包含周内所有事件分类聚合计数。
        """
        from datetime import timedelta

        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = start_dt + timedelta(days=7)
        end_str = end_dt.strftime("%Y-%m-%d")

        events: List[Dict[str, Any]] = []
        for rec in self._story.iter_all():
            rec_date = self._extract_date(rec)
            if rec_date is None:
                continue
            if start_date <= rec_date < end_str:
                events.append(rec)

        agg = self._aggregate(events, date=start_date)
        agg["report_type"] = "weekly"
        agg["date_range"] = {"start": start_date, "end": end_str}
        return agg

    # ── 落盘 ────────────────────────────────────────────────────

    def save_report(
        self,
        report: Dict[str, Any],
        path: str = "data/charts/evolution_daily_report.json",
    ) -> str:
        """将 report dict 写入 JSON 文件。

        Args:
            report: generate_daily / generate_weekly 返回的 dict。
            path:   目标文件路径。

        Returns:
            实际写入的绝对路径字符串。
        """
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("EvolutionReport saved to %s", p.resolve())
        return str(p.resolve())

    # ── 内部方法 ────────────────────────────────────────────────

    def _query_events_for_date(self, date: str) -> List[Dict[str, Any]]:
        """从 EvolutionStory 流中过滤出指定日期的事件。"""
        out: List[Dict[str, Any]] = []
        for rec in self._story.iter_all():
            rec_date = self._extract_date(rec)
            if rec_date == date:
                out.append(rec)
        return out

    @staticmethod
    def _extract_date(rec: Dict[str, Any]) -> Optional[str]:
        """从事件记录的 ts_iso 或 ts 字段提取 ``YYYY-MM-DD`` 日期。"""
        ts_iso = rec.get("ts_iso")
        if ts_iso:
            try:
                return ts_iso[:10]
            except Exception:  # noqa: BLE001
                pass
        ts = rec.get("ts")
        if ts is not None:
            try:
                return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d")
            except Exception:  # noqa: BLE001
                pass
        return None

    @staticmethod
    def _aggregate(events: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
        """将事件列表聚合成摘要 dict。

        Args:
            events: 原始事件记录列表。
            **extra: 额外字段（date / date_range 等）。

        Returns:
            {
                "new_factors": int,
                "promotions": int,
                "rollbacks": int,
                "retirements": int,
                "sync_events": int,
                "pnl_attributions": int,
                "events": [ ... ],
                **extra,
            }
        """
        new_factors = 0
        promotions = 0
        rollbacks = 0
        retirements = 0
        sync_events = 0
        pnl_attributions = 0

        for rec in events:
            etype = rec.get("event_type", "")
            if etype in _FACTOR_BIRTH_EVENTS:
                new_factors += 1
            elif etype in _FACTOR_PROMOTION_EVENTS:
                promotions += 1
            elif etype in _FACTOR_ROLLBACK_EVENTS:
                rollbacks += 1
            elif etype in _FACTOR_RETIREMENT_EVENTS:
                retirements += 1
            elif etype in _SYNC_EVENTS:
                sync_events += 1
            elif etype in _PNL_EVENTS:
                pnl_attributions += 1

        result: Dict[str, Any] = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "report_type": "daily",
            "new_factors": new_factors,
            "promotions": promotions,
            "rollbacks": rollbacks,
            "retirements": retirements,
            "sync_events": sync_events,
            "pnl_attributions": pnl_attributions,
            "total_events": len(events),
            "events": events,
        }
        result.update(extra)
        return result
