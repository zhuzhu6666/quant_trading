"""
monitor/system_health.py — 系统总健康检查 (每 60 秒)

检查项:
  - cTrader 桥连接
  - Live loop 存活性
  - 数据新鲜度 (M1/M5 bars, ticks, L2 depth)
  - 调度器 7 个 job 是否都在运行
  - DuckDB 3 个库可读写
  - 磁盘空间 / 进程内存

综合评分: healthy / degraded / critical
严重故障 → AutoRecovery + Alerter
"""

from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from backend.core.db import duckdb_readonly_connection
from loguru import logger

# ── 数据类型 ────────────────────────────────────────────────────


@dataclass
class ComponentStatus:
    name: str
    status: str  # "ok" | "degraded" | "critical"
    score: float  # 0.0 ~ 1.0
    detail: str = ""
    ts: float = 0.0


@dataclass
class HealthReport:
    overall: str = "healthy"  # "healthy" | "degraded" | "critical"
    overall_score: float = 1.0
    ts: float = 0.0
    components: dict[str, ComponentStatus] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ── 阈值 ────────────────────────────────────────────────────────

THRESHOLDS = {
    "m1_max_age": 300,       # 5 分钟
    "m1_warn_age": 900,      # 15 分钟
    "m5_max_age": 900,       # 15 分钟
    "m5_warn_age": 1800,     # 30 分钟
    "tick_max_age": 7200,    # 2 小时
    "tick_warn_age": 21600,  # 6 小时
    "l2_max_age": 60,        # 1 分钟
    "l2_warn_age": 300,      # 5 分钟
    "disk_min_gb": 10,       # 最少 10GB 剩余
    "disk_warn_gb": 20,      # 低于 20GB 告警
    "memory_max_pct": 80,    # 内存使用率上限 (%)
}

ADVISORY_ONLY_COMPONENTS = {
    "tick_data",  # Dukascopy tick 库仅供研究/订单流分析, 不应阻断 cTrader live
}


def _active_bar_component() -> str:
    """Return the bar freshness component that should block live trading."""
    try:
        from config.runtime_config import shared as _runtime_cfg

        timeframe = str(getattr(_runtime_cfg(), "timeframe", "M5") or "M5").upper()
    except Exception:
        timeframe = "M5"
    if timeframe == "M1":
        return "bar_m1"
    return "bar_m5"


def _advisory_only_components() -> set[str]:
    advisory = set(ADVISORY_ONLY_COMPONENTS)
    active_bar = _active_bar_component()
    for name in ("bar_m1", "bar_m5"):
        if name != active_bar:
            advisory.add(name)
    return advisory


def _market_closed_for_freshness(now: float, latest_market_data_ts: float | None = None) -> tuple[bool, str]:
    """Return whether stale market data is expected because the instrument is closed."""
    try:
        from backend.services.market_session import evaluate_market_session

        session = evaluate_market_session(
            symbol="XAUUSD+",
            now_ts=now,
            latest_market_data_ts=latest_market_data_ts,
        )
        if session.status in {"closed_confirmed", "closed_pending_confirmation", "closed_pending_positions"}:
            return True, f"{session.status}:{session.reason}"
    except Exception as exc:
        logger.debug("[system_health] market session freshness check failed: {}", exc)
    return False, ""


# ── 健康检查器 ───────────────────────────────────────────────────


