from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from backend.core.db import DATA_DIR


DAY_ALIASES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass
class MarketSessionState:
    symbol: str
    is_open: bool
    status: str
    reason: str
    now_ts: float
    timezone: str
    quote_age_seconds: float | None = None
    schedule_open: bool = False
    confirmation_source: str = ""
    seconds_to_close: float | None = None
    seconds_to_open: float | None = None
    near_close: bool = False
    can_open_positions: bool = False
    can_keep_market_connection: bool = True
    high_load_allowed: bool = False
    high_load_profile: str = "disabled"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_hhmm(value: str) -> dtime:
    hh, mm = str(value or "00:00").split(":", 1)
    return dtime(hour=int(hh), minute=int(mm), tzinfo=timezone.utc)


def _load_hours(symbol: str) -> list[tuple[int, dtime, dtime]]:
    path = Path(DATA_DIR).parent / "config" / "instruments.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        raw = {}
    item = raw.get(symbol) or raw.get(symbol.replace("+", "")) or {}
    hours = []
    for row in item.get("trading_hours") or []:
        if len(row) != 3:
            continue
        day = DAY_ALIASES.get(str(row[0]).lower())
        if day is None:
            continue
        hours.append((day, _parse_hhmm(row[1]), _parse_hhmm(row[2])))
    return hours


def _schedule_window(
    now: datetime,
    hours: list[tuple[int, dtime, dtime]],
) -> tuple[bool, float | None, float | None]:
    if not hours:
        return True, None, None
    current_close: float | None = None
    next_open: float | None = None
    today = now.date()
    windows: list[tuple[datetime, datetime]] = []
    for offset in range(-1, 8):
        base_date = today + timedelta(days=offset)
        for day, start, end in hours:
            delta_days = (day - base_date.weekday()) % 7
            start_date = base_date + timedelta(days=delta_days)
            start_dt = datetime.combine(start_date, start, tzinfo=timezone.utc)
            end_dt = datetime.combine(start_date, end, tzinfo=timezone.utc)
            if end < start:
                end_dt += timedelta(days=1)
            windows.append((start_dt, end_dt))
    for start_dt, end_dt in sorted(windows, key=lambda item: item[0]):
        if start_dt <= now <= end_dt:
            seconds = max(0.0, (end_dt - now).total_seconds())
            current_close = seconds if current_close is None else min(current_close, seconds)
        elif start_dt > now:
            seconds = max(0.0, (start_dt - now).total_seconds())
            next_open = seconds if next_open is None else min(next_open, seconds)
    return current_close is not None, current_close, next_open


