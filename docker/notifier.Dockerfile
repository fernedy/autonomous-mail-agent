# [docker/notifier.Dockerfile]
FROM python:3.11-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /build/wheels -r requirements.txt

FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
RUN useradd -m jarvisuser
COPY --from=builder /build/wheels /wheels
RUN pip install --no-cache /wheels/* && rm -rf /wheels

# Inyectamos el código
COPY src/ ./src/

RUN chown -R jarvisuser:jarvisuser /app
USER jarvisuser

# Ejecutamos el módulo
CMD ["python", "-m", "notifier.notifier_worker"]