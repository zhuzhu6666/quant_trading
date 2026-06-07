"""FastAPI app factory + lifespan + CORS + router registration."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import ALL_ROUTERS
from backend.core.logging import setup_logging
from backend.jobs import get_job_manager
from backend.ws.endpoints import router as ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # Bind the running loop so JobManager.submit() works when called from
    # a threadpool thread (e.g. FastAPI sync handler) that has no loop of its own.
    get_job_manager().bind_loop(asyncio.get_running_loop())
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Quant Trading Web Console",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for r in ALL_ROUTERS:
        app.include_router(r)
    app.include_router(ws_router)  # WS routes don't use prefix
    return app


app = create_app()
