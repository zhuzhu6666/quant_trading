"""execution/order_retry.py — OrderRejectionSimulator

模拟真实 MT5 拒单 (insufficient margin / invalid price / off quotes) + 补单重试.

设计:
  - 拒单概率模型 (基于市场状态: 事件日 / 低流动性)
  - 补单重试 (指数退避 + 抖动)

集成: 由 order_fn 包裹 MT5 调用后传给 PaperExecutionEngine 的 _open 链路.
"""

from __future__ import annotations
import logging
import time as _time
from typing import Callable, Optional
import numpy as np

logger = logging.getLogger(__name__)


class OrderRejectionSimulator:
    """模拟交易所拒单 + 补单重试 (指数退避).

    用法::

        sim = OrderRejectionSimulator()
        success, n = sim.try_open_with_retry(
            lambda: (True, "filled"),
            is_event_day=False,
        )
    """

    DEFAULT_CONFIG = {
        "base_reject_rate": 0.02,
        "event_day_boost": 3.0,
        "low_liquidity_boost": 2.0,
        "max_retries": 3,
        "backoff_base_ms": 100,
        "backoff_factor": 2.0,
        "max_backoff_ms": 5000,
        "seed": 42,
        # P10 (audit 2026-06-04 FOOTGUN-6): sleep_backoff 默认 True (实盘行为)
        # paper test 想加速可传 False
        "sleep_backoff": True,
    }

    def __init__(self, config: dict | None = None):
        cfg = {**self.DEFAULT_CONFIG, **(config or {})}
        self.config = cfg
        self._rng = np.random.default_rng(cfg["seed"])
        # 统计累计
        self._attempts = 0       # 累计补单次数 (总尝试数)
        self._rejections = 0     # 累计拒单次数
        self._fills = 0          # 累计成交次数

    def should_reject(self, is_event_day: bool = False, is_low_liquidity: bool = False) -> bool:
        rate = self.config["base_reject_rate"]
        if is_event_day:
            rate *= self.config["event_day_boost"]
        if is_low_liquidity:
            rate *= self.config["low_liquidity_boost"]
        rate = min(rate, 1.0)
        reject = self._rng.uniform() < rate
        if reject:
            self._rejections += 1
        return reject

    def backoff_delay_ms(self, attempt: int) -> float:
        delay = min(
            self.config["max_backoff_ms"],
            self.config["backoff_base_ms"] * (self.config["backoff_factor"] ** attempt),
        )
        jitter = 1.0 + self._rng.uniform(0, 0.3)
        return delay * jitter

    def try_open_with_retry(self, order_fn: Callable, is_event_day: bool = False, is_low_liquidity: bool = False) -> tuple:
        max_retries = self.config["max_retries"]
        for attempt in range(max_retries):
            self._attempts += 1
            if self.should_reject(is_event_day, is_low_liquidity):
                if attempt < max_retries - 1:
                    delay_ms = self.backoff_delay_ms(attempt)
                    # P10 (FOOTGUN-6): 之前算出来不 sleep, 实盘接入会风暴
                    if self.config.get("sleep_backoff", True):
                        _time.sleep(delay_ms / 1000.0)
                continue
            success, reason = order_fn()
            if success:
                self._fills += 1
                return (True, attempt)
            logger.warning("order_fn failed: reason=%s", reason)
            return (False, attempt)
        return (False, max_retries)

    def reset(self) -> None:
        self._attempts = 0
        self._rejections = 0
        self._fills = 0

    @property
    def attempts(self) -> int:
        return self._attempts

    @property
    def rejections(self) -> int:
        return self._rejections

    @property
    def fills(self) -> int:
        return self._fills

    def summary(self) -> dict:
        return {
            "attempts": self._attempts,
            "rejections": self._rejections,
            "fills": self._fills,
            "fill_rate_pct": round(self._fills / max(self._attempts, 1) * 100, 2),
            "reject_rate_pct": round(self._rejections / max(self._attempts, 1) * 100, 2),
        }
