# FastAPI + Uvicorn -- mismo patron que nexolu-ia-core y nexolu-comms-api.
#
# Sin gcc a proposito: todas las dependencias de pyproject.toml (incluida
# cryptography, la unica de las 3 apps que tiene componentes en Rust) bajan
# como wheel precompilado para linux x86_64 - nada se compila desde
# source. Instalar gcc agregaba ~240MB sin necesidad, verificado en vivo
# el 2026-08-20 (build identico con/sin gcc, mismo resultado, imagen final
# 536MB -> 293MB en ia-core).
FROM python:3.12-slim

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
