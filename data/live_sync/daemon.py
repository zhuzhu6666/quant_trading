"""
data/live_sync/daemon.py — 后台同步守护进程 (T16.5, 2026-06-02)

周期拉取 MT5 新 bar → 过滤 → 写 db.

模式:
- once: 跑一次全量 or 增量, 退出
- daemon: 持续运行, 每 interval_sec 秒增量拉一次
- cron-compatible: 标准输出 + 日志, 适合 Windows Task Scheduler / cron

用法 (CLI):
    python -m data.live_sync.daemon --mode once --type full --timeframes M15,H1,D1
    python -m data.live_sync.daemon --mode daemon --interval 60 --timeframes M15
"""
import logging
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from data.live_sync.orchestrator import SyncOrchestrator, SyncReport, DEFAULT_TIMEFRAMES

logger = logging.getLogger(__name__)


class SyncDaemon:
    """
    后台同步守护进程.

    用法:
        daemon = SyncDaemon(symbol="XAUUSD+", timeframes=["M15", "H1", "D1"])
        daemon.run_once()            # 跑一次增量
        daemon.run_daemon(interval_sec=60)  # 持续运行
    """

    def __init__(self, symbol: str = "XAUUSD+",
                 timeframes: list[str] = None,
                 db_path: str = "data/market_data.db"):
        self.symbol = symbol
        self.timeframes = timeframes or ["M15"]
        self.orch = SyncOrchestrator(db_path=db_path)
        self._running = False
        self._first_run = True

    def run_once(self, full: bool = False, n_bars: int = 5000) -> SyncReport:
        """跑一次 sync (full or incremental)"""
        if not self.orch.connect():
            return SyncReport(run_type="error", symbol=self.symbol, started_utc="",
                              error="MT5 连接失败")

        if full and self._first_run:
            report = self.orch.full_sync(self.symbol, self.timeframes, n_bars=n_bars)
            self._first_run = False
        else:
            report = self.orch.incremental_sync(self.symbol, self.timeframes)
        self.orch.shutdown()
        return report

    def run_daemon(self, interval_sec: int = 60, max_runs: int = 0):
        """
        持续运行, 每 interval_sec 秒增量拉一次.

        Args:
            interval_sec: 拉取间隔 (秒)
            max_runs: 最大运行次数 (0=无限)
        """
        run_count = 0
        self._running = True
        logger.info(f"[SyncDaemon] 启动 daemon, interval={interval_sec}s, timeframes={self.timeframes}")

        while self._running:
            run_count += 1
            start = _time.time()
            logger.info(f"[SyncDaemon] run #{run_count} @ {datetime.utcnow().strftime('%H:%M:%S')} UTC")

            try:
                if not self.orch.connect():
                    logger.error(f"[SyncDaemon] MT5 连接失败, 下次重试")
                    _time.sleep(interval_sec)
                    continue

                report = self.orch.incremental_sync(self.symbol, self.timeframes)
                if report.total_inserted > 0:
                    logger.info(f"[SyncDaemon] run #{run_count}: +{report.total_inserted} bars "
                                f"({report.elapsed_sec:.1f}s)")
                else:
                    logger.info(f"[SyncDaemon] run #{run_count}: 无新 bar ({report.elapsed_sec:.1f}s)")
                self.orch.shutdown()
            except Exception as e:
                logger.error(f"[SyncDaemon] run #{run_count} 异常: {type(e).__name__}: {e}")
                try:
                    self.orch.shutdown()
                except Exception:
                    pass

            if max_runs > 0 and run_count >= max_runs:
                logger.info(f"[SyncDaemon] 达到 max_runs={max_runs}, 退出")
                self._running = False
                break

            # 等到下次 run
            elapsed = _time.time() - start
            wait = max(0, interval_sec - elapsed)
            logger.info(f"[SyncDaemon] 等待 {wait:.0f}s 后下次 run...")
            _time.sleep(wait)

    def stop(self):
        self._running = False
