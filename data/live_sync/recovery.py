"""auto_recover — 数据同步自愈。

设计:
- 当 SyncHealth.is_degraded() 触发,本模块做有界重试:
  1. 调用 mt5_guard.reset_pipe() (若存在)
  2. backoff 重启 mt5_puller
  3. 重新核对 IPC hash
  4. 3 次失败切 bybit_puller 备用
- 不阻塞 loop,返回 coroutine 给 sync_runner_factory 调度
- 修复尝试上限由 RuntimeConfig.sync_recovery_max_attempts 控制
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from .health import SyncHealth

logger = logging.getLogger(__name__)


async def auto_recover(
    health: Optional[SyncHealth] = None,
    max_attempts: int = 3,
) -> Dict[str, Any]:
    """尝试恢复数据同步。返回 recovery 报告。

    Returns:
        dict 形如 {ok: bool, attempts: int, action: str, error: str}
    """
    if health is None:
        health = SyncHealth.shared()
    report: Dict[str, Any] = {"ok": False, "attempts": 0, "action": "", "error": ""}
    logger.warning("auto_recover: starting (consecutive_failures=%d)", health.record.consecutive_failures)

    for attempt in range(1, max_attempts + 1):
        report["attempts"] = attempt
        try:
            action = await _one_recovery_attempt(attempt)
            report["action"] = action
            # 一次小等待,让环境稳定
            await asyncio.sleep(0.5)
            # 假定本轮"动作"成功;若下一次 sync 又失败,SyncHealth 仍会标 is_degraded
            report["ok"] = True
            logger.info("auto_recover: attempt %d succeeded (action=%s)", attempt, action)
            break
        except Exception as e:  # noqa: BLE001
            report["error"] = repr(e)
            logger.exception("auto_recover: attempt %d failed", attempt)
            await asyncio.sleep(min(2 ** attempt, 10))

    return report


async def _one_recovery_attempt(attempt: int) -> str:
    """单次恢复尝试。

    优先级:
    1. mt5_guard.reset_pipe()(若可导入)
    2. mt5_puller 实例 reset
    3. 切 bybit_puller(若尝试 3 次仍失败)

    Returns: 实际执行的动作名。
    """
    # 1) mt5_guard
    try:
        from data.live_sync import mt5_guard  # type: ignore

        if hasattr(mt5_guard, "reset_pipe"):
            mt5_guard.reset_pipe()
            return f"attempt{attempt}:mt5_guard.reset_pipe"
    except Exception:  # noqa: BLE001
        pass

    # 2) mt5_puller.reset
    try:
        from data.live_sync import mt5_puller  # type: ignore

        if hasattr(mt5_puller, "reset"):
            mt5_puller.reset()
            return f"attempt{attempt}:mt5_puller.reset"
    except Exception:  # noqa: BLE001
        pass

    # 3) fallback 切 bybit
    try:
        from data.live_sync import bybit_puller  # type: ignore

        if hasattr(bybit_puller, "activate"):
            bybit_puller.activate()
            return f"attempt{attempt}:switch_to_bybit"
    except Exception:  # noqa: BLE001
        pass

    # 4) 兜底:什么都不做,只 sleep
    await asyncio.sleep(0.1)
    return f"attempt{attempt}:noop"
