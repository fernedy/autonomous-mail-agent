# [src/vision/email_reader.py]
"""
Email Reader — Lectura de correos vía Microsoft Graph API.

Única fuente de datos: GraphClient (Microsoft Graph API v1.0).
No más OWA REST API v2.0 (deprecada 31 Mar 2024).
No más Playwright/Chromium.
Los datos vienen como JSON estructurado — sender, subject NUNCA fallan.
"""

import json
import time
import logging
from typing import Optional

logger = logging.getLogger("vision.reader")


def _json_log(event_type, message, data=None, level="INFO"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": "vision",
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }, ensure_ascii=False))


class EmailReader:
    """Coordinador de lectura de correos vía Microsoft Graph API.

    Uso:
        reader = EmailReader(graph_client=graph_client)
        emails = await reader.read_unread_emails(limit=5)
        content = await reader.get_email_content(email_id)
    """

    def __init__(
        self,
        graph_client=None,
    ):
        """
        Args:
            graph_client: GraphClient para llamadas a Microsoft Graph API.
        """
        self._graph = graph_client
        self._stats = {"graph_api": 0, "errors": 0}

    # ──────────────────────────────────────────────
    #  API pública
    # ──────────────────────────────────────────────

    async def read_unread_emails(self, limit: int = 5) -> list[dict]:
        """Lee correos no leídos vía Microsoft Graph API.

        Returns:
            Lista de dicts con estructura unificada:
            - mail_id: str
            - sender: str (NUNCA vacío)
            - sender_email: str
            - subject: str (NUNCA contaminado)
            - preview: str
            - conversation_id: str
            - source: "graph_api"
        """
        if not self._graph:
            _json_log("tactical", "No Graph client available", {}, "ERROR")
            return []

        try:
            raw = await self._graph.list_unread_messages(limit=limit)
            if not raw:
                return []

            self._stats["graph_api"] += len(raw)

            return [
                {
                    "mail_id": msg["id"],
                    "sender": msg["sender_name"],
                    "sender_email": msg.get("sender_email", ""),
                    "subject": msg["subject"],
                    "preview": msg.get("preview", ""),
                    "source": "graph_api",
                    "received_at": msg.get("received_at", ""),
                    "is_read": msg.get("is_read", False),
                    "conversation_id": msg.get("conversation_id", ""),
                }
                for msg in raw
            ]

        except Exception as e:
            self._stats["errors"] += 1
            _json_log("tactical", f"Graph API unread error: {e}", {
                "action": "graph_unread_error",
                "error": str(e)[:100]
            }, "ERROR")
            return []

    async def get_email_content(self, mail_id: str) -> Optional[dict]:
        """Obtiene el contenido completo de un correo.

        Args:
            mail_id: ID del mensaje en Microsoft Graph.

        Returns:
            Dict con {mail_id, sender, sender_email, subject, body_text, body_preview,
            to_recipients, cc_recipients, conversation_id, source}.
        """
        if not self._graph:
            return None

        try:
            content = await self._graph.get_message_content(mail_id)
            if not content:
                return None

            # El body_text puede venir como HTML; convertir a texto plano
            body = content.get("body_text", "") or content.get("body_html", "")
            if body and content.get("body_html") and not content.get("body_text"):
                import re
                body = re.sub(r'<[^>]+>', ' ', body)
                body = re.sub(r'\s+', ' ', body).strip()

            return {
                "mail_id": mail_id,
                "sender": content.get("sender_name", ""),
                "sender_email": content.get("sender_email", ""),
                "subject": content.get("subject", ""),
                "body_text": body,
                "body_preview": content.get("preview", body[:200]),
                "source": "graph_api",
                "to_recipients": content.get("to_recipients", []),
                "cc_recipients": content.get("cc_recipients", []),
                "received_at": content.get("received_at", ""),
                "conversation_id": content.get("conversation_id", ""),
            }

        except Exception as e:
            self._stats["errors"] += 1
            _json_log("tactical", f"Graph API content error: {e}", {
                "action": "graph_content_error",
                "mail_id": mail_id[-20:] if mail_id else "unknown",
                "error": str(e)[:100]
            }, "ERROR")
            return None

    async def mark_as_read(self, mail_id: str) -> bool:
        """Marca un correo como leído vía Microsoft Graph API."""
        if not self._graph:
            return False
        return await self._graph.mark_as_read(mail_id)

    async def archive_message(self, mail_id: str) -> bool:
        """Archiva un correo vía Microsoft Graph API."""
        if not self._graph:
            return False
        return await self._graph.archive_message(mail_id)

    def get_stats(self) -> dict:
        """Retorna estadísticas de uso."""
        return dict(self._stats)
