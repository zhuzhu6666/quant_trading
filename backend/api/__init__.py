"""REST API routers. Aggregated by app.include_router(*routers)."""
from fastapi import APIRouter

from backend.api import backtest, health, paper

ALL_ROUTERS: list[APIRouter] = [
    health.router,
    backtest.router,
    paper.router,
]
