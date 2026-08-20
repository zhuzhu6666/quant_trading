"""GET /api/market/bars?symbol=&timeframe=&from=&to= — K-line data."""
import math
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.core.auth import RequireUser
from backend.services.fact_envelope import DEFAULT_STALE_AFTER_SEC, attach_fact
from backend.services.live_service import _get_live_bars
from data.store import DataStore

router = APIRouter(prefix="/api/market", tags=["market"])

_store: DataStore | None = None


def _get_store() -> DataStore:
    global _store
    if _store is None:
        _store = DataStore()
    return _store


class Bar(BaseModel):
    t: int  # unix seconds
    o: float
    h: float
    l: float
    c: float
    v: float
    spread: float = 0.0


class FactResponse(BaseModel):
    envelope: Literal["fact.v1"]
    contract: str
    state: Literal["known", "unknown", "stale", "error"]
    source: str
    observed_at: float | str | None
    generated_at: float
    stale_after_sec: float
    reason_code: str | None = None
    components: dict[str, Any] = Field(default_factory=dict)


class BarsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    bars: list[Bar]
    total: int
    range: dict
    fact: FactResponse = Field(alias="_fact")


VALID_TFS = {"M5", "M15", "M30", "H1", "H4", "D1"}  # noqa: F841 — kept for future validation


def _bars_response(
    bars: list[Bar],
    total: int,
    time_range: dict[str, int],
    observed_at: int | None,
    *,
    source: str = "bars_monthly",
    empty_reason_code: str = "market_bars_empty",
) -> BarsResponse:
    payload: dict[str, Any] = {"bars": bars, "total": total, "range": time_range}
    attach_fact(
        payload,
        contract="market.bars.v1",
        source=source if observed_at is not None else "none",
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["market"],
        reason_code=None if observed_at is not None else empty_reason_code,
    )
    return BarsResponse.model_validate(payload)


def _frame_to_bars(frame: pd.DataFrame | None) -> list[Bar]:
    """Serialize both durable and live UTC-indexed OHLC frames."""
    if frame is None or frame.empty:
        return []
    required = {"open", "high", "low", "close"}
    if not required.issubset(frame.columns):
        return []

    bars: list[Bar] = []
    for timestamp, row in frame.sort_index().iterrows():
        try:
            values = [float(row[column]) for column in ("open", "high", "low", "close")]
            if not all(math.isfinite(value) for value in values):
                continue
            parsed_timestamp = pd.Timestamp(timestamp)
            if parsed_timestamp.tzinfo is None:
                parsed_timestamp = parsed_timestamp.tz_localize("UTC")
            else:
                parsed_timestamp = parsed_timestamp.tz_convert("UTC")
            epoch = int(parsed_timestamp.timestamp())
            volume = float(row["volume"]) if "volume" in frame.columns and pd.notna(row["volume"]) else 0.0
            spread = float(row["spread"]) if "spread" in frame.columns and pd.notna(row["spread"]) else 0.0
            if not math.isfinite(volume):
                volume = 0.0
            if not math.isfinite(spread):
                spread = 0.0
        except (TypeError, ValueError, OverflowError):
            continue
        bars.append(Bar(t=epoch, o=values[0], h=values[1], l=values[2], c=values[3], v=volume, spread=spread))
    return bars


def _empty_live_bars(reason_code: str) -> BarsResponse:
    return _bars_response(
        [],
        0,
        {"from": 0, "to": 0},
        None,
        source="ctrader_live_trendbar",
        empty_reason_code=reason_code,
    )


def _get_live_bars_response(
    symbol: str,
    timeframe: str,
    from_ts: int | None,
    to_ts: int | None,
    limit: int,
) -> BarsResponse:
    """Project the bridge's in-memory cTrader trendbars without monthly fallback."""
    if symbol != "XAUUSD+":
        return _empty_live_bars("ctrader_live_trendbar_symbol_unavailable")

    # The bridge retains at most 5,000 bars. Pull the whole retained window when
    # a range is requested, then apply the existing HTTP range/limit semantics.
    requested = 5000 if from_ts is not None or to_ts is not None else max(1, min(int(limit), 5000))
    frame = _get_live_bars(symbol=symbol, timeframe=timeframe, n_bars=requested)
    if frame is None or frame.empty:
        return _empty_live_bars("ctrader_live_trendbar_unavailable")

    frame = frame.copy()
    frame.index = pd.to_datetime(frame.index, utc=True)
    frame = frame.sort_index()
    if from_ts is not None:
        frame = frame.loc[frame.index >= pd.to_datetime(from_ts, unit="s", utc=True)]
    if to_ts is not None:
        frame = frame.loc[frame.index <= pd.to_datetime(to_ts, unit="s", utc=True)]
    frame = frame.tail(max(1, int(limit)))
    bars = _frame_to_bars(frame)
    if not bars:
        return _empty_live_bars("ctrader_live_trendbar_unavailable")
    return _bars_response(
        bars,
        len(bars),
        {"from": bars[0].t, "to": bars[-1].t},
        bars[-1].t,
        source="ctrader_live_trendbar",
    )


@router.get("/bars", response_model=BarsResponse)
def get_bars(
    _user: RequireUser,
    symbol: str = "XAUUSD+",
    timeframe: Literal["M5", "M15", "M30", "H1", "H4", "D1"] = "M15",
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = 5000,
    source: Literal["monthly", "live"] = "monthly",
) -> BarsResponse:
    """Fetch K-line bars. If from/to not given, return last `limit` bars.

    audit 2026-06-10: 之前 load_bars 没下推 LIMIT, 每次都拉全表 50K 行 + Python tail,
    切 K 线 tab 阻塞 1-3s. 现改透传 limit/start/end 到 SQL.
    """
    if source == "live":
        return _get_live_bars_response(symbol, timeframe, from_ts, to_ts, limit)

    store = _get_store()
    start_iso = pd.Timestamp(from_ts, unit="s").isoformat() if from_ts is not None else None
    end_iso = pd.Timestamp(to_ts, unit="s").isoformat() if to_ts is not None else None
    df = store.load_bars(symbol, timeframe, start=start_iso, end=end_iso, limit=limit)
    if df.empty:
        return _bars_response([], 0, {"from": 0, "to": 0}, None)
    out_bars = _frame_to_bars(df)
    if not out_bars:
        return _bars_response([], 0, {"from": 0, "to": 0}, None)
    n = len(out_bars)
    return _bars_response(
        out_bars,
        n,
        {"from": out_bars[0].t, "to": out_bars[-1].t},
        out_bars[-1].t,
    )
