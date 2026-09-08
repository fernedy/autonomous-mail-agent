# [src/graph/graph_client.py]
"""
Graph Client — Microsoft Graph API wrapper with automatic token refresh.

En vez de browser fetch() con credentials: 'include',
usa HTTP directo con tokens Bearer de GraphAPIAuth (Client Credentials).

Ventajas:
  - Sin Playwright/Chromium (recursos mínimos)
  - API oficial de Microsoft (soportada, NO deprecada)
  - Token renovación automática (MSAL, flujo client credentials)
  - Sin cookies de sesión frágiles

Token lifecycle:
  1. GraphAPIAuth.get_token() → token de memoria o adquirido vía MSAL
  2. Si 401: auto-refresh vía GraphAPIAuth.refresh_token()
  3. Ambos fallan → la llamada retorna None y el caller reintenta

Nota app-only: el flujo Client Credentials no tiene contexto de usuario,
por lo que las rutas /me se traducen automáticamente a
/users/{GRAPH_MAILBOX_UPN} cuando la variable está definida.
"""

import asyncio
import json
import time
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("graph.client")

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"


def _json_log(event_type, message, data=None, level="INFO"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": "graph",
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }, ensure_ascii=False))


class GraphClient:
    """Microsoft Graph API client with automatic Bearer auth + token refresh.

    Usage:
        auth = GraphAPIAuth()
        client = GraphClient(auth)
        messages = await client.list_unread_messages(limit=5)
    """

    def __init__(self, auth, base_url: str = GRAPH_API_BASE):
        """
        Args:
            auth: GraphAPIAuth instance for token management.
            base_url: Graph API base URL (default v1.0).
        """
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        # UPN del buzón para autenticación app-only (Client Credentials).
        # Si no está definido, las llamadas usan /me (escenario delegado).
        self._mailbox_upn = os.environ.get("GRAPH_MAILBOX_UPN", "")
        self._http = httpx.AsyncClient(timeout=30.0)
        self._stats = {"api_calls": 0, "errors": 0}

    # ──────────────────────────────────────────────
    #  Token management
    # ──────────────────────────────────────────────

    async def _get_headers(self) -> dict:
        """Get headers with a valid Bearer token.

        Raises:
            RuntimeError: If no valid token available.
        """
        token = await self._auth.get_token()
        if not token:
            token = await self._auth.refresh_token()
        if not token:
            raise RuntimeError("No valid token available — check GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _resolve_path(self, path: str) -> str:
        """Traduce rutas /me a /users/{UPN} en modo app-only.

        El flujo Client Credentials no tiene usuario de contexto, por lo que
        /me no existe. Si GRAPH_MAILBOX_UPN está definido, todas las rutas
        /me/... se operan sobre ese buzón.
        """
        if self._mailbox_upn and path.startswith("/me"):
            return f"/users/{self._mailbox_upn}{path[3:]}"
        return path

    # ──────────────────────────────────────────────
    #  HTTP request (core)
    # ──────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, body: Optional[dict] = None, retry: bool = True
    ) -> Optional[dict]:
        """Make an authenticated request to Microsoft Graph API.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE).
            path: API path (e.g., "/me/Messages").
            body: Optional request body for POST/PATCH.
            retry: If True, retry once on 401 by refreshing the token.

        Returns:
            Parsed JSON response dict, or None on failure.
        """
        url = f"{self._base_url}{self._resolve_path(path)}"

        try:
            headers = await self._get_headers()
        except RuntimeError as e:
            _json_log("tactical", f"No token for request: {e}", {
                "path": path[:60]
            }, "ERROR")
            self._stats["errors"] += 1
            return None

        try:
            response = await self._http.request(
                method, url, json=body, headers=headers
            )
            self._stats["api_calls"] += 1

            # ── Token expired → refresh and retry once ──
            if response.status_code == 401 and retry:
                _json_log("tactical", "Token expired (401), refreshing and retrying", {
                    "path": path[:60]
                }, "WARNING")
                new_token = await self._auth.refresh_token()
                if new_token:
                    headers["Authorization"] = f"Bearer {new_token}"
                    response = await self._http.request(
                        method, url, json=body, headers=headers
                    )
                    self._stats["api_calls"] += 1
                else:
                    _json_log("tactical", "Token refresh failed — verify Graph API credentials", {
                        "path": path[:60]
                    }, "ERROR")
                    self._stats["errors"] += 1
                    return None

            # ── Rate limiting (429) → exponential backoff + retry ──
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 5))
                # Clampear entre 2 y 60 segundos
                retry_after = max(2, min(retry_after, 60))
                _json_log("tactical", f"Rate limited (429), backing off {retry_after}s", {
                    "path": path[:60],
                    "retry_after": retry_after
                }, "WARNING")
                await asyncio.sleep(retry_after)
                # Reintento con backoff
                response = await self._http.request(
                    method, url, json=body, headers=headers
                )
                self._stats["api_calls"] += 1
                # Si sigue dando 429, fallamos gracefulmente
                if response.status_code == 429:
                    _json_log("tactical", f"Rate limited again after backoff, giving up", {
                        "path": path[:60]
                    }, "ERROR")
                    self._stats["errors"] += 1
                    return None
                # Si ahora es exitoso, continuamos al return response.json()
                if response.status_code < 400:
                    return response.json()
                # Otro error no-429: cae al handler de errores generico abajo

            # ── Error responses (no 429, o 429 que fallo el retry) ──
            if response.status_code >= 400:
                error_body = ""
                try:
                    error_body = response.text[:500]
                except Exception:
                    pass
                _json_log("tactical", f"Graph API {response.status_code}: {error_body[:200]}", {
                    "path": path[:60],
                    "status": response.status_code,
                    "error": error_body[:200]
                }, "WARNING" if (response.status_code == 404 or "InefficientFilter" in error_body) else "ERROR")
                self._stats["errors"] += 1
                return None

            return response.json()

        except httpx.TimeoutException:
            _json_log("tactical", f"Graph API timeout: {method} {path[:60]}", {}, "ERROR")
            self._stats["errors"] += 1
            return None
        except Exception as e:
            _json_log("tactical", f"Graph API error: {method} {path[:60]}: {e}", {
                "error": str(e)[:100]
            }, "ERROR")
            self._stats["errors"] += 1
            return None

    # ──────────────────────────────────────────────
    #  Operaciones de lectura
    # ──────────────────────────────────────────────

    async def list_unread_messages(self, limit: int = 5) -> list[dict]:
        """Obtiene los N correos no leídos más recientes de la Bandeja de Entrada.

        Returns:
            Lista de dicts con {id, sender_name, sender_email, subject, preview,
            received_at, is_read, conversation_id}.
        """
        path = (
            "/me/mailFolders('Inbox')/messages"
            "?$filter=isRead eq false"
            "&$orderby=receivedDateTime desc"
            f"&$top={limit}"
            "&$select=id,from,subject,bodyPreview,receivedDateTime,isRead,conversationId"
        )
        data = await self._request("GET", path)
        if not data or "value" not in data:
            return []

        messages = []
        for msg in data["value"]:
            sender = msg.get("from", {}) or {}
            email_addr = sender.get("emailAddress", {}) or {}
            sender_name = email_addr.get("name", "") or email_addr.get("address", "Remitente Desconocido")
            messages.append({
                "id": msg.get("id", ""),
                "sender_name": sender_name,
                "sender_email": email_addr.get("address", ""),
                "subject": msg.get("subject", "Sin Asunto"),
                "preview": msg.get("bodyPreview", msg.get("bodyPreview", "")),
                "received_at": msg.get("receivedDateTime", ""),
                "is_read": msg.get("isRead", False),
                "conversation_id": msg.get("conversationId", ""),
            })

        return messages

    async def get_message_content(self, message_id: str) -> Optional[dict]:
        """Obtiene el contenido completo de un mensaje.

        Args:
            message_id: ID del mensaje en Graph.

        Returns:
            Dict con {id, sender_name, sender_email, subject, body_text, body_html,
            preview, to_recipients, cc_recipients, conversation_id, is_read}.
        """
        path = (
            f"/me/messages/{message_id}"
            "?$select=id,from,subject,body,toRecipients,ccRecipients,"
            "receivedDateTime,isRead,conversationId"
            "&$expand=attachments($select=name,size,contentType)"
        )
        data = await self._request("GET", path)
        if not data:
            return None

        sender = (data.get("from", {}) or {}).get("emailAddress", {}) or {}
        body = (data.get("body", {}) or {})
        body_content = body.get("content", "") or ""
        body_type = body.get("contentType", "html")

        return {
            "id": data.get("id", ""),
            "sender_name": sender.get("name", "Remitente Desconocido"),
            "sender_email": sender.get("address", ""),
            "subject": data.get("subject", "Sin Asunto"),
            "body_text": body_content if body_type.lower() == "text" else "",
            "body_html": body_content if body_type.lower() == "html" else "",
            "preview": data.get("bodyPreview", ""),
            "received_at": data.get("receivedDateTime", ""),
            "is_read": data.get("isRead", False),
            "conversation_id": data.get("conversationId", ""),
            "to_recipients": [
                r.get("emailAddress", {}).get("name", "")
                for r in (data.get("toRecipients", []) or [])
            ],
            "cc_recipients": [
                r.get("emailAddress", {}).get("name", "")
                for r in (data.get("ccRecipients", []) or [])
            ],
        }

    async def get_messages_by_conversation(self, conversation_id: str) -> list[dict]:
        """Obtiene todos los mensajes de una conversación/hilo.

        Estrategia:
        1. Intentar $search con conversationId (mas eficiente que $filter)
        2. Si falla, intentar $filter con conversationId (puede dar InefficientFilter)
        3. Si ambos fallan, retornar [] para que el caller procese como single message

        Args:
            conversation_id: ID de la conversación.

        Returns:
            Lista de mensajes ordenados por fecha ASC.
        """
        # Helper para parsear mensajes
        def _parse_messages(value):
            results = []
            for msg in (value or []):
                email_addr = (msg.get("from", {}) or {}).get("emailAddress", {}) or {}
                results.append({
                    "id": msg.get("id", ""),
                    "sender_name": email_addr.get("name", ""),
                    "sender_email": email_addr.get("address", ""),
                    "subject": msg.get("subject", ""),
                    "preview": msg.get("bodyPreview", ""),
                    "received_at": msg.get("receivedDateTime", ""),
                    "is_read": msg.get("isRead", False),
                })
            return results

        # Estrategia 1: $search (mas eficiente, usa indice de busqueda de Microsoft)
        # Nota: $search NO soporta $orderby (Graph API rechaza con 400).
        # Los resultados de $search vienen ordenados por relevancia.
        # Si $search falla (mailbox sin indice), cae a $filter.
        search_path = (
            "/me/messages"
            f"?$search=\"conversationId:{conversation_id}\""
            "&$top=50"
            "&$select=id,from,subject,bodyPreview,receivedDateTime,isRead"
        )
        data = await self._request("GET", search_path)
        if data and "value" in data:
            return _parse_messages(data["value"])

        # Estrategia 2: $filter (fallback - puede dar InefficientFilter en mailboxes grandes)
        filter_path = (
            "/me/messages"
            f"?$filter=conversationId eq '{conversation_id}'"
            "&$orderby=receivedDateTime asc"
            "&$top=50"
            "&$select=id,from,subject,bodyPreview,receivedDateTime,isRead"
        )
        data = await self._request("GET", filter_path)
        if data and "value" in data:
            return _parse_messages(data["value"])

        # Ambas estrategias fallaron -> dejamos que el caller procese como single
        return []

    # ──────────────────────────────────────────────
    #  Operaciones de escritura
    # ──────────────────────────────────────────────

    async def create_reply_draft(self, message_id: str, body_text: str) -> Optional[str]:
        """Crea un borrador de respuesta a un mensaje.

        IMPORTANTE: NO incluir campo 'comment' duplicado.
        El endpoint createReply de Graph API rechaza requests con
        SamePropertyContentConflictBody si message.body.content y comment
        tienen el mismo contenido. Solo usar message.body.content.

        Args:
            message_id: ID del mensaje original.
            body_text: Texto del cuerpo de la respuesta.

        Returns:
            ID del borrador creado, o None si falla.
        """
        html_body = body_text.replace('\n', '<br>').replace('\r', '')
        payload = {
            "message": {
                "body": {
                    "contentType": "HTML",
                    "content": html_body
                }
            }
        }
        data = await self._request("POST", f"/me/messages/{message_id}/createReply", payload)
        if data:
            return data.get("id")
        return None

    async def send_draft(self, draft_id: str) -> bool:
        """Envía un borrador.

        Args:
            draft_id: ID del borrador a enviar.

        Returns:
            True si se envió correctamente.
        """
        data = await self._request("POST", f"/me/messages/{draft_id}/send")
        return data is not None

    async def send_reply(self, message_id: str, body_text: str) -> bool:
        """Envía una respuesta DIRECTAMENTE (sin crear borrador)."""
        html_body = body_text.replace('\n', '<br>').replace('\r', '')
        payload = {
            "message": {
                "body": {
                    "contentType": "HTML",
                    "content": html_body
                }
            }
        }
        data = await self._request("POST", f"/me/messages/{message_id}/reply", payload)
        return data is not None

    async def create_reply_all_draft(self, message_id: str, body_html: str) -> Optional[str]:
        """Crea un borrador de respuesta a TODOS los destinatarios con HTML completo.

        El body_html debe ser HTML completo que incluya:
        - El texto de respuesta con formato
        - La firma HTML del usuario (interactiva, con estilos)
        - El mensaje original embebido (thread preservado manualmente)

        IMPORTANTE: El caller debe obtener el cuerpo del mensaje original
        con get_message_content() y embekerlo en el HTML para preservar
        el historial del hilo.

        Args:
            message_id: ID del mensaje original.
            body_html: Cuerpo HTML completo (respuesta + firma + hilo original).

        Returns:
            ID del borrador creado, o None si falla.
        """
        payload = {
            "message": {
                "body": {
                    "contentType": "HTML",
                    "content": body_html
                }
            }
        }
        data = await self._request("POST", f"/me/messages/{message_id}/createReplyAll", payload)
        if data:
            return data.get("id")
        return None

    async def mark_as_read(self, message_id: str) -> bool:
        """Marca un mensaje como leído."""
        data = await self._request("PATCH", f"/me/messages/{message_id}", {"isRead": True})
        return data is not None

    async def archive_message(self, message_id: str) -> bool:
        """Mueve un mensaje a la carpeta de Archivo.

        Graph API no tiene un comando directo 'Archive' como OWA REST API.
        Usamos move a la carpeta 'archive' (si existe) o marcamos como leído.
        """
        # Primero intentamos obtener el ID de la carpeta Archive
        folders = await self._request("GET", "/me/mailFolders")
        archive_folder_id = None
        if folders and "value" in folders:
            for folder in folders["value"]:
                if folder.get("wellKnownName") == "archive":
                    archive_folder_id = folder.get("id")
                    break

        if archive_folder_id:
            data = await self._request(
                "POST", f"/me/messages/{message_id}/move",
                {"destinationId": archive_folder_id}
            )
            if data:
                return True

        # Fallback: marcar como leído
        return await self.mark_as_read(message_id)

    async def move_to_folder(self, message_id: str, folder_id: str) -> bool:
        """Mueve un mensaje a una carpeta específica."""
        data = await self._request(
            "POST", f"/me/messages/{message_id}/move",
            {"destinationId": folder_id}
        )
        return data is not None

    async def delete_message(self, message_id: str) -> bool:
        """Mueve un mensaje a Elementos Eliminados."""
        # En Graph API, DELETE mueve a Elementos Eliminados automáticamente
        data = await self._request("DELETE", f"/me/messages/{message_id}")
        return data is not None

    # ──────────────────────────────────────────────
    #  Operaciones de gestión
    # ──────────────────────────────────────────────

    async def get_sent_messages(self, limit: int = 5) -> list[dict]:
        """Obtiene los N últimos correos enviados.

        Args:
            limit: Número de correos a obtener.

        Returns:
            Lista de dicts con {id, sender_name, to_recipients, subject, preview}.
        """
        path = (
            "/me/mailFolders('SentItems')/messages"
            "?$orderby=sentDateTime desc"
            f"&$top={limit}"
            "&$select=id,toRecipients,subject,bodyPreview,sentDateTime,from"
        )
        data = await self._request("GET", path)
        if not data or "value" not in data:
            return []

        messages = []
        for msg in data["value"]:
            sender = (msg.get("from", {}) or {}).get("emailAddress", {}) or {}
            to_list = [
                r.get("emailAddress", {}).get("name",
                    r.get("emailAddress", {}).get("address", ""))
                for r in (msg.get("toRecipients", []) or [])
            ]
            messages.append({
                "id": msg.get("id", ""),
                "sender_name": sender.get("name", ""),
                "to_recipients": "; ".join(to_list),
                "subject": msg.get("subject", ""),
                "preview": msg.get("bodyPreview", ""),
                "sent_at": msg.get("sentDateTime", ""),
            })

        return messages

    # ──────────────────────────────────────────────
    #  Diagnóstico
    # ──────────────────────────────────────────────

    async def health_check(self) -> dict:
        """Verifica que la API funcione (GET /me).

        Returns:
            Dict con {status, user, email, latency_ms}.
        """
        start = time.time()
        data = await self._request("GET", "/me?$select=id,displayName,mail,userPrincipalName")
        latency = round((time.time() - start) * 1000)

        if data:
            return {
                "status": "ok",
                "user": data.get("displayName", "unknown"),
                "email": data.get("mail", ""),
                "latency_ms": latency,
            }

        return {
            "status": "error",
            "latency_ms": latency,
        }

    async def get_user_profile(self) -> Optional[dict]:
        """Obtiene el perfil del usuario autenticado.

        Returns:
            Dict con {id, displayName, mail, userPrincipalName}.
        """
        data = await self._request("GET", "/me?$select=id,displayName,mail,userPrincipalName")
        if data:
            return {
                "id": data.get("id", ""),
                "displayName": data.get("displayName", ""),
                "mail": data.get("mail", ""),
                "userPrincipalName": data.get("userPrincipalName", ""),
            }
        return None

    def get_stats(self) -> dict:
        """Retorna estadísticas de uso."""
        return dict(self._stats)

    async def close(self):
        """Cierra la sesión HTTP."""
        try:
            await self._http.aclose()
        except Exception:
            pass
