"""REST API routers. Aggregated by app.include_router(*routers)."""
from fastapi import APIRouter

from backend.api import (
    ab_test, auth, backtest, calibrator, config, discover, factor_health, health, jobs, live, market, paper, reports, shadow, sync, tuning,
)

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    auth.router,
    backtest.router,
    paper.router,
    market.router,
    factor_health.router,
    sync.router,
    discover.router,
    tuning.router,
    calibrator.router,
    shadow.router,
    ab_test.router,
    reports.router,
    config.router,
    live.router,
    jobs.router,
]
