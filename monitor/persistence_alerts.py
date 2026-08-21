"""D13 (audit-defects-2026-08-21 增补): 关键持久化失败的统一告警出口。

背景: factor_health / canary_state 等关键写入失败此前只记 logger.debug
甚至完全吞掉, journal 与面板均不可见, D11 (factor_health 表合同错位)
因此潜伏 40 天无人察觉。

本模块给 alpha/ 层(不依赖 backend 服务图)提供一个轻量进程内告警口:
- 默认写 logs/alerts.log (WARNING 级), 与生产 Alerter 同文件同级别;
- 进程内已注册 Alerter 时(live_service 启动时调用 register_alerter),
  同时走多通道(webhook 等)。
重复抑制: 同一 key 1 小时内只报一次, 防止每周期刷屏。
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_alert_callback: Optional[Callable[[str, str, str], None]] = None
_last_sent: dict[str, float] = {}
_SUPPRESS_SECONDS = 3600.0


def register_alerter(callback: Callable[[str, str, str], None]) -> None:
    """live_service 启动时注入 Alerter.send, 进程内全局生效。"""
    global _alert_callback
    _alert_callback = callback


def persistence_failure_alert(key: str, title: str, message: str) -> None:
    """关键持久化失败告警入口。

    key 用于重复抑制(如同一张表的同一错误); 每小时至多一次,
    保证可见又不刷屏。任何二次异常都吞掉——告警器绝不能反过来
    打断业务路径。
    """
    try:
        now = time.time()
        last = _last_sent.get(key, 0.0)
        if now - last < _SUPPRESS_SECONDS:
            return
        _last_sent[key] = now

        line = (
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | "
            f"[WARNING] [persistence] {title}: {message}"
        )
        # 控制台 + 标准 logging (进 journald/backend.log)
        logger.warning("[persistence] %s: %s", title, message)
        # alerts.log (与 Alerter 同一落点, 面板/巡检能看到)
        log_file = os.path.join("logs", "alerts.log")
        try:
            d = os.path.dirname(log_file)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        # 多通道 (若已注册)
        cb = _alert_callback
        if cb is not None:
            cb("WARNING", title, message)
    except Exception:
        logger.debug("persistence_failure_alert failed silently", exc_info=True)
