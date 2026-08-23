from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

_CTRADER_EPOCH_DATE = date(1970, 1, 1)
_BrokerHoliday = tuple[date, bool, int, int, str, Any]


@dataclass
class MarketSessionState:
    symbol: str
    is_open: bool
    status: str
    reason: str
    now_ts: float
    timezone: str
    quote_age_seconds: float | None = None
    quote_change_age_seconds: float | None = None
    market_data_age_seconds: float | None = None
    schedule_open: bool = False
    confirmation_source: str = ""
    seconds_to_close: float | None = None
    seconds_to_open: float | None = None
    near_close: bool = False
    can_open_positions: bool = False
    can_keep_market_connection: bool = True
    high_load_allowed: bool = False
    high_load_profile: str = "disabled"
    api_available: bool = False
    broker_connected: bool | None = None
    market_closed_confidence: str = "low"
    evidence: list[str] | None = None
    schedule_source: str = "config"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def maintenance_wait_evidence(
    session: dict[str, Any] | None,
    *,
    latest_market_data_ts: float,
    now_ts: float,
    grace_seconds: float,
) -> dict[str, Any]:
    """Classify a bounded scheduled-open/no-quote maintenance wait.

    This deliberately uses the shared market-session evidence instead of a
    fixed wall-clock window. A live API plus no broker error is required, and
    stale data becomes critical again as soon as the configured grace expires.
    """
    payload = dict(session or {})
    status = str(payload.get("status") or "")
    evidence = [str(item) for item in (payload.get("evidence") or [])]
    age = max(0.0, float(now_ts) - float(latest_market_data_ts or 0.0)) if latest_market_data_ts else float("inf")
    grace = max(0.0, float(grace_seconds or 0.0))
    api_healthy = bool(payload.get("api_available")) and payload.get("broker_connected") is not False
    broker_error = "broker_market_closed_error" in evidence or bool(payload.get("broker_error"))
    eligible_statuses = {"open_pending_quote", "broker_connected_market_data_stale"}
    eligible = status in eligible_statuses and api_healthy and not broker_error
    active = eligible and age < grace
    return {
        "eligible": eligible,
        "active": active,
        "status": status,
        "age_seconds": age,
        "grace_seconds": grace,
        "remaining_seconds": max(0.0, grace - age) if eligible else 0.0,
        "api_healthy": api_healthy,
        "evidence": evidence,
    }


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


def _load_session_overrides(symbol: str) -> list[dict[str, Any]]:
    path = Path(DATA_DIR).parent / "config" / "instruments.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        raw = {}
    item = raw.get(symbol) or raw.get(symbol.replace("+", "")) or {}
    rows = item.get("trading_hour_overrides") or item.get("session_overrides") or []
    return [row for row in rows if isinstance(row, dict)]


def _parse_override_dt(value: Any, *, default_date: str = "") -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "T" in text:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc)
        if len(text) == 10 and text.count("-") == 2:
            return datetime.fromisoformat(f"{text}T00:00:00+00:00")
        if ":" in text and default_date:
            return datetime.combine(datetime.fromisoformat(default_date).date(), _parse_hhmm(text), tzinfo=timezone.utc)
    except Exception:
        return None
    return None


