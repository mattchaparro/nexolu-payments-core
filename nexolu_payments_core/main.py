"""Punto de entrada del servicio: `uvicorn nexolu_payments_core.main:app`."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from nexolu_payments_core.api.v1 import health, payments, webhooks
from nexolu_payments_core.config import get_settings
from nexolu_payments_core.core.memory.db import init_models
from nexolu_payments_core.core.telemetry.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    # Autocrear tablas solo tiene sentido en SQLite de desarrollo. En
    # produccion (Postgres) el esquema se maneja con `alembic upgrade head`,
    # corrido como parte del despliegue, no al arrancar el proceso.
    if settings.database_url.startswith("sqlite"):
        await init_models()

    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Nexolu Payments Core",
        description=(
            "Pasarela de pagos unificada del ecosistema Nexolu: recibe pagos via "
            "Wompi y notifica a cualquier app integrada, agnostica de cual sea."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    app.include_router(health.router)
    app.include_router(payments.router)
    app.include_router(webhooks.router)

    return app


app = create_app()