def evaluate_market_session(
    *,
    symbol: str = "XAUUSD+",
    now_ts: float | None = None,
    latest_quote_ts: float | None = None,
    quote_stale_seconds: float = 300.0,
    closed_confirm_seconds: float = 300.0,
    pre_close_block_seconds: float = 1800.0,
    broker_error: str = "",
    has_open_positions: bool = False,
) -> MarketSessionState:
    ts = float(now_ts or datetime.now(timezone.utc).timestamp())
    now = datetime.fromtimestamp(ts, tz=timezone.utc)
    hours = _load_hours(symbol)
    in_schedule, seconds_to_close, seconds_to_open = _schedule_window(now, hours)
    quote_age = None
    if latest_quote_ts and latest_quote_ts > 0:
        quote_age = max(0.0, ts - float(latest_quote_ts))
    has_quote = bool(latest_quote_ts and latest_quote_ts > 0)
    broker_text = str(broker_error or "").upper()
    broker_closed = any(key in broker_text for key in ("MARKET_CLOSED", "TRADING_MARKET_CLOSED", "OFF_QUOTES", "NO_QUOTES"))
    quote_stale = quote_age is not None and quote_age >= float(quote_stale_seconds)
    close_confirmed_by_quote = quote_age is not None and quote_age >= float(closed_confirm_seconds)
    near_close = (
        in_schedule
        and seconds_to_close is not None
        and seconds_to_close <= float(pre_close_block_seconds)
    )

    if broker_closed:
        status = "closed_pending_positions" if has_open_positions else "closed_confirmed"
        return MarketSessionState(
            symbol=symbol,
            is_open=False,
            status=status,
            reason="broker_market_closed",
            now_ts=ts,
            timezone="UTC",
            quote_age_seconds=quote_age,
            schedule_open=in_schedule,
            confirmation_source="broker_error",
            seconds_to_close=seconds_to_close,
            seconds_to_open=seconds_to_open,
            near_close=False,
            can_open_positions=False,
            can_keep_market_connection=bool(has_open_positions),
            high_load_allowed=True,
            high_load_profile="limited_with_positions" if has_open_positions else "full",
        )

    if not in_schedule:
        confirmed = close_confirmed_by_quote
        status = (
            "closed_pending_positions"
            if confirmed and has_open_positions
            else "closed_confirmed"
            if confirmed
            else "closed_pending_confirmation"
        )
        return MarketSessionState(
            symbol=symbol,
            is_open=False,
            status=status,
            reason="scheduled_closed" if confirmed else "scheduled_closed_waiting_confirmation",
            now_ts=ts,
            timezone="UTC",
            quote_age_seconds=quote_age,
            schedule_open=False,
            confirmation_source="quote_stale_after_schedule_close" if confirmed else "schedule_only",
            seconds_to_close=None,
            seconds_to_open=seconds_to_open,
            near_close=False,
            can_open_positions=False,
            can_keep_market_connection=not (confirmed and not has_open_positions),
            high_load_allowed=bool(confirmed),
            high_load_profile=(
                "limited_with_positions"
                if confirmed and has_open_positions
                else "full"
                if confirmed
                else "disabled"
            ),
        )

    if not has_quote:
        return MarketSessionState(
            symbol=symbol,
            is_open=False,
            status="open_pending_quote",
            reason="scheduled_open_waiting_fresh_quote",
            now_ts=ts,
            timezone="UTC",
            quote_age_seconds=quote_age,
            schedule_open=True,
            confirmation_source="schedule_only",
            seconds_to_close=seconds_to_close,
            seconds_to_open=seconds_to_open,
            near_close=near_close,
            can_open_positions=False,
            can_keep_market_connection=True,
            high_load_allowed=False,
        )

    if quote_stale:
        return MarketSessionState(
            symbol=symbol,
            is_open=False,
            status="quote_stale",
            reason="quote_stale",
            now_ts=ts,
            timezone="UTC",
            quote_age_seconds=quote_age,
            schedule_open=True,
            confirmation_source="stale_quote",
            seconds_to_close=seconds_to_close,
            seconds_to_open=seconds_to_open,
            near_close=near_close,
            can_open_positions=False,
            can_keep_market_connection=True,
            high_load_allowed=False,
        )

    if near_close:
        return MarketSessionState(
            symbol=symbol,
            is_open=True,
            status="pre_close_risk",
            reason="near_scheduled_close",
            now_ts=ts,
            timezone="UTC",
            quote_age_seconds=quote_age,
            schedule_open=True,
            confirmation_source="fresh_quote",
            seconds_to_close=seconds_to_close,
            seconds_to_open=seconds_to_open,
            near_close=True,
            can_open_positions=False,
            can_keep_market_connection=True,
            high_load_allowed=False,
        )

    return MarketSessionState(
        symbol=symbol,
        is_open=True,
        status="open_confirmed",
        reason="scheduled_open_fresh_quote",
        now_ts=ts,
        timezone="UTC",
        quote_age_seconds=quote_age,
        schedule_open=True,
        confirmation_source="fresh_quote",
        seconds_to_close=seconds_to_close,
        seconds_to_open=seconds_to_open,
        near_close=False,
        can_open_positions=True,
        can_keep_market_connection=True,
        high_load_allowed=False,
    )
