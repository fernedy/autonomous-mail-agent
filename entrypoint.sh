#!/bin/bash
set -e

echo "[SYSTEM] Iniciando Vision Worker (Device Code Flow + Microsoft Graph API)..."
echo "[SYSTEM] Sin Playwright. Sin Chromium. Sin VNC. Recursos minimos."
echo "[SYSTEM] Autenticacion via Device Code Flow con Microsoft Office Client ID."

# Solo ejecutar el worker
exec python -m vision.vision_worker
