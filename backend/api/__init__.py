"""REST API routers. Aggregated by app.include_router(*routers)."""
from fastapi import APIRouter

from backend.api import (
    ab_test, auth, backtest, calibrator, config, control, discover,
    external_data, factor_health, health, jobs, live, market, metrics,
    paper, reports, shadow, strategies, sync, tuning,
)

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    auth.router,
    backtest.router,
    paper.router,
    market.router,
    factor_health.router,
    external_data.router,
    sync.router,
    discover.router,
    tuning.router,
    calibrator.router,
    shadow.router,
    ab_test.router,
    reports.router,
    config.router,
    live.router,
    strategies.router,
    jobs.router,
    metrics.router,
    control.router,
]
