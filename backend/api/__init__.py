"""REST API routers. Aggregated by app.include_router(*routers)."""
from fastapi import APIRouter

from backend.api import (
    backtest, calibrator, discover, factor_health, health, market, paper, sync, tuning,
)

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    backtest.router,
    paper.router,
    market.router,
    factor_health.router,
    sync.router,
    discover.router,
    tuning.router,
    calibrator.router,
]
