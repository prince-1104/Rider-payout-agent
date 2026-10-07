"""FastAPI application entry point."""

import logging

from fastapi import FastAPI

from app.api import health, messages, ops, trace
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    app = FastAPI(title=settings.app_name, version="0.1.0")
    app.include_router(health.router)
    app.include_router(messages.router)
    app.include_router(trace.router)
    app.include_router(ops.router)
    return app


app = create_app()
