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

    # DataStore returns df with DatetimeIndex; convert to unix seconds
    times = (df.index.astype("int64") // 1_000_000_000).astype("int64")
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
        times = times[-limit:]

    bars = [
        Bar(
            t=int(times[i]),
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
            v=float(row.get("volume", 0)),
            spread=float(row.get("spread", 0)),
        )
        for i, (_, row) in enumerate(df.iterrows())
    ]
    return BarsResponse(
        bars=bars,
        total=len(bars),
        range={"from": bars[0].t if bars else 0, "to": bars[-1].t if bars else 0},
    )
