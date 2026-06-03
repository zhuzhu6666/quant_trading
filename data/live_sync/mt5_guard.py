"""
data/live_sync/mt5_guard.py -- MT5 terminal64 process guard

Design goals:
- Prevent the "no terminal64 -> mt5.initialize() auto-spawns a hidden one" trap
  (the one that produced a hidden instance + the GUI terminal on 2026-06-03).
- Reject running when multiple terminal64 processes are alive.
- Heartbeat the MT5 connection so dead handles get reaped and the daemon
  re-acquires a fresh one instead of silently hanging on copy_rates_from_pos.

Public API:
    check_one(...) -> list[int]
        Verify exactly one terminal64.exe is running. If 0, poll until found
        (or max_wait_sec expires). If >1, raise RuntimeError.
    ping(mt5_module, symbol=...) -> bool
        Cheap heartbeat: ask MT5 for the latest tick of a symbol. Returns
        False on None, on a zero epoch (dead handle), or on any exception.
    shutdown_safely(mt5_module)
        Best-effort mt5.shutdown(); swallows exceptions (used during reconnect).
    reconnect(mt5_module, ...) -> int
        Full reconnect cycle: shutdown -> wait for one -> initialize -> ping.
"""
from __future__ import annotations

import logging
import subprocess
import time

logger = logging.getLogger(__name__)


_POWERSHELL_LIST_PIDS = (
    "@(Get-Process terminal64 -ErrorAction SilentlyContinue | "
    "ForEach-Object { $_.Id }) -join \"`n\""
)


def _list_terminal64_pids() -> list[int]:
    """Return the list of terminal64.exe PIDs currently running. Empty if none."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _POWERSHELL_LIST_PIDS],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        logger.warning("[MT5Guard] powershell timed out while listing terminal64")
        return []
    except Exception as e:
        logger.warning(f"[MT5Guard] powershell invocation failed: {e}")
        return []

    out = (r.stdout or "").strip()
    if not out:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def count_terminal64() -> int:
    """Just the count, no logging."""
    return len(_list_terminal64_pids())


def check_one(poll_sec: float = 5.0, max_wait_sec: int = 0) -> list[int]:
    """
    Enforce "exactly one terminal64 is running".

    - 0 instances: log and poll every poll_sec seconds until one appears,
      or max_wait_sec elapses. max_wait_sec=0 means wait forever.
    - 1 instance: log PID and return [pid].
    - >1 instances: raise RuntimeError. Never pick a winner or kill one --
      that's the operator's call. This is the case that produced the
      auto-spawned hidden instance + the manual GUI terminal on 2026-06-03.

    Returns the single PID on success.
    """
    waited = 0.0
    while True:
        pids = _list_terminal64_pids()
        n = len(pids)
        if n == 1:
            logger.info(f"[MT5Guard] exactly 1 terminal64 running, PID={pids[0]}")
            return pids
        if n > 1:
            raise RuntimeError(
                f"[MT5Guard] detected {n} terminal64.exe processes "
                f"(PIDs={pids}). Refusing to start: keep only one MT5 terminal "
                f"open at a time. Close the others and restart the daemon."
            )
        if max_wait_sec > 0 and waited >= max_wait_sec:
            raise RuntimeError(
                f"[MT5Guard] no terminal64.exe found after waiting "
                f"{max_wait_sec}s. Please open MetaTrader 5, log in, "
                f"and start the daemon again."
            )
        logger.info(
            f"[MT5Guard] no terminal64 running; polling every "
            f"{poll_sec:.0f}s (waited {waited:.0f}s / max {max_wait_sec}s)"
        )
        time.sleep(poll_sec)
        waited += poll_sec


def ping(mt5_module, symbol: str = "XAUUSD+") -> bool:
    """
    Cheap liveness probe for the MT5 IPC handle.

    Returns True only if we get a tick with a non-zero epoch timestamp.
    Returns False on:
      - symbol_info_tick returning None
      - tick.time == 0 (stale / dead handle)
      - any exception from the C extension
    """
    if mt5_module is None:
        return False
    try:
        tick = mt5_module.symbol_info_tick(symbol)
    except Exception as e:
        logger.warning(f"[MT5Guard] ping raised: {type(e).__name__}: {e}")
        return False
    if tick is None:
        return False
    return bool(getattr(tick, "time", 0) > 0)


def shutdown_safely(mt5_module) -> None:
    """mt5.shutdown() that swallows everything; used before re-initialize."""
    if mt5_module is None:
        return
    try:
        mt5_module.shutdown()
    except Exception as e:
        logger.debug(f"[MT5Guard] shutdown raised (ignored): {e}")


def reconnect(mt5_module, poll_sec: float = 5.0, max_wait_sec: int = 300) -> int:
    """
    Full reconnect cycle:
      1. shutdown the current (possibly-dead) handle
      2. wait until exactly 1 terminal64 exists
      3. mt5.initialize() to acquire a fresh handle
      4. verify with a ping

    Returns the terminal64 PID on success. Raises on failure.
    """
    logger.warning("[MT5Guard] reconnecting to MT5 (current handle stale)")
    shutdown_safely(mt5_module)
    pids = check_one(poll_sec=poll_sec, max_wait_sec=max_wait_sec)
    if not mt5_module.initialize():
        err = mt5_module.last_error()
        raise RuntimeError(f"[MT5Guard] mt5.initialize() failed: {err}")
    if not ping(mt5_module):
        raise RuntimeError("[MT5Guard] post-initialize ping returned False")
    logger.info(f"[MT5Guard] reconnected, terminal64 PID={pids[0]}")
    return pids[0]