def _override_window(now: datetime, overrides: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    next_close: float | None = None
    for row in overrides:
        date_value = str(row.get("date") or row.get("day") or "").strip()
        close_at = _parse_override_dt(row.get("closed_from") or row.get("close_at"), default_date=date_value)
        if close_at is None and date_value and row.get("close"):
            close_at = _parse_override_dt(row.get("close"), default_date=date_value)
        reopen_at = _parse_override_dt(row.get("closed_until") or row.get("reopen_at") or row.get("reopen"), default_date=date_value)
        if close_at is None or reopen_at is None:
            continue
        if reopen_at <= close_at:
            reopen_at += timedelta(days=1)
        if close_at <= now < reopen_at:
            return {
                "active": True,
                "reason": str(row.get("reason") or row.get("name") or "session_override_closed"),
                "seconds_to_open": max(0.0, (reopen_at - now).total_seconds()),
                "seconds_to_close": None,
                "source": "trading_hour_override",
            }
        if now < close_at:
            seconds = max(0.0, (close_at - now).total_seconds())
            next_close = seconds if next_close is None else min(next_close, seconds)
            result = {
                "active": False,
                "reason": str(row.get("reason") or row.get("name") or "session_override_upcoming"),
                "seconds_to_close": next_close,
                "source": "trading_hour_override",
            }
    return result


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


def _resolve_broker_schedule_timezone(value: Any) -> tuple[str, Any | None]:
    name = str(value or "UTC").strip() or "UTC"
    aliases = {
        "GMT": "UTC",
        "UTC": "UTC",
    }
    candidate = aliases.get(name.upper(), name)
    try:
        return name, ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return name, None


def _parse_broker_schedule(
    broker_schedule: dict[str, Any] | None,
) -> tuple[str, Any, list[tuple[int, int]], list[_BrokerHoliday]] | None:
    """Validate and normalize cTrader weekly intervals and holidays."""

    if not isinstance(broker_schedule, dict):
        return None
    intervals: list[tuple[int, int]] = []
    for item in broker_schedule.get("intervals") or []:
        if not isinstance(item, dict):
            continue
        try:
            start_second = int(item.get("start_second"))
            end_second = int(item.get("end_second"))
        except (TypeError, ValueError):
            continue
        if not (0 <= start_second <= 604800 and 0 <= end_second <= 604800):
            continue
        if start_second == end_second:
            continue
        intervals.append((start_second, end_second))
    if not intervals:
        return None
    timezone_name, schedule_tz = _resolve_broker_schedule_timezone(
        broker_schedule.get("timezone")
    )
    if schedule_tz is None:
        return None
    holidays: list[_BrokerHoliday] = []
    for item in broker_schedule.get("holidays") or []:
        if not isinstance(item, dict):
            continue
        try:
            holiday_date = _CTRADER_EPOCH_DATE + timedelta(
                days=int(item.get("holiday_date"))
            )
            start_second = int(item.get("start_second"))
            end_second = int(item.get("end_second"))
        except (OverflowError, TypeError, ValueError):
            continue
        if not (0 <= start_second <= 86400 and 0 <= end_second <= 86400):
            continue
        # The bridge normalizes omitted optional boundaries to 00:00-24:00;
        # tolerate a legacy 0/0 representation as a full-day closure. Other
        # equal non-zero boundaries carry no usable duration.
        if start_second == end_second and start_second != 0:
            continue
        holiday_timezone_name, holiday_tz = _resolve_broker_schedule_timezone(
            item.get("timezone") or timezone_name
        )
        if holiday_tz is None:
            continue
        holidays.append(
            (
                holiday_date,
                bool(item.get("is_recurring", False)),
                start_second,
                end_second,
                holiday_timezone_name,
                holiday_tz,
            )
        )
    return timezone_name, schedule_tz, intervals, holidays


def _holiday_windows_for_range(
    *,
    start_ts: float,
    end_ts: float,
    holidays: list[_BrokerHoliday],
) -> list[tuple[float, float]]:
    """Expand cTrader one-off/recurring holiday closures into UTC windows."""

    if end_ts <= start_ts or not holidays:
        return []
    windows: list[tuple[float, float]] = []
    for base_date, recurring, start_second, end_second, _tz_name, holiday_tz in holidays:
        local_start = datetime.fromtimestamp(
            start_ts - 86400, tz=holiday_tz
        ).date()
        local_end = datetime.fromtimestamp(
            end_ts + 86400, tz=holiday_tz
        ).date()
        if recurring:
            holiday_dates: list[date] = []
            for year in range(local_start.year, local_end.year + 1):
                try:
                    holiday_dates.append(base_date.replace(year=year))
                except ValueError:
                    # A recurring 29-Feb entry has no occurrence in a
                    # non-leap year; cTrader's next leap-year entry remains
                    # the only valid date.
                    continue
        else:
            holiday_dates = [base_date]
        for holiday_date in holiday_dates:
            start_dt = datetime.combine(
                holiday_date,
                dtime.min,
                tzinfo=holiday_tz,
            ) + timedelta(seconds=start_second)
            end_second_offset = end_second
            if end_second_offset <= start_second:
                end_second_offset += 86400
            end_dt = datetime.combine(
                holiday_date,
                dtime.min,
                tzinfo=holiday_tz,
            ) + timedelta(seconds=end_second_offset)
            window_start = start_dt.timestamp()
            window_end = end_dt.timestamp()
            if (
                window_end > start_ts
                and window_start < end_ts
                and window_end > window_start
            ):
                windows.append((window_start, window_end))
    return windows


def _subtract_windows(
    open_windows: list[tuple[float, float]],
    closed_windows: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Remove broker holiday windows from weekly open windows."""

    if not closed_windows:
        return open_windows
    result: list[tuple[float, float]] = []
    for open_start, open_end in open_windows:
        pieces = [(open_start, open_end)]
        for closed_start, closed_end in closed_windows:
            next_pieces: list[tuple[float, float]] = []
            for piece_start, piece_end in pieces:
                if closed_end <= piece_start or closed_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if piece_start < closed_start:
                    next_pieces.append((piece_start, min(piece_end, closed_start)))
                if closed_end < piece_end:
                    next_pieces.append((max(piece_start, closed_end), piece_end))
            pieces = next_pieces
            if not pieces:
                break
        result.extend((start, end) for start, end in pieces if end > start)
    return sorted(result, key=lambda item: item[0])


def _broker_schedule_window(
    now: datetime,
    broker_schedule: dict[str, Any] | None,
) -> tuple[bool, float | None, float | None, str, bool] | None:
    """Calculate the current broker session from ProtoOASymbol intervals.

    cTrader expresses intervals as seconds from Sunday 00:00 in the symbol's
    own timezone.  Keeping this conversion here makes the broker schedule a
    drop-in replacement for the static YAML fallback without creating a
    second session authority.
    """
    parsed = _parse_broker_schedule(broker_schedule)
    if parsed is None:
        return None
    timezone_name, schedule_tz, intervals, holidays = parsed

    now_ts = now.timestamp()
    weekly_windows = _schedule_windows_for_range(
        start_ts=now_ts - 7 * 86400,
        end_ts=now_ts + 21 * 86400,
        schedule_tz=schedule_tz,
        intervals=intervals,
    )
    holiday_windows = _holiday_windows_for_range(
        start_ts=now_ts - 7 * 86400,
        end_ts=now_ts + 21 * 86400,
        holidays=holidays,
    )
    windows = _subtract_windows(weekly_windows, holiday_windows)

    current_close: float | None = None
    next_open: float | None = None
    for start_ts, end_ts in windows:
        if start_ts <= now_ts < end_ts:
            seconds = max(0.0, end_ts - now_ts)
            current_close = (
                seconds
                if current_close is None
                else min(current_close, seconds)
            )
        elif start_ts > now_ts:
            seconds = max(0.0, start_ts - now_ts)
            next_open = (
                seconds
                if next_open is None
                else min(next_open, seconds)
            )
    holiday_active = any(
        window_start <= now_ts < window_end
        for window_start, window_end in holiday_windows
    )
    return (
        current_close is not None,
        current_close,
        next_open,
        timezone_name,
        holiday_active,
    )


def _config_schedule_intervals(symbol: str) -> list[tuple[int, int]]:
    """Convert the static YAML fallback into the same Sunday-based format."""

    intervals: list[tuple[int, int]] = []
    for day, start, end in _load_hours(symbol):
        start_second = ((day + 1) % 7) * 86400 + (
            start.hour * 3600 + start.minute * 60 + start.second
        )
        end_second = ((day + 1) % 7) * 86400 + (
            end.hour * 3600 + end.minute * 60 + end.second
        )
        if end_second <= start_second:
            end_second += 604800
        intervals.append((start_second, end_second))
    return intervals


def _schedule_windows_for_range(
    *,
    start_ts: float,
    end_ts: float,
    schedule_tz: Any,
    intervals: list[tuple[int, int]],
) -> list[tuple[float, float]]:
    """Expand weekly local-time intervals into UTC epoch windows."""

    start_local = datetime.fromtimestamp(start_ts, tz=schedule_tz)
    end_local = datetime.fromtimestamp(end_ts, tz=schedule_tz)
    sunday_date = start_local.date() - timedelta(days=(start_local.weekday() + 1) % 7)
    week_start = datetime.combine(sunday_date, dtime.min, tzinfo=schedule_tz)
    weeks_after_start = max(0, (end_local.date() - sunday_date).days // 7)
    windows: list[tuple[float, float]] = []
    for week_offset in range(-1, weeks_after_start + 2):
        base = week_start + timedelta(days=7 * week_offset)
        for start_second, end_second in intervals:
            end_offset = end_second
            if end_offset <= start_second:
                end_offset += 604800
            start_dt = base + timedelta(seconds=start_second)
            end_dt = base + timedelta(seconds=end_offset)
            windows.append((start_dt.timestamp(), end_dt.timestamp()))
    return windows


def _market_open_seconds_between_with_source(
    start_ts: float,
    end_ts: float,
    *,
    symbol: str = "XAUUSD+",
    broker_schedule: dict[str, Any] | None = None,
) -> tuple[float, str]:
    start = float(start_ts or 0.0)
    end = float(end_ts or 0.0)
    if end <= start:
        return 0.0, "config"

    parsed = _parse_broker_schedule(broker_schedule)
    if parsed is not None:
        _timezone_name, schedule_tz, intervals, holidays = parsed
        source = "ctrader_symbol"
    else:
        schedule_tz = timezone.utc
        intervals = _config_schedule_intervals(symbol)
        source = "config_fallback" if broker_schedule else "config"

    # This matches _schedule_window's behavior for a symbol without a static
    # schedule: no configured hours means no known closure, so preserve the
    # wall-clock duration instead of inventing a weekend.
    if not intervals:
        return end - start, source

    weekly_windows = _schedule_windows_for_range(
        start_ts=start,
        end_ts=end,
        schedule_tz=schedule_tz,
        intervals=intervals,
    )
    holiday_windows = _holiday_windows_for_range(
        start_ts=start - 7 * 86400,
        end_ts=end + 7 * 86400,
        holidays=holidays if parsed is not None else [],
    )
    total = 0.0
    for window_start, window_end in _subtract_windows(weekly_windows, holiday_windows):
        total += max(0.0, min(end, window_end) - max(start, window_start))
    return total, source


def market_open_seconds_between(
    start_ts: float,
    end_ts: float,
    *,
    symbol: str = "XAUUSD+",
    broker_schedule: dict[str, Any] | None = None,
) -> float:
    """Count open-market seconds using cTrader schedule, then YAML fallback."""

    seconds, _source = _market_open_seconds_between_with_source(
        start_ts,
        end_ts,
        symbol=symbol,
        broker_schedule=broker_schedule,
    )
    return seconds


def market_open_seconds_between_with_source(
    start_ts: float,
    end_ts: float,
    *,
    symbol: str = "XAUUSD+",
    broker_schedule: dict[str, Any] | None = None,
) -> tuple[float, str]:
    """Return open seconds plus the schedule authority used for the result."""

    return _market_open_seconds_between_with_source(
        start_ts,
        end_ts,
        symbol=symbol,
        broker_schedule=broker_schedule,
    )


def evaluate_market_session(
    *,
    symbol: str = "XAUUSD+",
    now_ts: float | None = None,
    latest_quote_ts: float | None = None,
    latest_quote_change_ts: float | None = None,
    latest_market_data_ts: float | None = None,
    quote_stale_seconds: float = 300.0,
    market_data_stale_seconds: float = 600.0,
    closed_confirm_seconds: float = 300.0,
    pre_close_block_seconds: float = 1800.0,
    broker_error: str = "",
    has_open_positions: bool = False,
    api_available: bool = False,
    broker_connected: bool | None = None,
    account_api_ok: bool = False,
    positions_api_ok: bool = False,
    broker_schedule: dict[str, Any] | None = None,
) -> MarketSessionState:
    ts = float(now_ts or datetime.now(timezone.utc).timestamp())
    now = datetime.fromtimestamp(ts, tz=timezone.utc)
    hours = _load_hours(symbol)
    overrides = _load_session_overrides(symbol)
    schedule_source = "config"
    schedule_timezone = "UTC"
    holiday_active = False
    broker_window = _broker_schedule_window(now, broker_schedule)
    if broker_window is not None:
        (
            in_schedule,
            seconds_to_close,
            seconds_to_open,
            schedule_timezone,
            holiday_active,
        ) = broker_window
        schedule_source = "ctrader_symbol"
    else:
        in_schedule, seconds_to_close, seconds_to_open = _schedule_window(now, hours)
    override = _override_window(now, overrides)
    override_active = bool(override.get("active", False))
    if override_active:
        in_schedule = False
        seconds_to_close = None
        seconds_to_open = float(override.get("seconds_to_open") or 0.0)
    elif override.get("seconds_to_close") is not None:
        seconds_to_close = min(
            float(seconds_to_close) if seconds_to_close is not None else float(override["seconds_to_close"]),
            float(override["seconds_to_close"]),
        )
    quote_age = None
    if latest_quote_ts and latest_quote_ts > 0:
        quote_age = max(0.0, ts - float(latest_quote_ts))
    quote_change_age = None
    if latest_quote_change_ts and latest_quote_change_ts > 0:
        quote_change_age = max(0.0, ts - float(latest_quote_change_ts))
    market_data_age = None
    if latest_market_data_ts and latest_market_data_ts > 0:
        market_data_age = max(0.0, ts - float(latest_market_data_ts))
    has_quote = bool(latest_quote_ts and latest_quote_ts > 0)
    broker_text = str(broker_error or "").upper()
    broker_closed = any(key in broker_text for key in ("MARKET_CLOSED", "TRADING_MARKET_CLOSED", "OFF_QUOTES", "NO_QUOTES"))
    quote_stale = quote_age is not None and quote_age >= float(quote_stale_seconds)
    quote_motion_stale = quote_change_age is None or quote_change_age >= float(quote_stale_seconds)
    market_data_stale = market_data_age is not None and market_data_age >= float(market_data_stale_seconds)
    close_confirmed_by_quote = quote_age is not None and quote_age >= float(closed_confirm_seconds)
    close_confirmed_by_market_data = (
        market_data_age is not None and market_data_age >= float(closed_confirm_seconds)
    )
    api_alive = bool(api_available or broker_connected or account_api_ok or positions_api_ok)
    evidence = []
    if broker_connected is True:
        evidence.append("broker_connected")
    elif broker_connected is False:
        evidence.append("broker_disconnected")
    if account_api_ok:
        evidence.append("account_api_ok")
    if positions_api_ok:
        evidence.append("positions_api_ok")
    if broker_closed:
        evidence.append("broker_market_closed_error")
    if quote_stale:
        evidence.append("quote_stale")
    if quote_motion_stale and has_quote:
        evidence.append("quote_motion_stale")
    if market_data_stale:
        evidence.append("market_data_stale")
    if override_active:
        evidence.append("trading_hour_override_active")
    if schedule_source == "ctrader_symbol":
        evidence.append("ctrader_symbol_schedule")
        if isinstance(broker_schedule, dict) and broker_schedule.get("holidays"):
            evidence.append("ctrader_symbol_holiday_data")
        if holiday_active:
            evidence.append("ctrader_symbol_holiday_active")
    elif broker_schedule:
        evidence.append("ctrader_schedule_unavailable")
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
            timezone=schedule_timezone,
            schedule_source=schedule_source,
            quote_age_seconds=quote_age,
            quote_change_age_seconds=quote_change_age,
            market_data_age_seconds=market_data_age,
            schedule_open=in_schedule,
            confirmation_source="broker_error",
            seconds_to_close=seconds_to_close,
            seconds_to_open=seconds_to_open,
            near_close=False,
            can_open_positions=False,
            can_keep_market_connection=bool(has_open_positions),
            high_load_allowed=True,
            high_load_profile="limited_with_positions" if has_open_positions else "full",
            api_available=api_alive,
            broker_connected=broker_connected,
            market_closed_confidence="high",
            evidence=evidence,
        )

    if not in_schedule:
        confirmed = close_confirmed_by_quote or close_confirmed_by_market_data or (override_active and api_alive)
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
            reason=(
                str(override.get("reason") or "trading_hour_override_closed")
                if override_active and confirmed
                else "broker_symbol_holiday"
                if holiday_active and confirmed
                else "scheduled_closed"
                if confirmed
                else "broker_symbol_holiday_waiting_confirmation"
                if holiday_active
                else str(override.get("reason") or "scheduled_closed_waiting_confirmation")
            ),
            now_ts=ts,
            timezone=schedule_timezone,
            schedule_source=schedule_source,
            quote_age_seconds=quote_age,
            quote_change_age_seconds=quote_change_age,
            market_data_age_seconds=market_data_age,
            schedule_open=False,
            confirmation_source=(
                "api_alive_after_trading_hour_override"
                if confirmed and override_active and api_alive
                else "quote_stale_after_trading_hour_override"
                if confirmed and override_active
                else "quote_stale_after_schedule_close"
                if close_confirmed_by_quote
                else "market_data_stale_after_schedule_close"
                if confirmed
                else str(override.get("source") or "schedule_only")
            ),
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
            api_available=api_alive,
            broker_connected=broker_connected,
            market_closed_confidence="high" if confirmed and override_active else "medium" if confirmed else "low",
            evidence=evidence,
        )

    if not has_quote:
        return MarketSessionState(
            symbol=symbol,
            is_open=False,
            status="open_pending_quote",
            reason="scheduled_open_waiting_fresh_quote",
            now_ts=ts,
            timezone=schedule_timezone,
            schedule_source=schedule_source,
            quote_age_seconds=quote_age,
            quote_change_age_seconds=quote_change_age,
            market_data_age_seconds=market_data_age,
            schedule_open=True,
            confirmation_source="schedule_only",
            seconds_to_close=seconds_to_close,
            seconds_to_open=seconds_to_open,
            near_close=near_close,
            can_open_positions=False,
            can_keep_market_connection=True,
            high_load_allowed=False,
            api_available=api_alive,
            broker_connected=broker_connected,
            market_closed_confidence="low",
            evidence=evidence,
        )

    if api_alive and market_data_stale:
        evidence.append("api_alive_while_market_data_stale")
        return MarketSessionState(
            symbol=symbol,
            is_open=False,
            status="broker_connected_market_data_stale",
            reason="api_alive_market_data_stale",
            now_ts=ts,
            timezone=schedule_timezone,
            schedule_source=schedule_source,
            quote_age_seconds=quote_age,
            quote_change_age_seconds=quote_change_age,
            market_data_age_seconds=market_data_age,
            schedule_open=True,
            confirmation_source="api_health_and_stale_market_data",
            seconds_to_close=seconds_to_close,
            seconds_to_open=seconds_to_open,
            near_close=near_close,
            can_open_positions=False,
            can_keep_market_connection=True,
            high_load_allowed=False,
            api_available=api_alive,
            broker_connected=broker_connected,
            market_closed_confidence="medium",
            evidence=evidence,
        )

    if quote_stale or (api_alive and quote_motion_stale):
        if api_alive:
            if quote_stale:
                evidence.append("api_alive_while_quote_stale")
                status = "broker_connected_quote_stale"
                reason = "api_alive_quote_stale"
                confirmation_source = "api_health_and_stale_quote"
                confidence = "medium"
            else:
                evidence.append("api_alive_without_quote_motion")
                status = "open_pending_quote_movement"
                reason = "waiting_quote_movement_confirmation"
                confirmation_source = "api_health_waiting_quote_motion"
                confidence = "low"
        else:
            status = "quote_stale"
            reason = "quote_stale"
            confirmation_source = "stale_quote"
            confidence = "low"
        return MarketSessionState(
            symbol=symbol,
            is_open=False,
            status=status,
            reason=reason,
            now_ts=ts,
            timezone=schedule_timezone,
            schedule_source=schedule_source,
            quote_age_seconds=quote_age,
            quote_change_age_seconds=quote_change_age,
            market_data_age_seconds=market_data_age,
            schedule_open=True,
            confirmation_source=confirmation_source,
            seconds_to_close=seconds_to_close,
            seconds_to_open=seconds_to_open,
            near_close=near_close,
            can_open_positions=False,
            can_keep_market_connection=True,
            high_load_allowed=False,
            api_available=api_alive,
            broker_connected=broker_connected,
            market_closed_confidence=confidence,
            evidence=evidence,
        )

    if near_close:
        return MarketSessionState(
            symbol=symbol,
            is_open=True,
            status="pre_close_risk",
            reason="near_scheduled_close",
            now_ts=ts,
            timezone=schedule_timezone,
            schedule_source=schedule_source,
            quote_age_seconds=quote_age,
            quote_change_age_seconds=quote_change_age,
            market_data_age_seconds=market_data_age,
            schedule_open=True,
            confirmation_source="fresh_quote",
            seconds_to_close=seconds_to_close,
            seconds_to_open=seconds_to_open,
            near_close=True,
            can_open_positions=False,
            can_keep_market_connection=True,
            high_load_allowed=False,
            api_available=api_alive,
            broker_connected=broker_connected,
            market_closed_confidence="low",
            evidence=evidence,
        )

    return MarketSessionState(
        symbol=symbol,
        is_open=True,
        status="open_confirmed",
        reason="scheduled_open_fresh_quote",
        now_ts=ts,
        timezone=schedule_timezone,
        schedule_source=schedule_source,
        quote_age_seconds=quote_age,
        quote_change_age_seconds=quote_change_age,
        market_data_age_seconds=market_data_age,
        schedule_open=True,
        confirmation_source="fresh_quote",
        seconds_to_close=seconds_to_close,
        seconds_to_open=seconds_to_open,
        near_close=False,
        can_open_positions=True,
        can_keep_market_connection=True,
        high_load_allowed=False,
        api_available=api_alive,
        broker_connected=broker_connected,
        market_closed_confidence="none",
        evidence=evidence,
    )
