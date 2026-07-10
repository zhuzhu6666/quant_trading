"""Liveness + db connectivity check."""
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from backend.core.db import connect_duckdb
from backend.core.paths import DB_PATH

router = APIRouter(prefix="/api", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    db: str
    ctrader: str
    server_time: str
    uptime_seconds: float


_START_TIME = time.time()


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    db_status = "connected"
    try:
        conn = connect_duckdb(DB_PATH, read_only=True)
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception as e:
        # DuckDB allows one writer process. During live collection a healthy DB
        # can be temporarily unavailable to this liveness probe, so don't mark
        # the whole service degraded just because an active writer owns the lock.
        if type(e).__name__ == "ConnectionException" and DB_PATH.exists():
            db_status = "locked_by_writer"
        else:
            db_status = f"error: {type(e).__name__}"

    ctrader_status = "unknown"
    try:
        from backend.services.runtime_health_projection import RuntimeHealthProjectionService

        projection = RuntimeHealthProjectionService().latest(max_age_seconds=180.0)
        if projection.get("ok"):
            ctrader_status = str((projection.get("ctrader") or {}).get("status") or "unknown")
    except Exception:
        ctrader_status = "unknown"

    return HealthResponse(
        status="ok" if db_status in {"connected", "locked_by_writer"} else "degraded",
        db=db_status,
        ctrader=ctrader_status,
        server_time=datetime.now(timezone.utc).isoformat(),
        uptime_seconds=time.time() - _START_TIME,
    )

