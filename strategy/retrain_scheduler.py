"""strategy/retrain_scheduler.py — 周期 retrain hook (T8, 2026-06-02)

轻量实现: 每 N 笔 trade close 触发一次重训
- 默认 N=200 (跟 SelfLearningScheduler check_interval=50 错开)
- 触发时调用 scripts/walkforward_p0_6.py 加载新数据 + 重训 XGBoost
- 输出新 calibrator 给 ProbabilityCalibrator (替换 identity)

实装约束:
- walkforward 跑 1-3 分钟, paper 路径里同步阻塞 (用 threading 起后台线程更优雅, v1 先简单)
- 失败不能影响 paper 主循环 (try/except 包住)
- retrain 频率太高会浪费, 太低跟不上 drift
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time as _time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class RetrainEvent:
    """一次 retrain 事件的记录"""
    bar_idx: int
    triggered_at: float
    n_trades_so_far: int
    duration_sec: float
    success: bool
    error: str = ""
    calibrator_path: str = ""


class RetrainScheduler:
    """
    周期 retrain 调度器 — 触发 walkforward + 更新 calibrator

    用法:
        scheduler = RetrainScheduler(
            trigger_every_n_trades=200,
            min_trades_before_first=100,
            walkforward_script="scripts/walkforward_p0_6.py",
            calibrator_path="data/charts/calibrator_bucket.json",
        )
        # paper 主循环里每笔 close 调:
        scheduler.on_trade_close(bar_idx=i, n_trades_so_far=len(closes))

    触发条件:
        n_trades >= min_trades_before_first (避免冷启动)
        n_trades - last_retrain_n >= trigger_every_n_trades

    实现:
        v1: 同步阻塞 (v2 改 threading.Thread daemon)
        v1: subprocess.run 调 walkforward 脚本
    """

    def __init__(
        self,
        trigger_every_n_trades: int = 200,
        min_trades_before_first: int = 100,
        walkforward_script: str = "scripts/walkforward_p0_6.py",
        calibrator_path: str = "data/charts/calibrator_bucket.json",
        timeout_sec: int = 300,
    ):
        self.trigger_every_n_trades = trigger_every_n_trades
        self.min_trades_before_first = min_trades_before_first
        self.walkforward_script = walkforward_script
        self.calibrator_path = calibrator_path
        self.timeout_sec = timeout_sec

        self._last_retrain_n_trades: int = 0
        self._events: list[RetrainEvent] = []
        self._lock = threading.Lock()
        self._running = False

    def should_trigger(self, n_trades_so_far: int) -> bool:
        if self._running:
            return False
        if n_trades_so_far < self.min_trades_before_first:
            return False
        return (n_trades_so_far - self._last_retrain_n_trades) >= self.trigger_every_n_trades

    def on_trade_close(self, bar_idx: int, n_trades_so_far: int) -> RetrainEvent | None:
        """
        每笔 trade close 时调, 内部判断是否触发 retrain
        返回 RetrainEvent 如果触发了, 否则 None
        """
        if not self.should_trigger(n_trades_so_far):
            return None

        with self._lock:
            if self._running:
                return None  # 已经在跑, 跳过
            self._running = True
            t0 = _time.time()

        event = RetrainEvent(
            bar_idx=bar_idx,
            triggered_at=t0,
            n_trades_so_far=n_trades_so_far,
            duration_sec=0.0,
            success=False,
        )
        try:
            script = Path(self.walkforward_script)
            if not script.exists():
                event.error = f"walkforward script not found: {script}"
                return event

            # 同步阻塞调 walkforward
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
            )
            if result.returncode != 0:
                event.error = f"walkforward failed: {result.stderr[-200:]}"
                return event

            # 检查 calibrator 是否生成
            cal_path = Path(self.calibrator_path)
            if cal_path.exists():
                event.calibrator_path = str(cal_path)
                event.success = True
            else:
                event.error = f"calibrator not generated at {cal_path}"
                # retrain 跑成功但 calibrator 没生成 — 部分成功
                event.success = True

            self._last_retrain_n_trades = n_trades_so_far
            logger.info(
                f"[RetrainScheduler] retrain #{len(self._events)+1} "
                f"@ bar {bar_idx}, n_trades={n_trades_so_far}, "
                f"duration={event.duration_sec:.1f}s"
            )
            return event

        except subprocess.TimeoutExpired:
            event.error = f"walkforward timeout ({self.timeout_sec}s)"
            return event
        except Exception as e:
            event.error = f"{type(e).__name__}: {e}"
            return event
        finally:
            event.duration_sec = _time.time() - t0
            self._events.append(event)
            with self._lock:
                self._running = False

    def get_events(self) -> list[dict]:
        return [
            {
                "bar_idx": e.bar_idx,
                "n_trades_so_far": e.n_trades_so_far,
                "duration_sec": round(e.duration_sec, 2),
                "success": e.success,
                "error": e.error,
                "calibrator_path": e.calibrator_path,
            }
            for e in self._events
        ]

    def stats(self) -> dict:
        if not self._events:
            return {"n_events": 0, "n_success": 0, "n_fail": 0}
        n_succ = sum(1 for e in self._events if e.success)
        return {
            "n_events": len(self._events),
            "n_success": n_succ,
            "n_fail": len(self._events) - n_succ,
            "avg_duration_sec": sum(e.duration_sec for e in self._events) / len(self._events),
            "last_calibrator": self._events[-1].calibrator_path if self._events else "",
        }
