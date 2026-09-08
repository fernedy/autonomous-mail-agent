# [docker/vision.Dockerfile]
# Vision Worker — Device Code Flow + Microsoft Graph API
# Autenticacion via MSAL Device Code Flow con Microsoft Office Client ID.
# API calls via GraphClient (Microsoft Graph API HTTP directo).
# Sin Playwright. Sin Chromium. Sin VNC. Recursos minimos.
#
# v4.0: Hybrid BrowserAuth (Playwright + Chromium + VNC) — FALLIDO (pesado)
# v5.0: Device Code Flow con Microsoft Office Client ID — LIVIANO

FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Instalar dependencias minimas del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Crear directorios y usuario no-root
RUN useradd -m jarvisuser && mkdir -p /app/config && \
    chown -R jarvisuser:jarvisuser /app

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar codigo fuente
COPY --chown=jarvisuser:jarvisuser src/ ./src/
COPY --chmod=755 entrypoint.sh ./

USER jarvisuser

# Entrypoint: solo ejecutar el worker
CMD ["bash", "./entrypoint.sh"]
