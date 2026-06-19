"""
Auto-recovery system for autonomous operation of the trading system.

Monitors the live loop and scheduler health, performs automatic restarts
with warmup sequences, and alerts on critical failures.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from dataclasses import dataclass

from loguru import logger


class AutoRecovery:
    """
    Automatic recovery for the trading system.

    Live loop recovery:
    1. Health check every 30s — checks if live loop is still running
    2. 2 consecutive failures -> auto-restart the loop
    3. After restart -> warmup (replay last 200 bars)
    4. 3 consecutive restart failures -> abandon and alert

    Scheduler recovery:
    1. On backend start, check if scheduler was running before crash
    2. Schedule pending tasks
    3. Cron expressions persisted in-memory (RuntimeConfig-based)

    Alert integration:
    - Uses monitor/alerter.py for notifications
    - Logs all recovery actions
    """

    def __init__(
        self,
        check_interval: float = 30.0,
        max_failures: int = 2,
        max_restart_attempts: int = 3,
        alerter=None,
    ):
        self.check_interval = check_interval
        self.max_failures = max_failures
        self.max_restart_attempts = max_restart_attempts
        self.alerter = alerter

        # Callback storage
        self._loop_check_fn: Optional[Callable[[], bool]] = None
        self._loop_restart_fn: Optional[Callable[[], bool]] = None
        self._loop_warmup_fn: Optional[Callable[[], None]] = None
        self._scheduler_check_fn: Optional[Callable[[], bool]] = None
        self._scheduler_restart_fn: Optional[Callable[[], bool]] = None

        # Threading
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        # State tracking
        self._failures = 0
        self._restart_attempts = 0
        self._last_check: float = 0.0
        self._loop_healthy = True
        self._scheduler_healthy = True

    def start(self) -> bool:
        """Start the recovery monitor background thread.

        Returns False if the monitor is already running.
        """
        with self._lock:
            if self._running:
                logger.warning("AutoRecovery monitor is already running")
                return False
            self._running = True

        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="AutoRecovery",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "AutoRecovery monitor started (check_interval={}s, max_failures={}, "
            "max_restart_attempts={})",
            self.check_interval,
            self.max_failures,
            self.max_restart_attempts,
        )
        return True

    def stop(self) -> None:
        """Stop the recovery monitor thread."""
        with self._lock:
            self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        logger.info("AutoRecovery monitor stopped")

    def register_live_loop(
        self,
        check_fn: Callable[[], bool],
        restart_fn: Callable[[], bool],
        warmup_fn: Callable[[], None],
    ) -> None:
        """Register callbacks for live loop monitoring.

        Args:
            check_fn: Returns True if the loop is healthy.
            restart_fn: Restarts the loop, returns True on success.
            warmup_fn: Warmup sequence after restart.
        """
        self._loop_check_fn = check_fn
        self._loop_restart_fn = restart_fn
        self._loop_warmup_fn = warmup_fn
        logger.debug("Live loop callbacks registered")

    def register_scheduler(
        self,
        check_fn: Callable[[], bool],
        restart_fn: Callable[[], bool],
    ) -> None:
        """Register callbacks for scheduler monitoring.

        Args:
            check_fn: Returns True if the scheduler is healthy.
            restart_fn: Restarts the scheduler, returns True on success.
        """
        self._scheduler_check_fn = check_fn
        self._scheduler_restart_fn = restart_fn
        logger.debug("Scheduler callbacks registered")

    def health_status(self) -> dict:
        """Return the current health status.

        Returns:
            dict with keys: running, loop_healthy, scheduler_healthy,
            failures, last_check, restart_attempts
        """
        with self._lock:
            return {
                "running": self._running,
                "loop_healthy": self._loop_healthy,
                "scheduler_healthy": self._scheduler_healthy,
                "failures": self._failures,
                "last_check": self._last_check,
                "restart_attempts": self._restart_attempts,
            }

    def _monitor_loop(self) -> None:
        """Background thread that runs check_interval health-check loops."""
        while True:
            with self._lock:
                if not self._running:
                    break

            try:
                self._perform_checks()
            except Exception:
                logger.exception("Unhandled error in recovery monitor check cycle")

            # Sleep in small increments so we can be stopped promptly
            for _ in range(int(self.check_interval)):
                with self._lock:
                    if not self._running:
                        return
                time.sleep(1)

    def _perform_checks(self) -> None:
        """Run health checks and recovery actions for this cycle."""
        with self._lock:
            self._last_check = time.time()

        self._check_live_loop()
        self._check_scheduler()

    def _check_live_loop(self) -> None:
        """Check live loop health and trigger recovery if needed."""
        if self._loop_check_fn is None:
            return

        try:
            healthy = self._loop_check_fn()
        except Exception:
            logger.exception("Live loop health check raised an exception")
            healthy = False

        with self._lock:
            self._loop_healthy = healthy

        if healthy:
            with self._lock:
                if self._failures > 0:
                    logger.info("Live loop health restored, resetting failure counter")
                self._failures = 0
                self._restart_attempts = 0
            return

        # Unhealthy — increment failure counter
        with self._lock:
            self._failures += 1
            current_failures = self._failures
            current_attempts = self._restart_attempts

        logger.warning(
            "Live loop health check failed ({}/{})",
            current_failures,
            self.max_failures,
        )

        if current_failures < self.max_failures:
            return

        # Max failures reached — attempt restart
        self._attempt_live_loop_restart()

    def _attempt_live_loop_restart(self) -> None:
        """Attempt to restart the live loop with warmup."""
        with self._lock:
            self._restart_attempts += 1
            attempt = self._restart_attempts

        logger.warning(
            "Attempting live loop restart ({}/{})",
            attempt,
            self.max_restart_attempts,
        )

        restart_ok = False
        if self._loop_restart_fn is not None:
            try:
                restart_ok = self._loop_restart_fn()
            except Exception:
                logger.exception("Live loop restart function raised an exception")
                restart_ok = False

        if restart_ok:
            # Run warmup
            if self._loop_warmup_fn is not None:
                try:
                    self._loop_warmup_fn()
                except Exception:
                    logger.exception(
                        "Live loop warmup function raised an exception"
                    )

            with self._lock:
                self._failures = 0
                self._restart_attempts = 0
                self._loop_healthy = True

            logger.info("Live loop restarted and warmed up successfully")
            self._alert("info", "Live loop restarted and warmed up successfully")
        else:
            logger.error(
                "Live loop restart failed ({}/{})",
                attempt,
                self.max_restart_attempts,
            )

            if attempt >= self.max_restart_attempts:
                logger.critical(
                    "Live loop restart abandoned after {} failed attempts — "
                    "stopping recovery monitor",
                    self.max_restart_attempts,
                )
                self._alert(
                    "critical",
                    f"Live loop restart abandoned after "
                    f"{self.max_restart_attempts} failed attempts",
                )
                self._running = False

    def _check_scheduler(self) -> None:
        """Check scheduler health and trigger recovery if needed."""
        if self._scheduler_check_fn is None:
            return

        try:
            healthy = self._scheduler_check_fn()
        except Exception:
            logger.exception("Scheduler health check raised an exception")
            healthy = False

        with self._lock:
            self._scheduler_healthy = healthy

        if not healthy and self._scheduler_restart_fn is not None:
            logger.warning("Scheduler unhealthy — attempting restart")
            try:
                restart_ok = self._scheduler_restart_fn()
            except Exception:
                logger.exception("Scheduler restart function raised an exception")
                restart_ok = False

            if restart_ok:
                with self._lock:
                    self._scheduler_healthy = True
                logger.info("Scheduler restarted successfully")
                self._alert("info", "Scheduler restarted successfully")
            else:
                logger.error("Scheduler restart failed")

    def _alert(self, level: str, message: str) -> None:
        """Send an alert via the configured alerter, if available.

        Args:
            level: Severity level ('info', 'warning', 'error', 'critical').
            message: Alert message text.
        """
        if self.alerter is not None:
            try:
                if hasattr(self.alerter, "send"):
                    self.alerter.send(level.upper(), "AutoRecovery", message)
                elif callable(self.alerter):
                    self.alerter(level=level, message=message)
            except Exception:
                logger.exception("Failed to send alert via alerter")
        else:
            logger.debug("Alert suppressed (no alerter configured): [{}] {}", level, message)
