#!/bin/bash
# Deploy de ESTE servicio, independiente de los otros 3. Corre desde el
# droplet, asumiendo la estructura de hermanos de nexolu-infra (ver su
# README.md): este repo y nexolu-infra clonados uno al lado del otro.
set -e
cd "$(dirname "$0")"

echo "==> git pull"
git pull origin main

echo "==> Reconstruyendo y reiniciando payments-core"
cd ../nexolu-infra
docker compose build payments-core

echo "==> Migrando esquema (alembic upgrade head)"
docker compose run --rm payments-core alembic upgrade head

docker compose up -d payments-core

echo "==> Listo. Verificar: curl -s https://payments.nexolu.co/health"
