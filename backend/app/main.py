"""FastAPI application factory. Run: ``uv run uvicorn app.main:app``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import core, hedges, wallets
from app.api.ws import WsHub
from app.api.ws import router as ws_router
from app.config import Settings, get_settings
from app.logging_setup import configure_logging
from app.runtime import Runtime

RuntimeFactory = Callable[[Settings], Awaitable[Runtime]]


def create_app(
    cfg: Settings | None = None, runtime_factory: RuntimeFactory | None = None
) -> FastAPI:
    cfg = cfg or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        rt = await (runtime_factory or Runtime.create)(cfg)
        hub = WsHub(rt)
        hub.register_hydrator("hedge", lambda _data: rt.hedger.payload())
        app.state.runtime = rt
        app.state.ws_hub = hub
        hub.start()
        await rt.start()
        try:
            yield
        finally:
            await hub.stop()
            await rt.stop()

    app = FastAPI(title="polycopy", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.include_router(core.router)
    app.include_router(wallets.router)
    app.include_router(hedges.router)
    app.include_router(ws_router)
    return app


app = create_app()
