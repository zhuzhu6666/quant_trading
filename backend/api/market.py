"""GET /api/market/bars?symbol=&timeframe=&from=&to= — K-line data."""
from typing import Any, Literal

import pandas as pd
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.core.auth import RequireUser
from backend.services.fact_envelope import DEFAULT_STALE_AFTER_SEC, attach_fact
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
) -> BarsResponse:
    payload: dict[str, Any] = {"bars": bars, "total": total, "range": time_range}
    attach_fact(
        payload,
        contract="market.bars.v1",
        source="bars_monthly" if observed_at is not None else "none",
        observed_at=observed_at,
        stale_after_sec=DEFAULT_STALE_AFTER_SEC["market"],
        reason_code=None if observed_at is not None else "market_bars_empty",
    )
    return BarsResponse.model_validate(payload)


@router.get("/bars", response_model=BarsResponse)
def get_bars(
    _user: RequireUser,
    symbol: str = "XAUUSD+",
    timeframe: Literal["M5", "M15", "M30", "H1", "H4", "D1"] = "M15",
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = 5000,
) -> BarsResponse:
    """Fetch K-line bars. If from/to not given, return last `limit` bars.

    audit 2026-06-10: 之前 load_bars 没下推 LIMIT, 每次都拉全表 50K 行 + Python tail,
    切 K 线 tab 阻塞 1-3s. 现改透传 limit/start/end 到 SQL.
    """
    store = _get_store()
    start_iso = pd.Timestamp(from_ts, unit="s").isoformat() if from_ts is not None else None
    end_iso = pd.Timestamp(to_ts, unit="s").isoformat() if to_ts is not None else None
    df = store.load_bars(symbol, timeframe, start=start_iso, end=end_iso, limit=limit)
    if df.empty:
        return _bars_response([], 0, {"from": 0, "to": 0}, None)

    # DataStore returns df with datetime64[s] (SECOND precision, not ns).
    # The unix-seconds value is already what we want for the `t` field.
    # (audit v7-fix-4: v5 audit assumed datetime64[ns] and added an extra
    # `// 1_000_000_000` division, which clobbered every timestamp to 1
    # because integer-dividing a 2026 unix-second by 1e9 gives 1. Verified
    # the real dtype by importing DataStore directly and inspecting
    # df.index.dtype.)
    times = df.index.astype("int64").to_numpy()
    opens = df["open"].to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    volumes = df["volume"].to_numpy() if "volume" in df.columns else None
    spreads = df["spread"].to_numpy() if "spread" in df.columns else None
    n = len(df)
    out_bars: list[Bar] = [None] * n  # type: ignore[list-item]
    for i in range(n):
        out_bars[i] = Bar(
            t=int(times[i]),
            o=float(opens[i]),
            h=float(highs[i]),
            l=float(lows[i]),
            c=float(closes[i]),
            v=float(volumes[i]) if volumes is not None else 0.0,
            spread=float(spreads[i]) if spreads is not None else 0.0,
        )
    return _bars_response(
        out_bars,
        n,
        {"from": int(times[0]) if n else 0, "to": int(times[-1]) if n else 0},
        int(times[-1]) if n else None,
    )
