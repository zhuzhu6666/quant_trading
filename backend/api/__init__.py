"""REST API routers. Aggregated by app.include_router(*routers)."""
from fastapi import APIRouter

from backend.api import (
    ab_test, auth, backtest, calibrator, config, control, discover,
    external_data, factor_health, factor_v4, health, jobs, live, logs,
    market, metrics, paper, reports, risk, ops, experiments, shadow, strategies, sync, tuning,
)

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    auth.router,
    backtest.router,
    paper.router,
    market.router,
    factor_health.router,
    factor_v4.router,
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
    logs.router,
    strategies.router,
    jobs.router,
    metrics.router,
    control.router,
    risk.router,
    ops.router,
    experiments.router,
]
