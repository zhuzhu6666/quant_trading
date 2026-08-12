"""Safety-first startup and bar warmup for the serial live loop."""

from __future__ import annotations

import copy
import traceback
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class StartupSafetyRuntime:
    get_ctrader: Any
    reconcile_positions: Any
    run_safety_cycle: Any
    reconcile_account: Any
    reconcile_value: Any
    live_state_update: Any
    persist_safety_fail_closed: Any


@dataclass(frozen=True)
class BarWarmupRuntime:
    warmup_from_local_db: Any
    get_ctrader: Any
    wait_ctrader_ready: Any
    fetch_bars_with_retry: Any
    load_bar_cache: Any
    publish_latest_price: Any
    save_bar_cache: Any
    logger_warning: Any
    now: Any


@dataclass(frozen=True)
class BarWarmupResult:
    frame: Any
    source: str


def run_startup_safety_cycle(
    *,
    broker: str,
    generation_id: str,
    log: Any,
    runtime: StartupSafetyRuntime,
) -> dict[str, Any]:
    """Run broker snapshot and safety before PG, bars or factor warmup."""

    try:
        bridge, broker_error, warming = runtime.get_ctrader()
        ready = bool(
            bridge is not None
            and not warming
            and getattr(bridge, "is_connected", False)
        )
        positions = runtime.reconcile_positions(bridge if ready else None)
        safety = runtime.run_safety_cycle(
            bridge=bridge if ready else None,
            broker=broker,
            tick=0,
            log=log,
            generation_id=generation_id,
            reconcile_result=positions,
        )
        if ready:
            _publish_startup_account(
                bridge=bridge,
                broker=broker,
                runtime=runtime,
            )
        blockers = tuple(safety.get("blockers") or ())
        if blockers:
            log(
                "startup safety fail-closed: "
                + ",".join(str(item) for item in blockers)
                + (
                    f" broker_error={broker_error}"
                    if broker_error
                    else ""
                )
            )
        return {
            "ok": True,
            "broker_ready": ready,
            "broker_error": str(broker_error or ""),
            "blockers": list(blockers),
            "safety": safety,
        }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        runtime.persist_safety_fail_closed(
            blockers=("startup_safety_cycle_exception",),
            source="live_loop_startup",
            error=error,
        )
        log(
            "startup safety failed closed; main loop will retry in 5s: "
            f"{error}"
        )
        return {
            "ok": False,
            "broker_ready": False,
            "broker_error": error,
            "blockers": ["startup_safety_cycle_exception"],
            "safety": {},
        }


def warmup_live_bars(
    *,
    broker: str,
    timeframe: str,
    log: Any,
    runtime: BarWarmupRuntime,
    symbol: str = "XAUUSD+",
    requested_bars: int = 200,
    minimum_bars: int = 30,
) -> BarWarmupResult | None:
    """Load startup bars from cTrader, monthly fallback, then cache."""

    frame = None
    source = ""
    if broker == "ctrader":
        try:
            bridge, error, warming = runtime.get_ctrader()
            if error:
                log(f"online history unavailable; trying local fallback: {error}")
            else:
                if warming or not bridge.is_connected:
                    wait_error = runtime.wait_ctrader_ready(
                        bridge,
                        timeout_sec=30.0,
                    )
                    if wait_error:
                        log(
                            "online history not ready; trying local fallback: "
                            f"{wait_error}"
                        )
                    else:
                        frame = runtime.fetch_bars_with_retry(
                            bridge,
                            timeframe=timeframe,
                            n_bars=requested_bars,
                        )
                else:
                    frame = runtime.fetch_bars_with_retry(
                        bridge,
                        timeframe=timeframe,
                        n_bars=requested_bars,
                    )
                if _has_minimum_bars(frame, minimum_bars):
                    source = "broker"
        except Exception as exc:
            log(
                "online history warmup failed; trying local fallback: "
                f"{type(exc).__name__}: {exc}"
            )

    if not _has_minimum_bars(frame, minimum_bars):
        if broker != "ctrader":
            log(f"FATAL: unknown broker {broker}")
            return None
        try:
            frame = runtime.warmup_from_local_db(
                symbol,
                timeframe,
                requested_bars,
            )
            if _has_minimum_bars(frame, minimum_bars):
                source = "local_db"
                _warn_if_local_bars_stale(frame, runtime=runtime)
        except Exception as exc:
            log(
                "local history fallback failed: "
                f"{type(exc).__name__}: {exc}\n"
                f"{traceback.format_exc()[-500:]}"
            )

    if not _has_minimum_bars(frame, minimum_bars):
        cache_frame = runtime.load_bar_cache()
        if _has_minimum_bars(cache_frame, minimum_bars):
            frame = cache_frame
            source = "cache"
            log(
                f"WARNING: loaded {len(frame)} bars from backup cache, "
                f"last close={frame['close'].iloc[-1]:.2f}"
            )
        else:
            count = 0 if frame is None else len(frame)
            log(
                f"FATAL: insufficient history bars (got {count} < "
                f"{minimum_bars}) — local DB empty, broker returned 0, "
                "and no backup cache"
            )
            return None

    warmup_price = float(frame["close"].iloc[-1])
    warmup_ts = float(runtime.now())
    try:
        last_index = frame.index[-1]
        if hasattr(last_index, "timestamp"):
            warmup_ts = float(last_index.timestamp())
    except Exception:
        warmup_ts = float(runtime.now())
    runtime.publish_latest_price(
        warmup_price,
        source=f"warmup_{source or 'bar'}",
        ts=warmup_ts,
    )
    log(
        f"warmed up: {len(frame)} bars (source={source}), "
        f"last close={warmup_price:.2f}"
    )
    runtime.save_bar_cache(frame)
    return BarWarmupResult(frame=frame, source=source)


def _publish_startup_account(
    *,
    bridge: Any,
    broker: str,
    runtime: StartupSafetyRuntime,
) -> None:
    account_result = runtime.reconcile_account(bridge)
    if account_result is None:
        return
    raw_account = runtime.reconcile_value(account_result, "account", None)
    if raw_account is None:
        return
    account = (
        asdict(raw_account)
        if is_dataclass(raw_account)
        else dict(raw_account)
    )
    account.update({"ok": True, "broker": broker})
    runtime.live_state_update(
        account=account,
        account_reconciled=copy.deepcopy(account),
        account_updated_at=float(
            runtime.reconcile_value(account_result, "observed_at", 0.0)
            or 0.0
        ),
        account_reconcile_id=str(
            runtime.reconcile_value(account_result, "reconcile_id", "")
            or ""
        ),
        account_reconcile_failed_at=None,
        account_reconcile_error=None,
    )


def _has_minimum_bars(frame: Any, minimum_bars: int) -> bool:
    return frame is not None and len(frame) >= int(minimum_bars)


def _warn_if_local_bars_stale(
    frame: Any,
    *,
    runtime: BarWarmupRuntime,
) -> None:
    last_ts = frame.index[-1]
    age_hours = (
        (
            pd.Timestamp.fromtimestamp(
                float(runtime.now()),
                tz="UTC",
            ).tz_localize(None)
            - last_ts.tz_localize(None)
        ).total_seconds()
        / 3600
        if last_ts.tzinfo
        else 0
    )
    if age_hours > 24:
        runtime.logger_warning(
            f"local DB bars are {age_hours:.1f}h stale "
            f"(last bar: {last_ts}). Strategy will warm up on outdated "
            "data. Consider running live_sync."
        )
