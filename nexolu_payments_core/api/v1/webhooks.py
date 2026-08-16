"""Incoming provider webhooks.

Wompi uses one central endpoint. The transaction reference is the routing key;
Payments Core resolves the transaction first, then obtains the merchant's
provider credentials and the target integration from the transaction context.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.memory.db import get_session
from nexolu_payments_core.core.payments import service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post("/wompi")
async def wompi_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    payload = await request.json()
    try:
        transaction = await service.handle_provider_webhook(session, provider_slug="wompi", payload=payload)
    except PermissionError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Firma invalida.") from None

    if transaction is not None:
        logger.info("wompi.webhook.processed", extra={"reference": transaction.reference, "status": transaction.status})
    return {"ok": True}
