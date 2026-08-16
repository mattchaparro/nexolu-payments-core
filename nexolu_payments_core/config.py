"""Central service configuration.

Provider credentials and application registrations live in the database.
Environment variables contain only service-wide secrets/configuration.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(default="sqlite+aiosqlite:///./nexolu_payments_core.db")
    payments_master_key: str = ""
    provisioning_key: str = ""
    default_currency: str = "COP"
    webhook_timeout_seconds: int = 10
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