class SystemHealth:
    """系统总健康检查器 — 每 60 秒由 scheduler job 触发."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_report: HealthReport | None = None
        self._alerter: Callable | None = None  # alert callback

    def set_alerter(self, alerter: Callable) -> None:
        self._alerter = alerter

    def get_last_report(self) -> HealthReport | None:
        with self._lock:
            return self._last_report

    def run(self) -> HealthReport:
        """执行一次全量健康检查，返回报告."""
        report = HealthReport(ts=time.time())
        components: dict[str, ComponentStatus] = {}
        errors: list[str] = []

        # 1. cTrader 桥连接
        try:
            from backend.services.live_service import _get_ctrader
            bridge, err, warming = _get_ctrader()
            if err:
                components["ctrader_bridge"] = ComponentStatus(
                    name="cTrader 桥",
                    status="critical",
                    score=0.0,
                    detail=f"unavailable: {err[:100]}",
                    ts=time.time(),
                )
                errors.append(f"cTrader bridge: {err[:100]}")
            elif warming:
                components["ctrader_bridge"] = ComponentStatus(
                    name="cTrader 桥",
                    status="degraded",
                    score=0.3,
                    detail="warming up (not yet connected)",
                    ts=time.time(),
                )
            elif not bridge.is_connected:
                components["ctrader_bridge"] = ComponentStatus(
                    name="cTrader 桥",
                    status="critical",
                    score=0.0,
                    detail="disconnected",
                    ts=time.time(),
                )
                errors.append("cTrader bridge: disconnected")
            else:
                components["ctrader_bridge"] = ComponentStatus(
                    name="cTrader 桥",
                    status="ok",
                    score=1.0,
                    detail="connected",
                    ts=time.time(),
                )
        except Exception as e:
            components["ctrader_bridge"] = ComponentStatus(
                name="cTrader 桥", status="critical", score=0.0,
                detail=f"check failed: {e}", ts=time.time(),
            )
            errors.append(f"ctrader_bridge check: {e}")

        # 2. Live loop 存活性
        try:
            from backend.services.live_service import loop_status
            ls = loop_status()
            if ls.get("running"):
                components["live_loop"] = ComponentStatus(
                    name="实盘循环",
                    status="ok",
                    score=1.0,
                    detail=f"pid={ls.get('pid')} broker={ls.get('broker')}",
                    ts=time.time(),
                )
            else:
                components["live_loop"] = ComponentStatus(
                    name="实盘循环",
                    status="critical",
                    score=0.0,
                    detail="not running",
                    ts=time.time(),
                )
                errors.append("Live loop not running")
        except Exception as e:
            components["live_loop"] = ComponentStatus(
                name="实盘循环", status="critical", score=0.0,
                detail=f"check failed: {e}", ts=time.time(),
            )
            errors.append(f"live_loop check: {e}")

        # 3. 数据新鲜度
        self._check_data_freshness(components, errors)

        # 4. DuckDB 可读写
        self._check_duckdb(components, errors)

        # 5. 磁盘 / 内存
        self._check_system_resources(components, errors)

        # ── 综合评分 ──
        report.components = components
        report.errors = errors

        scores = [c.score for c in components.values()]
        report.overall_score = sum(scores) / len(scores) if scores else 0.0

        advisory_only_components = _advisory_only_components()
        criticals = [
            name for name, c in components.items()
            if c.status == "critical" and name not in advisory_only_components
        ]
        degradeds = [
            name for name, c in components.items()
            if c.status == "degraded" and name not in advisory_only_components
        ]
        if criticals:
            report.overall = "critical"
        elif degradeds:
            report.overall = "degraded"
        else:
            report.overall = "healthy"

        with self._lock:
            self._last_report = report

        # 严重故障 → 告警
        if report.overall == "critical" and self._alerter:
            try:
                blocking_errors = [
                    err for err in errors
                    if not err.lower().startswith("tick data stale:")
                ]
                detail = "; ".join((blocking_errors or errors)[:5])
                self._alerter("ERROR", "⚠️ 系统健康检查", f"级别: critical\n{detail}")
            except Exception:
                pass

        logger.info(
            "[system_health] overall={} score={:.2f} components={} errors={}",
            report.overall, report.overall_score,
            {k: v.status for k, v in components.items()},
            len(errors),
        )
        return report

    # ── 子检查 ────────────────────────────────────────────

    def _check_data_freshness(
        self, components: dict[str, ComponentStatus], errors: list[str]
    ) -> None:
        """检查 bars / ticks / L2 数据新鲜度."""
        now = time.time()
        market_closed = False
        market_closed_detail = ""

        try:
            from backend.core.db import DUCKDB_BARS
            with duckdb_readonly_connection(DUCKDB_BARS, snapshot_first=True) as db:

                # M1
                m1_ts = db.execute(
                    "SELECT MAX(time) FROM bars WHERE symbol='XAUUSD+' AND timeframe='M1'"
                ).fetchone()[0]
                if m1_ts:
                    age = now - m1_ts
                    market_closed, market_closed_detail = _market_closed_for_freshness(now, float(m1_ts))
                    if age < THRESHOLDS["m1_max_age"]:
                        components["bar_m1"] = ComponentStatus(
                            name="M1 Bar", status="ok", score=1.0,
                            detail=f"{age/60:.0f} min ago", ts=now,
                        )
                    elif market_closed:
                        components["bar_m1"] = ComponentStatus(
                            name="M1 Bar", status="ok", score=1.0,
                            detail=f"market closed; last {age/60:.0f} min ago ({market_closed_detail})", ts=now,
                        )
                    elif age < THRESHOLDS["m1_warn_age"]:
                        components["bar_m1"] = ComponentStatus(
                            name="M1 Bar", status="degraded", score=0.5,
                            detail=f"{age/60:.0f} min ago", ts=now,
                        )
                    else:
                        components["bar_m1"] = ComponentStatus(
                            name="M1 Bar", status="critical", score=0.0,
                            detail=f"{age/60:.0f} min ago (stale)", ts=now,
                        )
                        errors.append(f"M1 bar stale: {age/60:.0f} min")
                else:
                    components["bar_m1"] = ComponentStatus(
                        name="M1 Bar", status="critical", score=0.0,
                        detail="no data", ts=now,
                    )

                # M5
                m5_ts = db.execute(
                    "SELECT MAX(time) FROM bars WHERE symbol='XAUUSD+' AND timeframe='M5'"
                ).fetchone()[0]
                if m5_ts:
                    age = now - m5_ts
                    if not market_closed:
                        market_closed, market_closed_detail = _market_closed_for_freshness(now, float(m5_ts))
                    if age < THRESHOLDS["m5_max_age"]:
                        components["bar_m5"] = ComponentStatus(
                            name="M5 Bar", status="ok", score=1.0,
                            detail=f"{age/60:.0f} min ago", ts=now,
                        )
                    elif market_closed:
                        components["bar_m5"] = ComponentStatus(
                            name="M5 Bar", status="ok", score=1.0,
                            detail=f"market closed; last {age/60:.0f} min ago ({market_closed_detail})", ts=now,
                        )
                    elif age < THRESHOLDS["m5_warn_age"]:
                        components["bar_m5"] = ComponentStatus(
                            name="M5 Bar", status="degraded", score=0.5,
                            detail=f"{age/60:.0f} min ago", ts=now,
                        )
                    else:
                        components["bar_m5"] = ComponentStatus(
                            name="M5 Bar", status="critical", score=0.0,
                            detail=f"{age/60:.0f} min ago (stale)", ts=now,
                        )
                        errors.append(f"M5 bar stale: {age/60:.0f} min")
                else:
                    components["bar_m5"] = ComponentStatus(
                        name="M5 Bar", status="critical", score=0.0,
                        detail="no data", ts=now,
                    )
        except Exception as e:
            components["data_freshness"] = ComponentStatus(
                name="数据新鲜度", status="critical", score=0.0,
                detail=f"check failed: {e}", ts=now,
            )
            errors.append(f"data_freshness: {e}")

        # Ticks (ticks.duckdb)
        try:
            with duckdb_readonly_connection(
                Path(__file__).resolve().parent.parent / "data" / "ticks.duckdb",
                snapshot_first=True,
            ) as tdb:
                tick_ts = tdb.execute("SELECT MAX(time) FROM ticks").fetchone()[0]
            if tick_ts:
                age = now - tick_ts
                if age < THRESHOLDS["tick_max_age"]:
                    components["tick_data"] = ComponentStatus(
                        name="Tick 数据", status="ok", score=1.0,
                        detail=f"{age/60:.0f} min ago", ts=now,
                    )
                elif market_closed:
                    components["tick_data"] = ComponentStatus(
                        name="Tick 数据", status="ok", score=1.0,
                        detail=f"market closed; last {age/60:.0f} min ago ({market_closed_detail})", ts=now,
                    )
                elif age < THRESHOLDS["tick_warn_age"]:
                    components["tick_data"] = ComponentStatus(
                        name="Tick 数据", status="degraded", score=0.5,
                        detail=f"{age/60:.0f} min ago", ts=now,
                    )
                else:
                    components["tick_data"] = ComponentStatus(
                        name="Tick 数据", status="critical", score=0.0,
                        detail=f"{age/60:.0f} min ago (stale)", ts=now,
                    )
                    errors.append(f"Tick data stale: {age/60:.0f} min")
            else:
                components["tick_data"] = ComponentStatus(
                    name="Tick 数据", status="critical", score=0.0,
                    detail="no data", ts=now,
                )
        except Exception as e:
            components["tick_data"] = ComponentStatus(
                name="Tick 数据", status="critical", score=0.0,
                detail=f"check failed: {e}", ts=now,
            )

        # L2 depth (l2.duckdb)
        try:
            with duckdb_readonly_connection(
                Path(__file__).resolve().parent.parent / "data" / "l2.duckdb",
                snapshot_first=True,
            ) as ldb:
                l2_ts = ldb.execute("SELECT MAX(ts) FROM orderbook_changes").fetchone()[0]
                l2_cnt = ldb.execute("SELECT COUNT(*) FROM orderbook_changes").fetchone()[0]
            if l2_ts and l2_cnt > 0:
                age = now - l2_ts
                if age < THRESHOLDS["l2_max_age"]:
                    components["l2_depth"] = ComponentStatus(
                        name="L2 订单簿", status="ok", score=1.0,
                        detail=f"{l2_cnt:,} rows, last {age:.0f}s ago", ts=now,
                    )
                elif market_closed:
                    components["l2_depth"] = ComponentStatus(
                        name="L2 订单簿", status="ok", score=1.0,
                        detail=f"market closed; {l2_cnt:,} rows, last {age:.0f}s ago ({market_closed_detail})", ts=now,
                    )
                elif age < THRESHOLDS["l2_warn_age"]:
                    components["l2_depth"] = ComponentStatus(
                        name="L2 订单簿", status="degraded", score=0.5,
                        detail=f"{l2_cnt:,} rows, last {age:.0f}s ago", ts=now,
                    )
                else:
                    components["l2_depth"] = ComponentStatus(
                        name="L2 订单簿", status="critical", score=0.0,
                        detail=f"{l2_cnt:,} rows, last {age:.0f}s ago (stale)", ts=now,
                    )
            else:
                components["l2_depth"] = ComponentStatus(
                    name="L2 订单簿", status="degraded", score=0.3,
                    detail="no data yet (market may be closed)", ts=now,
                )
        except Exception as e:
            components["l2_depth"] = ComponentStatus(
                name="L2 订单簿", status="critical", score=0.0,
                detail=f"check failed: {e}", ts=now,
            )

    def _check_duckdb(
        self, components: dict[str, ComponentStatus], errors: list[str]
    ) -> None:
        """确认 3 个 DuckDB 库可读写."""
        dbs = [
            ("bars.duckdb", "K 线库"),
            ("ticks.duckdb", "Tick 库"),
            ("l2.duckdb", "L2 库"),
        ]
        base = Path(__file__).resolve().parent.parent / "data"
        for fname, label in dbs:
            try:
                path = str(base / fname)
                if not os.path.isfile(path):
                    components[f"db_{fname.split('.')[0]}"] = ComponentStatus(
                        name=label, status="critical", score=0.0,
                        detail="file not found", ts=time.time(),
                    )
                    errors.append(f"DuckDB {fname} not found")
                    continue
                with duckdb_readonly_connection(path, snapshot_first=True) as conn:
                    conn.execute("SELECT 1")
                components[f"db_{fname.split('.')[0]}"] = ComponentStatus(
                    name=label, status="ok", score=1.0,
                    detail="readable",
                )
            except Exception as e:
                components[f"db_{fname.split('.')[0]}"] = ComponentStatus(
                    name=label, status="critical", score=0.0,
                    detail=str(e)[:100], ts=time.time(),
                )
                errors.append(f"DuckDB {fname}: {e}")

    def _check_system_resources(
        self, components: dict[str, ComponentStatus], errors: list[str]
    ) -> None:
        """磁盘剩余空间 + 进程内存."""
        now = time.time()

        # 磁盘 (data 目录所在分区)
        try:
            import shutil
            total, used, free = shutil.disk_usage(
                Path(__file__).resolve().parent.parent / "data"
            )
            free_gb = free / (1024 ** 3)
            if free_gb >= THRESHOLDS["disk_warn_gb"]:
                components["disk_space"] = ComponentStatus(
                    name="磁盘空间", status="ok", score=1.0,
                    detail=f"{free_gb:.1f} GB free", ts=now,
                )
            elif free_gb >= THRESHOLDS["disk_min_gb"]:
                components["disk_space"] = ComponentStatus(
                    name="磁盘空间", status="degraded", score=0.5,
                    detail=f"{free_gb:.1f} GB free (low)", ts=now,
                )
            else:
                components["disk_space"] = ComponentStatus(
                    name="磁盘空间", status="critical", score=0.0,
                    detail=f"{free_gb:.1f} GB free (critical)", ts=now,
                )
                errors.append(f"Disk low: {free_gb:.1f} GB free")
        except Exception as e:
            components["disk_space"] = ComponentStatus(
                name="磁盘空间", status="degraded", score=0.5,
                detail=f"check failed: {e}", ts=now,
            )

        # 内存 (通过 psutil 或 ps 读取)
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            mem_pct = proc.memory_percent()
            rss_mb = proc.memory_info().rss / (1024 ** 2)
            # psutil.memory_percent() 已返回 0~100 百分比，直接与阈值比对
            if mem_pct < THRESHOLDS["memory_max_pct"]:
                components["memory"] = ComponentStatus(
                    name="进程内存", status="ok", score=1.0,
                    detail=f"{mem_pct:.1f}% ({rss_mb:.0f} MB)", ts=now,
                )
            else:
                components["memory"] = ComponentStatus(
                    name="进程内存", status="degraded", score=0.5,
                    detail=f"{mem_pct:.1f}% ({rss_mb:.0f} MB) high", ts=now,
                )
        except ImportError:
            # 无 psutil 时跳过
            pass
        except Exception as e:
            components["memory"] = ComponentStatus(
                name="进程内存", status="degraded", score=0.5,
                detail=f"check failed: {e}", ts=now,
            )


# ── 单例 ────────────────────────────────────────────────────────

_system_health_instance: SystemHealth | None = None
_system_health_lock = threading.Lock()


def shared() -> SystemHealth:
    global _system_health_instance
    if _system_health_instance is None:
        with _system_health_lock:
            if _system_health_instance is None:
                _system_health_instance = SystemHealth()
    return _system_health_instance
