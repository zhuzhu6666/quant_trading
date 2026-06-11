"""Sync service — 把 data.live_sync 包装成 LoopHost 可用的 factory。

对外保持原 get_status / run_sync_once 接口(其它服务/测试还在用),
新增 sync_runner_factory 给 LoopHost 用。
Phase 1.4 会把 health/recovery/quality_gate 接进来。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from loguru import logger

from backend.jobs.progress import ProgressCB
from backend.runtime.runtime_state import RuntimeState

# 保留模块级 logger 名称(老代码用 loguru,新代码用 stdlib logging,统一用 name 区分)
_stdlib_logger = logging.getLogger(__name__)


def get_status() -> dict:
    """Return current sync status (from the live_sync_status.json on disk)."""
    from scripts.live_sync import get_status as _get_status

    return _get_status()


def run_sync_once(params: dict[str, Any], progress_cb: ProgressCB) -> dict:
    """Run a one-shot sync. Delegates to scripts/live_sync.run_sync_once."""
    from scripts.live_sync import run_sync_once as _run

    return _run(
        timeframes=params.get("timeframes", ["M15", "H1", "D1"]),
        sync_type=params.get("type", "incremental"),
        progress_cb=progress_cb,
    )


# ----- Phase 1.1 新增:LoopHost factory -----
async def sync_runner_factory(state: RuntimeState) -> None:
    """LoopHost 的 sync 工厂:周期跑一次 orchestrator.run_once。

    Phase 1.4 接入: SyncHealth / recovery.auto_recover。
    """
    from data.live_sync.health import SyncHealth

    loop_status = state.get_loop("sync")
    interval_sec = int(loop_status.extra.get("interval_sec", 300)) if loop_status else 300
    health = SyncHealth.shared()
    _stdlib_logger.info("sync loop started, interval=%ds", interval_sec)
    while True:
        try:
            await _do_one_sync(state, health)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _stdlib_logger.exception("sync iteration failed")
            health.record_failure("iteration_exception")
            state.emit_metric("sync_iteration_error", {})

        # Phase 1.4 接入: 若已 degraded,触发自愈
        if health.is_degraded():
            from data.live_sync.recovery import auto_recover

            try:
                rec = await auto_recover(health=health, max_attempts=3)
                _stdlib_logger.warning("sync auto_recover report: %s", rec)
                state.emit_metric("sync_recovery_attempted", {"ok": 1 if rec.get("ok") else 0})
                if rec.get("ok"):
                    health.record_success()  # 假定自愈已成功,重置连续失败计数
                    from monitor.evolution_story import EvolutionStory
                    EvolutionStory.shared().append(
                        "sync_recovered",
                        {"attempts": rec.get("attempts"), "action": rec.get("action")},
                    )
            except Exception:  # noqa: BLE001
                _stdlib_logger.exception("auto_recover failed")

        await asyncio.sleep(interval_sec)


async def _do_one_sync(state: RuntimeState, health) -> None:
    """一次同步迭代。Phase 1.4 接入 SyncHealth / DataQualityGate。"""
    from monitor.evolution_story import EvolutionStory

    health.record_attempt()
    try:
        from data.live_sync.orchestrator import run_once  # type: ignore

        result = run_once()
    except Exception as e:  # noqa: BLE001
        _stdlib_logger.debug("orchestrator.run_once failed: %r", e)
        health.record_failure(f"run_once: {e!r}")
        state.emit_metric("sync_iteration_skipped", {})
        return

    inserted = int(result.get("total_inserted", 0)) if isinstance(result, dict) else 0
    per_tf = result.get("per_tf", []) if isinstance(result, dict) else []

    # 从 per_tf 提取 last_bar_ts_by_tf(per_tf 元素是 dict,可能含 last_sync_utc / last_bar_ts)
    last_bar_ts_by_tf: dict[str, float] = {}
    for entry in per_tf:
        if not isinstance(entry, dict):
            continue
        tf = entry.get("timeframe") or entry.get("tf")
        ts_raw = entry.get("last_bar_ts") or entry.get("last_sync_utc") or entry.get("last_ts")
        if not tf or ts_raw is None:
            continue
        try:
            # 接受 ISO 字符串或 epoch float
            if isinstance(ts_raw, (int, float)):
                last_bar_ts_by_tf[str(tf)] = float(ts_raw)
            else:
                from datetime import datetime, timezone

                dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                last_bar_ts_by_tf[str(tf)] = dt.timestamp()
        except Exception:  # noqa: BLE001
            continue

    is_success = (not result.get("error")) if isinstance(result, dict) else False
    if is_success:
        health.record_success(last_bar_ts_by_tf=last_bar_ts_by_tf)
        # 累计事件流(只记 inserted>0 的成功,避免每 5 分钟刷"成功"事件)
        if inserted > 0:
            EvolutionStory.shared().append(
                "sync_success",
                {"inserted": inserted, "per_tf": last_bar_ts_by_tf},
            )
    else:
        err = str(result.get("error", "unknown")) if isinstance(result, dict) else "unknown"
        health.record_failure(err)
        EvolutionStory.shared().append("sync_failure", {"error": err})

    state.emit_metric("sync_iteration_done", {"inserted": inserted})
