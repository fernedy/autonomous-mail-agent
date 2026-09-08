# Etapa 1: Builder
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /build/wheels -r requirements.txt

# Etapa 2: Runtime (Ultraligero, sin Playwright)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Seguridad por Diseño: Usuario explícito sin privilegios
RUN useradd -m jarvisuser

# Inyectamos solo los precompilados
COPY --from=builder /build/wheels /wheels
RUN pip install --no-cache /wheels/* && rm -rf /wheels

# Copiamos código fuente
COPY src/ ./src/

# Asignamos propiedad y bajamos privilegios
RUN chown -R jarvisuser:jarvisuser /app
USER jarvisuser

CMD ["python", "-m", "src.brain.brain_worker"]