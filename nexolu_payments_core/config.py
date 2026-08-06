"""Configuracion central del servicio.

Todo lo que varia entre entornos (desarrollo, staging, produccion) vive aqui,
leido de variables de entorno. Las credenciales de cada proveedor (Wompi,
etc.) y el registro de que apps pueden llamar al Core NO viven aca -- eso es
configurable en la base de datos (tablas `integrations`/`provider_credentials`,
ver core/memory/entities.py), a proposito, para no tener que redeployar el
servicio cada vez que se conecta una app nueva o se rota un secreto.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Persistencia
    database_url: str = Field(default="sqlite+aiosqlite:///./nexolu_payments_core.db")

    # Clave Fernet (32 bytes urlsafe base64) usada para cifrar en reposo las
    # credenciales de proveedor guardadas en `provider_credentials`
    # (integrity_secret, events_secret, private_key). Generar con:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Sin esta clave el servicio arranca pero cualquier lectura/escritura de
    # credenciales falla explicitamente (ver core/security/crypto.py).
    payments_master_key: str = ""

    default_currency: str = "COP"

    # Timeout y reintentos al notificar a una app integradora (ver
    # core/webhooks/dispatcher.py). Los reintentos son sincronos dentro del
    # mismo request del webhook entrante -- ver nota de "trabajo futuro" en
    # docs/APP_INTEGRATION.md sobre mover esto a una cola.
    webhook_timeout_seconds: int = 10

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
