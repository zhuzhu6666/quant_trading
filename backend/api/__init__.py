"""REST API routers. Aggregated by app.include_router(*routers)."""
from fastapi import APIRouter

from backend.api import health

ALL_ROUTERS: list[APIRouter] = [
    health.router,
]
