"""CLI para dar de alta o actualizar una integracion (app cliente) y sus
credenciales de Wompi directamente en la base de datos.

No hay panel de administracion todavia -- esto es lo minimo para que
conectar una app nueva sea "configurable en BD" sin escribir SQL a mano.
Correr de nuevo con el mismo --slug actualiza la integracion existente (y
crea una fila nueva de FeeSchedule solo si no habia ninguna activa).

Uso:
    python -m scripts.register_integration \\
        --slug pos-legacy --name "Nexolu POS" \\
        --api-key <api-key-para-pos-legacy> \\
        --webhook-url https://pos.nexolu.co/integrations/payments-core/webhook \\
        --webhook-secret <secreto-generado> \\
        --wompi-public-key pub_prod_xxx --wompi-private-key prv_prod_xxx \\
        --wompi-integrity-secret xxx --wompi-events-secret xxx \\
        --environment production

Generar api-key/webhook-secret con algo como:
    python -c "import secrets; print(secrets.token_urlsafe(32))"
"""
from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from nexolu_payments_core.core.memory.db import get_sessionmaker, init_models
from nexolu_payments_core.core.memory.entities import FeeSchedule, Integration, ProviderCredential


async def _run(args: argparse.Namespace) -> None:
    await init_models()

    async with get_sessionmaker()() as session:
        integration = (
            await session.execute(select(Integration).where(Integration.slug == args.slug))
        ).scalar_one_or_none()

        if integration is None:
            integration = Integration(slug=args.slug)
            session.add(integration)

        integration.name = args.name
        integration.environment = args.environment
        integration.api_key = args.api_key
        integration.webhook_url = args.webhook_url
        integration.webhook_secret = args.webhook_secret
        integration.is_active = True
        await session.flush()

        credential = (
            await session.execute(
                select(ProviderCredential).where(
                    ProviderCredential.integration_id == integration.id,
                    ProviderCredential.provider_slug == "wompi",
                    ProviderCredential.environment == args.environment,
                )
            )
        ).scalar_one_or_none()

        if credential is None:
            credential = ProviderCredential(
                integration_id=integration.id, provider_slug="wompi", environment=args.environment
            )
            session.add(credential)

        credential.public_key = args.wompi_public_key
        credential.private_key = args.wompi_private_key
        credential.integrity_secret = args.wompi_integrity_secret
        credential.events_secret = args.wompi_events_secret
        credential.is_active = True

        fee_schedule = (
            await session.execute(
                select(FeeSchedule).where(
                    FeeSchedule.integration_id == integration.id,
                    FeeSchedule.provider_slug == "wompi",
                    FeeSchedule.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()

        if fee_schedule is None:
            session.add(
                FeeSchedule(
                    integration_id=integration.id,
                    provider_slug="wompi",
                    percent_fee=args.percent_fee,
                    fixed_fee_cop=args.fixed_fee_cop,
                    iva_percent=args.iva_percent,
                )
            )

        await session.commit()

    print(f"Integracion '{args.slug}' lista.")
    print(f"Webhook de Wompi para configurar en su dashboard: /v1/webhooks/wompi/{args.slug}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--api-key", required=True, help="La app llama al Core con Authorization: Bearer <api-key>")
    parser.add_argument("--webhook-url", required=True, help="A donde el Core notifica los cambios de estado")
    parser.add_argument("--webhook-secret", required=True, help="Para que la app verifique X-Nexolu-Signature")
    parser.add_argument("--environment", default="sandbox", choices=["sandbox", "production"])
    parser.add_argument("--wompi-public-key", required=True)
    parser.add_argument("--wompi-private-key", required=True)
    parser.add_argument("--wompi-integrity-secret", required=True)
    parser.add_argument("--wompi-events-secret", required=True)
    parser.add_argument("--percent-fee", type=float, default=2.65)
    parser.add_argument("--fixed-fee-cop", type=int, default=700)
    parser.add_argument("--iva-percent", type=float, default=19.0)
    args = parser.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
