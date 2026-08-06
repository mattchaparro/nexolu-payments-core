"""Webhooks ENTRANTES: los que el proveedor (Wompi) le manda al Core.

No llevan `Authorization`: Wompi no sabe de nuestras API keys, autentica con
su propia firma (verificada en `service.handle_provider_webhook`). La ruta
lleva el slug de la integracion porque cada una puede tener su propio
merchant account de Wompi (su propio `events_secret`) -- asi que en el
dashboard de Wompi de cada integracion se configura una URL distinta,
apuntando aca.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.memory import repository
from nexolu_payments_core.core.memory.db import get_session
from nexolu_payments_core.core.payments import service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("/wompi/{integration_slug}")
async def wompi_webhook(
    integration_slug: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    integration = await repository.get_integration_by_slug(session, integration_slug)
    if integration is None:
        # 401 en vez de 404: no le damos a un tercero una forma de
        # distinguir "el slug no existe" de "la firma es invalida".
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Integracion no encontrada.")

    payload = await request.json()

    try:
        transaction = await service.handle_provider_webhook(
            session, integration=integration, provider_slug="wompi", payload=payload
        )
    except PermissionError:
        logger.warning("wompi.webhook.invalid_signature", extra={"integration": integration_slug})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Firma invalida.") from None

    if transaction is not None:
        logger.info(
            "wompi.webhook.processed",
            extra={
                "integration": integration_slug,
                "reference": transaction.reference,
                "status": transaction.status,
            },
        )

    return {"ok": True}
