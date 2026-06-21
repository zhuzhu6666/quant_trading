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
from datetime import datetime, timezone, date, timedelta
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
            db_path="data/events.duckdb",
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
        db_path: str = "data/events.duckdb",
    ):
        self.enable_nfp_skip = enable_nfp_skip
        self.nfp_skip_days = nfp_skip_days
        self.enable_dual_event_skip = enable_dual_event_skip
        self.enable_gvz_gate = enable_gvz_gate
        self.gvz_drop_pct = gvz_drop_pct
        self.db_path = db_path

        self._nfp_window: set[str] = set()
        self._fomc_dates: set[str] = set()  # 兼容旧接口 (str)
        self._cpi_dates: set[str] = set()   # 兼容旧接口 (str)
        # OPT-5 (audit 2026-06-06): 启动时一次性 strptime → date 对象
        # 旧实现 (line 118/120/124) 每 bar 调 strptime 50K bar × 100 events = 5M 次
        # 现在 should_skip 只用 date.fromisoformat (C 实现, ~5× 更快) + 预解析集合
        self._fomc_date_objs: set = set()  # set[date]
        self._cpi_date_objs: set = set()   # set[date]
        self._dual_event_window: set = set()  # 预计算 FOMC ∩ CPI ±3 天 全部 date 集合
        self._gvz_series: dict[str, float] = {}

        self._load()
        self._skipped_count = 0  # 统计: 跳过了多少 bar

    def _precompute_dual_event_windows(self):
        """OPT-5: 预计算 FOMC ∩ CPI ±3 天窗口, 避免 should_skip 里重复算.

        旧实现每 bar 都算 has_fomc AND has_cpi (50K × 100 = 5M strptime).
        现在 _load 时一次性把 FOMC ∩ CPI ±3 天全部 date 算好, 存在 _dual_event_window.
        should_skip 只做一次 in-set 查询.
        """
        window = 3
        cpi_set = self._cpi_date_objs
        if not self._fomc_date_objs or not cpi_set:
            return
        # 对每个 FOMC date, 展开 ±3 天, 看是否落在任一 CPI date ±3 天
        # 实际更高效: 直接展开 FOMC ±3 天, 跟 CPI ∩ 即可
        from datetime import timedelta
        combined = set()
        for d in self._fomc_date_objs:
            for offset in range(-window, window + 1):
                combined.add(d + timedelta(days=offset))
        # 跟 CPI ∩: 只保留 ±3 天有 CPI 的
        for d in cpi_set:
            for offset in range(-window, window + 1):
                combined.add(d + timedelta(days=offset))
        # 真正的 dual window = 任何 FOMC ±3 天内 AND 任何 CPI ±3 天内的 date
        # 但有 FOMC AND CPI 都接近的 date — 直接检查: 任一 date, 3 天内有 FOMC AND 3 天内有 CPI
        # 简单算法: 遍历所有 candidate date (FOMC ±3 ∪ CPI ±3), 检查 3 天内两类是否都有
        result = set()
        for d in combined:
            has_fomc = any(abs((d - fd).days) <= window for fd in self._fomc_date_objs)
            has_cpi = any(abs((d - cd).days) <= window for cd in cpi_set)
            if has_fomc and has_cpi:
                result.add(d)
        self._dual_event_window = result

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
                # OPT-5: 一次性 parse str → date 对象
                from datetime import date
                self._fomc_date_objs = {date.fromisoformat(d) for d in self._fomc_dates}
                self._cpi_date_objs = {date.fromisoformat(d) for d in self._cpi_dates}
                # OPT-5: 预计算 dual event window (避免 should_skip 里每 bar 重算)
                self._precompute_dual_event_windows()
                logger.info(f"[EventFilter] FOMC: {len(self._fomc_dates)} 个, CPI: {len(self._cpi_dates)} 个, dual-window: {len(self._dual_event_window)} 天")

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

        OPT-5 (audit 2026-06-06): 重写以消除 5M 次 strptime.
        ─────────────────────────────────────────────────────
        旧实现:  每 bar 调 datetime.strptime 一次 (bar date) + 双事件时又调 100 次
                  (50 FOMC × abs diff + 50 CPI × abs diff) = 5M strptime on 50K bars
        新实现:  bar date 用 date.fromisoformat (C, 5× 快) + 预计算 _dual_event_window
                  只做 in-set 查询, 单次 ~50ns, 总 ~50K × 50ns = 2.5ms
        """
        try:
            bar_date_str = datetime.fromtimestamp(bar_time, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            return False, ""

        # 1. NFP 窗口 (set 查 str, O(1))
        if self.enable_nfp_skip and bar_date_str in self._nfp_window:
            self._skipped_count += 1
            return True, "NFP_WINDOW"

        # 2. FOMC + CPI 双事件 (查预计算 _dual_event_window)
        if self.enable_dual_event_skip and self._dual_event_window:
            try:
                # date.fromisoformat 是 C 实现, 比 strptime 快 ~5×
                bar_date = date.fromisoformat(bar_date_str)
                if bar_date in self._dual_event_window:
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
            except Exception as e:
                logger.debug("GVZ gate check failed for %s: %s", bar_date_str, e)

        return False, ""

    def stats(self) -> dict:
        return {
            "nfp_window_days": len(self._nfp_window),
            "fomc_events": len(self._fomc_dates),
            "cpi_events": len(self._cpi_dates),
            "gvz_series_days": len(self._gvz_series),
            "total_skipped_bars": self._skipped_count,
        }
