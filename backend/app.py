"""FastAPI app factory + lifespan + CORS + router registration.

Dev mode (start.bat): Next.js dev on :3000 proxies /api/* to uvicorn on :8000.
Prod mode (start-prod.bat): static frontend is served by uvicorn on :8000 directly.

CORS: in prod, allow same-origin (the static dir is mounted at /) plus localhost:3000
for dev convenience.
"""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from backend.api import ALL_ROUTERS
from backend.core.logging import setup_logging
from backend.core.paths import BACKEND_DIR
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
    # CORS allow_origins: env-driven (QUANT_CORS_ALLOWED_ORIGINS) so the
    # same image can be used for localhost dev, 192.168.x.x LAN dev, and
    # prod behind a reverse proxy. Format: comma-separated origins, e.g.
    #   QUANT_CORS_ALLOWED_ORIGINS=http://localhost:3000,http://192.168.1.5:3000
    # Default: localhost + 127.0.0.1 on :3000 (the dev-mode Next.js server).
    # (audit 2026-06-08: was hardcoded to the same two values, which broke
    # any LAN access from a phone/tablet for QA.)
    import os as _os
    _cors_env = _os.environ.get("QUANT_CORS_ALLOWED_ORIGINS", "").strip()
    if _cors_env:
        _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
    else:
        _cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for r in ALL_ROUTERS:
        app.include_router(r)
    app.include_router(ws_router)  # WS routes don't use prefix

    # Serve static frontend if built (prod mode).
    # In dev, /static is not mounted; Next.js dev server handles the frontend on :3000.
    static_dir = BACKEND_DIR / "static"
    if static_dir.exists():
        # Mount /_next (Next.js chunks)
        _next = static_dir / "_next"
        if _next.exists():
            app.mount("/_next", StaticFiles(directory=str(_next)), name="next-chunks")

        # Serve static assets. (audit 2026-06-08: previously the code looked
        # for them in static_dir / "static" only, but `next build` with
        # output:export puts the user-placed public/* assets (favicon, PWA
        # icons, etc.) at the root of `out/`, NOT under a "static" subdir.
        # So /icon-192.png and manifest.json would 404 in prod. We mount
        # the whole static_dir at /assets to make them reachable. The SPA
        # fallback below already serves them by path, so /assets is just a
        # belt-and-braces mount for clients that hit a raw path.)
        app.mount("/assets", StaticFiles(directory=str(static_dir)), name="static-assets")

        # SPA fallback: any non-/api / non-/ws GET serves index.html
        # (so /market, /factors, etc. all load index.html which React hydrates)
        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_fallback(full_path: str) -> Response:
            # If the request path matches a real file in static_dir, serve it
            candidate = static_dir / full_path
            if candidate.is_file():
                return FileResponse(str(candidate))
            # Otherwise serve index.html for client-side routing
            index_html = static_dir / "index.html"
            if index_html.exists():
                return FileResponse(str(index_html), media_type="text/html")
            return Response("Frontend not built. Run start-prod.bat first.", status_code=503)

    return app


app = create_app()
