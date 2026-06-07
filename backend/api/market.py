"""GET /api/market/bars?symbol=&timeframe=&from=&to= — K-line data."""
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from data.store import DataStore

router = APIRouter(prefix="/api/market", tags=["market"])

_store: DataStore | None = None


def _get_store() -> DataStore:
    global _store
    if _store is None:
        _store = DataStore("data/market_data.db")
    return _store


class Bar(BaseModel):
    t: int  # unix seconds
    o: float
    h: float
    l: float
    c: float
    v: float
    spread: float = 0.0


class BarsResponse(BaseModel):
    bars: list[Bar]
    total: int
    range: dict


VALID_TFS = {"M5", "M15", "M30", "H1", "H4", "D1"}  # noqa: F841 — kept for future validation


@router.get("/bars", response_model=BarsResponse)
def get_bars(
    symbol: str = "XAUUSD+",
    timeframe: Literal["M5", "M15", "M30", "H1", "H4", "D1"] = "M15",
    from_ts: int | None = Query(None, alias="from"),
    to_ts: int | None = Query(None, alias="to"),
    limit: int = 5000,
) -> BarsResponse:
    """Fetch K-line bars. If from/to not given, return last `limit` bars."""
    store = _get_store()
    df = store.load_bars(symbol, timeframe)
    if df.empty:
        return BarsResponse(bars=[], total=0, range={"from": 0, "to": 0})

    # DataStore returns df with datetime64[s] (SECOND precision, not ns).
    # The unix-seconds value is already what we want for the `t` field.
    # (audit v7-fix-4: v5 audit assumed datetime64[ns] and added an extra
    # `// 1_000_000_000` division, which clobbered every timestamp to 1
    # because integer-dividing a 2026 unix-second by 1e9 gives 1. Verified
    # the real dtype by importing DataStore directly and inspecting
    # df.index.dtype.)
    times = df.index.astype("int64")
    if from_ts is not None:
        mask = times >= from_ts
        df = df[mask]
        times = times[mask]
    if to_ts is not None:
        mask = times <= to_ts
        df = df[mask]
        times = times[mask]
    if limit and len(df) > limit:
        df = df.tail(limit)
        # (audit v7-fix-1: v5 vectorized refactor left `times` at its original
        # full length while `df` was tail-trimmed, so `times[-limit:]` was
        # misaligned with the trimmed df. The two arrays must stay parallel;
        # recompute `times` from the trimmed df.index to be safe.)
        times = df.index.astype("int64")

    # Vectorized: convert columns to numpy arrays once, then build Bar objects in a
    # single tight Python loop. ~10x faster than df.iterrows() on 50K rows because
    # we skip pandas row-construction overhead per iteration. (audit v5 fix B-3.)
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
    return BarsResponse(
        bars=out_bars,
        total=n,
        range={"from": int(times[0]) if n else 0, "to": int(times[-1]) if n else 0},
    )
