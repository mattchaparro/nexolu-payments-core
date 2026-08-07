# FastAPI + Uvicorn -- mismo patron que nexolu-ia-core y nexolu-comms-api.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY nexolu_payments_core ./nexolu_payments_core
COPY alembic ./alembic
COPY alembic.ini ./
COPY scripts ./scripts

RUN pip install --no-cache-dir .

EXPOSE 8000
# El esquema se maneja con `alembic upgrade head` como paso de deploy (ver
# deploy/README.md en nexolu-infra) -- este contenedor NUNCA migra solo al
# arrancar.
CMD ["uvicorn", "nexolu_payments_core.main:app", "--host", "0.0.0.0", "--port", "8000"]
