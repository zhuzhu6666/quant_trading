from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import Any, Callable


class CTraderRuntime:
    """Non-blocking cTrader connection owner.

    The live service keeps the public facade. This class owns the mutable
    connection state so the large live module can shed one responsibility at a
    time without changing trading behavior.
    """

    def __init__(self, *, lock_path: Path) -> None:
        self.lock_path = Path(lock_path)
        self.bridge: Any = None
        self.lock = threading.Lock()
        self.connect_thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.next_retry_at: float = 0.0
        self.guard_handle: Any = None

    def ensure_process_guard(self) -> str | None:
        if self.guard_handle is not None:
            return None
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            try:
                import fcntl  # type: ignore

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except ImportError:
                import msvcrt  # type: ignore

                fh.seek(0)
                if self.lock_path.stat().st_size == 0:
                    fh.write("0")
                    fh.flush()
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            fh.seek(0)
            fh.truncate()
            fh.write(f"pid={os.getpid()} started_at={time.time():.3f}\n")
            fh.flush()
            self.guard_handle = fh
            return None
        except OSError as exc:
            fh.close()
            return f"ctrader session lock is held by another backend process: {exc}"

    def retry_remaining(self) -> float:
        remaining = self.next_retry_at - time.time()
        return remaining if remaining > 0 else 0.0

    @staticmethod
    def _connect_backoff_seconds(bridge: Any) -> float:
        try:
            return max(float(bridge.connect_backoff_seconds()), 1.0)
        except Exception:
            return 5.0

    def kickoff_connect(self, *, logger: Any) -> threading.Thread:
        bridge = self.bridge

        def _bg() -> None:
            try:
                ok = bridge.connect()
                if not ok:
                    retry_in = self._connect_backoff_seconds(bridge)
                    self.next_retry_at = time.time() + retry_in
                    self.last_error = "cTrader connect failed (check credentials / network)"
                    logger.warning(
                        "[ctrader] background connect failed: {}; retry in {:.1f}s",
                        self.last_error,
                        retry_in,
                    )
                    return
                self.next_retry_at = 0.0
                self.last_error = None
                try:
                    if hasattr(bridge, "refresh_account_info"):
                        bridge.refresh_account_info()
                    else:
                        bridge.account_info()
                except Exception as exc:
                    logger.debug("[ctrader] initial account prime failed: %s", exc)
                try:
                    if hasattr(bridge, "refresh_positions"):
                        bridge.refresh_positions()
                    else:
                        bridge.get_positions()
                except Exception as exc:
                    logger.debug("[ctrader] initial positions prime failed: %s", exc)
                logger.info("[ctrader] background connect OK")
            except Exception as exc:
                retry_in = self._connect_backoff_seconds(bridge)
                self.next_retry_at = time.time() + retry_in
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                logger.warning(
                    "[ctrader] background connect exception: {}; retry in {:.1f}s",
                    self.last_error,
                    retry_in,
                )

        thread = threading.Thread(target=_bg, daemon=True, name="ctrader-bg-connect")
        thread.start()
        self.connect_thread = thread
        return thread

    def get_or_start(
        self,
        *,
        make_bridge: Callable[..., tuple[Any, str | None]],
        should_send_orders: Callable[[str], bool],
        apply_runtime_config: Callable[[Any], None],
        logger: Any,
    ) -> tuple[Any, str | None, bool]:
        with self.lock:
            guard_err = self.ensure_process_guard()
            if guard_err:
                return None, guard_err, False

            if self.bridge is not None:
                self.bridge.send_orders = should_send_orders("ctrader")
                apply_runtime_config(self.bridge)
                if self.bridge.is_connected:
                    return self.bridge, None, False
                if getattr(self.bridge, "is_connecting", False):
                    return self.bridge, None, True
                if self.retry_remaining() > 0:
                    return self.bridge, None, True
                if self.connect_thread is None or not self.connect_thread.is_alive():
                    self.kickoff_connect(logger=logger)
                return self.bridge, None, True

            try:
                bridge, build_err = make_bridge(send_orders=should_send_orders("ctrader"))
                if build_err:
                    self.bridge = None
                    return None, build_err, False
                self.bridge = bridge
            except Exception as exc:
                self.bridge = None
                return None, f"{type(exc).__name__}: {exc}"[:300], False

            if not self.bridge.has_token():
                self.bridge = None
                return None, "no cTrader credentials in .env (CTRADER_CLIENT_ID/SECRET/ACCESS_TOKEN/ACCOUNT_ID)", False
            apply_runtime_config(self.bridge)
            self.kickoff_connect(logger=logger)
            return self.bridge, None, True
