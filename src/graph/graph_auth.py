# [src/graph/graph_auth.py]
"""
Autenticación OAuth2 Client Credentials para Microsoft Graph API.

ÚNICO método de conexión soportado: Microsoft Graph API con flujo
Client Credentials (app-only) registrado en Azure AD / Entra ID.
NO existe flujo interactivo: sin device code, sin navegador, sin MFA.

Configuración (variables de entorno):
  GRAPH_TENANT_ID        — Tenant de Azure AD (obligatorio, o TENANT_ID si no es 'default')
  GRAPH_CLIENT_ID        — Client ID (Application ID) del app registration
  GRAPH_CLIENT_SECRET    — Secret del app registration (o GRAPH_CLIENT_SECRET_FILE)
  GRAPH_CLIENT_SECRET_FILE — Ruta al secret montado como Docker Secret
  GRAPH_MAILBOX_UPN      — UPN del buzón a operar (obligatorio en app-only,
                           porque el flujo client credentials no tiene /me)

Permisos de aplicación requeridos en el app registration (con admin consent):
  Mail.ReadWrite, Mail.Send, User.Read.All

Ventajas:
  - 100% desatendido: el worker arranca y autentica solo
  - Sin tokens interactivos que expiren sin mecanismo de renovación
  - Token cache en memoria con renovación automática por expiración
"""

import os
import time
import asyncio
import logging
from typing import Optional

import msal

logger = logging.getLogger("graph.auth")

GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

# Renovar el token 120s antes de su expiración real para evitar
# carreras contra el reloj en las llamadas salientes.
_TOKEN_EXPIRY_SKEW_SECONDS = 120


def _read_secret(env_var: str, file_env_var: str) -> Optional[str]:
    """Lee un secreto desde variable de entorno o desde archivo (Docker Secret)."""
    value = os.environ.get(env_var)
    if value:
        return value.strip()
    path = os.environ.get(file_env_var)
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                logger.info(f"Secreto leído desde archivo: {file_env_var}")
                return content
        except Exception as e:
            logger.warning(f"No se pudo leer el secreto desde {path}: {e}")
    return None


class GraphAPIAuth:
    """Autenticación Client Credentials (app-only) contra Microsoft Graph API.

    Usage:
        auth = GraphAPIAuth()
        token = await auth.get_token()

    El token se adquiere silenciosamente con MSAL ConfidentialClientApplication
    y se reutiliza desde la memoria hasta 120s antes de su expiración.
    """

    def __init__(self):
        tenant_id = os.environ.get("GRAPH_TENANT_ID") or os.environ.get("TENANT_ID", "")
        if not tenant_id or tenant_id == "default":
            raise RuntimeError(
                "GRAPH_TENANT_ID no configurado. La conexión a Graph API "
                "por Client Credentials requiere un tenant de Azure AD explícito."
            )
        self._authority = f"https://login.microsoftonline.com/{tenant_id}"

        self._client_id = os.environ.get("GRAPH_CLIENT_ID", "")
        self._client_secret = _read_secret(
            "GRAPH_CLIENT_SECRET", "GRAPH_CLIENT_SECRET_FILE"
        )

        if not self._client_id:
            raise RuntimeError(
                "GRAPH_CLIENT_ID no configurado. Registre la aplicación en "
                "Azure AD y exporte el Client ID."
            )
        if not self._client_secret:
            raise RuntimeError(
                "GRAPH_CLIENT_SECRET (o GRAPH_CLIENT_SECRET_FILE) no configurado. "
                "Exporte el secret del app registration o móntelo como Docker Secret."
            )

        self._app: Optional[msal.ConfidentialClientApplication] = None
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

        logger.info(
            "GraphAPIAuth inicializado (Client Credentials) — "
            f"tenant={tenant_id[:8]}..., client_id={self._client_id[:8]}..."
        )

    def _get_app(self) -> msal.ConfidentialClientApplication:
        """Crea (una sola vez) la aplicación confidencial MSAL."""
        if self._app is None:
            self._app = msal.ConfidentialClientApplication(
                client_id=self._client_id,
                client_credential=self._client_secret,
                authority=self._authority,
            )
        return self._app

    async def get_token(self) -> Optional[str]:
        """Obtiene un access token válido (desde memoria o adquirido vía MSAL).

        Returns:
            Access token string, o None si la autenticación falló.
        """
        if self._token and time.time() < (self._token_expires_at - _TOKEN_EXPIRY_SKEW_SECONDS):
            return self._token

        # acquire_token_for_client es bloqueante (HTTP) → ejecutar en un thread
        # para no bloquear el event loop del worker.
        result = await asyncio.to_thread(
            self._get_app().acquire_token_for_client, scopes=GRAPH_SCOPE
        )

        if result and "access_token" in result:
            self._token = result["access_token"]
            expires_in = int(result.get("expires_in", 3600))
            self._token_expires_at = time.time() + expires_in
            logger.info(f"Token de Graph API adquirido (expira en {expires_in}s)")
            return self._token

        error = (result or {}).get("error", "unknown")
        error_desc = (result or {}).get("error_description", "")[:300]
        logger.error(f"Autenticación Graph API falló: {error} — {error_desc}")
        self._token = None
        return None

    async def is_authenticated(self) -> bool:
        """True si hay un token válido disponible (o se puede renovar)."""
        return await self.get_token() is not None

    async def refresh_token(self) -> Optional[str]:
        """Fuerza la renovación del token descartando la copia en memoria."""
        self._token = None
        self._token_expires_at = 0.0
        return await self.get_token()
