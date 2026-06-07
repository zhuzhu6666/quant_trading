"""REST API routers. Aggregated by app.include_router(*routers)."""
from fastapi import APIRouter

from backend.api import backtest, factor_health, health, market, paper

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    backtest.router,
    paper.router,
    market.router,
    factor_health.router,
]
