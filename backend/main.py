"""
FastAPI application.

Serves the built React dashboard and the API on one local port. The server binds
to 127.0.0.1 by default (see ``run.py``) so nothing on your network — or the
internet — can reach an endpoint that controls a funded wallet.
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import router as api_router
from .state import AppState

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def create_app() -> FastAPI:
    state = AppState()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(
        title="Polymarket Copy Trading Bot",
        description="Local web dashboard for the Polymarket copy trading bot.",
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.app = state

    # The dashboard is served from this same origin in normal use. The allowed
    # origins below exist only so `npm run dev` (Vite on :5173) can talk to the
    # API during development — no wildcard, because these routes move money.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:8765", "http://127.0.0.1:8765",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        with contextlib.suppress(Exception):
            state.log.error(f"Server error on {request.url.path}: {exc}")
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}"},
        )

    app.include_router(api_router, prefix="/api")

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "clients": state.hub.client_count,
                "engine": state.status()["state"]}

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built dashboard, with SPA fallback for client-side routes."""
    if not FRONTEND_DIST.exists():
        @app.get("/")
        async def _missing() -> JSONResponse:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": "The dashboard has not been built yet. Run "
                              "`npm install && npm run build` in ./frontend, or "
                              "start the app with run.py which does it for you."
                },
            )
        return

    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = FRONTEND_DIST / "index.html"

    @app.get("/{path:path}")
    async def spa(path: str) -> FileResponse:
        candidate = (FRONTEND_DIST / path).resolve()
        # Serve real files (favicon, manifest…) but never escape the dist dir.
        if (path and candidate.is_file()
                and candidate.is_relative_to(FRONTEND_DIST.resolve())):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
