"""execution/event_filter.py — 共享事件过滤器 (T13, 2026-06-02)

解决 MAB 4 策略共享时 breakout/trend/mean_rev 没有事件 skip 逻辑的问题.
原 baseline +407% 之所以能跑, 是因为 multi_factor_m15 自带事件 skip. 但 MAB 路径里
其它 3 个 strategy 不读 params 的 enable_nfp_skip 等字段, 在 NFP/FOMC/CPI 事件日正常开仓
遇 OOH 跳 ($100-176 USD) 爆仓.

T13: 把事件 skip 抽成共享层, 在 MABPaperRunner 主循环里先调 filter.should_skip(bar),
     True 就跳过该 bar 所有 strategy. 跟 strategy 自带 skip 兼容 (后者已 skip 不会再开).

事件类型 (跟 multi_factor_m15 同款, 来源 data/news_cache):
- NFP: 发布日 ±1 天 skip (1 天 = skip 当天+前后各 1 天 = 3 天窗口)
- FOMC+CPI 双事件: 开仓日 ±3 天内同时有 FOMC 和 CPI 时 skip
- GVZ-gate: 黄金波动率日变化 < 阈值时 skip (平静日, 黄金一般趋势不显著)

v1 实现 (T13):
- 共享在 MABPaperRunner 启动时 load 一次 (跟 multi_factor_m15 同款)
- 主循环每根 bar 调 should_skip(bar_time) → bool
- 跳过时 strategy.on_bar 仍调 (让 strategy 内部状态前进), 但 signal 被 filter 设 None
"""
from __future__ import annotations

import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SharedEventFilter:
    """
    共享事件过滤器 — MAB 4 策略共用

    用法:
        ef = SharedEventFilter(
            enable_nfp_skip=True, nfp_skip_days=1,
            enable_dual_event_skip=True,
            enable_gvz_gate=True, gvz_drop_pct=-2.0,
            db_path="data/market_data.db",
        )
        # paper 主循环里每根 bar:
        if ef.should_skip(bar_time):
            signal = None  # 跳过该 bar 所有 strategy
    """

    def __init__(
        self,
        enable_nfp_skip: bool = True,
        nfp_skip_days: int = 1,
        enable_dual_event_skip: bool = True,
        enable_gvz_gate: bool = True,
        gvz_drop_pct: float = -2.0,
        db_path: str = "data/market_data.db",
    ):
        self.enable_nfp_skip = enable_nfp_skip
        self.nfp_skip_days = nfp_skip_days
        self.enable_dual_event_skip = enable_dual_event_skip
        self.enable_gvz_gate = enable_gvz_gate
        self.gvz_drop_pct = gvz_drop_pct
        self.db_path = db_path

        self._nfp_window: set[str] = set()
        self._fomc_dates: set[str] = set()
        self._cpi_dates: set[str] = set()
        self._gvz_series: dict[str, float] = {}

        self._load()
        self._skipped_count = 0  # 统计: 跳过了多少 bar

    def _load(self):
        """从 SQLite 加载事件日历和 GVZ (缺失/异常时静默)"""
        try:
            from data.news_cache import (
                load_nfp_dates, load_event_dates, load_gvz_series,
                expand_to_window, daily_change_pct,
            )
            if not Path(self.db_path).exists():
                logger.warning(f"[EventFilter] {self.db_path} 不存在, 跳过事件 skip")
                return

            if self.enable_nfp_skip:
                nfp = load_nfp_dates(self.db_path)
                self._nfp_window = expand_to_window(nfp, self.nfp_skip_days)
                logger.info(f"[EventFilter] NFP window: {len(self._nfp_window)} 天")

            if self.enable_dual_event_skip:
                self._fomc_dates = load_event_dates(self.db_path, 'FOMC')
                self._cpi_dates = load_event_dates(self.db_path, 'CPI')
                logger.info(f"[EventFilter] FOMC: {len(self._fomc_dates)} 个, CPI: {len(self._cpi_dates)} 个")

            if self.enable_gvz_gate:
                self._gvz_series = load_gvz_series(self.db_path)
                logger.info(f"[EventFilter] GVZ series: {len(self._gvz_series)} 天")
        except Exception as e:
            logger.warning(f"[EventFilter] load 失败: {e}")

    def should_skip(self, bar_time: float) -> tuple[bool, str]:
        """
        判定 bar 是否该跳过 (不开新仓).

        Returns:
            (skip, reason) — skip=True 跳过, reason 说明原因
        """
        try:
            bar_date_str = datetime.utcfromtimestamp(bar_time).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            return False, ""

        # 1. NFP 窗口
        if self.enable_nfp_skip and bar_date_str in self._nfp_window:
            self._skipped_count += 1
            return True, "NFP_WINDOW"

        # 2. FOMC + CPI 双事件
        if self.enable_dual_event_skip and self._fomc_dates and self._cpi_dates:
            try:
                bd = datetime.strptime(bar_date_str, "%Y-%m-%d").date()
                has_fomc = any(
                    abs((bd - datetime.strptime(d, "%Y-%m-%d").date()).days) <= 3
                    for d in self._fomc_dates
                )
                has_cpi = any(
                    abs((bd - datetime.strptime(d, "%Y-%m-%d").date()).days) <= 3
                    for d in self._cpi_dates
                )
                if has_fomc and has_cpi:
                    self._skipped_count += 1
                    return True, "DUAL_EVENT"
            except (ValueError, TypeError):
                pass

        # 3. GVZ-gate (黄金波动率日变化 < 阈值, 平静日 skip)
        if self.enable_gvz_gate and self._gvz_series:
            try:
                from data.news_cache import daily_change_pct
                gvz_chg = daily_change_pct(self._gvz_series, bar_date_str)
                if gvz_chg is not None and gvz_chg < self.gvz_drop_pct:
                    self._skipped_count += 1
                    return True, f"GVZ_DROP({gvz_chg:.1f}%)"
            except Exception:
                pass

        return False, ""

    def stats(self) -> dict:
        return {
            "nfp_window_days": len(self._nfp_window),
            "fomc_events": len(self._fomc_dates),
            "cpi_events": len(self._cpi_dates),
            "gvz_series_days": len(self._gvz_series),
            "total_skipped_bars": self._skipped_count,
        }
