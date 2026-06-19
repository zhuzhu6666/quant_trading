"""Liveness + db connectivity check."""
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

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
        import duckdb
        conn = duckdb.connect(str(DB_PATH))
        conn.execute("SELECT 1").fetchone()
        conn.close()
    except Exception as e:
        db_status = f"error: {type(e).__name__}"

    return HealthResponse(
        status="ok" if db_status == "connected" else "degraded",
        db=db_status,
        ctrader="unknown",
        server_time=datetime.now(timezone.utc).isoformat(),
        uptime_seconds=time.time() - _START_TIME,
    )
