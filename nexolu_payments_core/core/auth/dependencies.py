"""Dependencia de FastAPI para autenticar aplicaciones cliente.

El Core no tiene sesion de usuario final: su unico sujeto autenticado es la
APLICACION que llama (pos-saas-legacy, nexolu-ia-core, futuras), via API key
en el header `Authorization`. La identidad se resuelve contra la tabla
`integrations` (configurable en BD, ver `scripts/register_integration.py`),
no contra una variable de entorno -- a diferencia de nexolu-ia-core, donde el
registro de apps es un JSON en `.env`. Aca el numero de integraciones y sus
credenciales de proveedor cambian con mas frecuencia y no deben requerir un
redeploy.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from nexolu_payments_core.core.memory import repository
from nexolu_payments_core.core.memory.db import get_session
from nexolu_payments_core.core.memory.entities import Integration


async def get_current_integration(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Integration:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Falta el header Authorization.")

    api_key = authorization.split(" ", 1)[1].strip()
    integration = await repository.get_integration_by_api_key(session, api_key)

    if integration is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key invalida.")

    return integration
