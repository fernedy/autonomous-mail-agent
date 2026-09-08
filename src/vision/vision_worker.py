# [src/vision/vision_worker.py]
"""
Vision Worker - Triage de correos via Microsoft Graph API.

ARQUITECTURA v6.0 (Sep 2026):
  Autenticacion via GraphAPIAuth (OAuth2 Client Credentials, app-only).
  API via GraphClient (Microsoft Graph API v1.0 HTTP directo).

  La autenticacion es 100% desatendida:
  1. MSAL adquiere token con GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET
  2. GraphClient usa el Bearer token para Microsoft Graph API
  3. El token se renueva automaticamente antes de expirar

  Sin Playwright. Sin Chromium. Sin VNC. Sin device code. Sin MFA interactivo.
  Recursos minimos: CPU 0.25, RAM 256MB.
"""

import os
import asyncio
import json
import logging
import re
import time
from graph.graph_auth import GraphAPIAuth
from graph.graph_client import GraphClient
from shared.redis_client import RedisTaskBroker
from shared.utils import extract_clean_reason
from vision.email_reader import EmailReader
from auto_learning import AutoLearner
from brain.llm_router_gateway import LLMRouterGateway
from observability.tracer import AgentTracer

logger = logging.getLogger("vision.worker")


def _json_log(event_type, message, data=None, level="INFO"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": "vision",
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }, ensure_ascii=False))


class VisionWorker:
    """Orquestador de triage de correos via Microsoft Graph API.

    Autenticacion via OAuth2 Client Credentials (app-only) en Azure AD.
    Sin Playwright. Sin Chromium. Sin OWA REST API. Sin device code.
    Solo Graph API oficial + Brain LLM Gateway + Telegram notifications.
    """

    def __init__(self, graph_client: GraphClient, redis_broker: RedisTaskBroker):
        self.graph = graph_client
        self.auth: GraphAPIAuth | None = None
        self.broker = redis_broker
        # Multi-tenant: usar TENANT_ID si está configurado, sino 'default'
        self.tenant_id = os.environ.get("TENANT_ID", "default")
        self.queue_in = f"queue:{self.tenant_id}:vision_tasks"
        self.queue_out = f"queue:{self.tenant_id}:raw_emails"
        self.processed_history = set()

        # Control de pausa para auto-aprendizaje
        self._learning_paused = asyncio.Event()
        self._learner: AutoLearner | None = None
        self._learning_lock = asyncio.Lock()
        self._trigger_inbox_clean = asyncio.Event()
        
        # LLM Router Gateway para análisis con IA (detección de intención +
        # selección automática de modelo; integración API tipo OpenAI)
        self._llm_router = LLMRouterGateway()
        self._llm_router.model = "auto"  # auto: el gateway elige el modelo por intención
        self._was_inbox_empty = False

        # Microsoft Graph API client
        self.email_reader = EmailReader(graph_client=self.graph)

        # Health check background task
        self._health_task = None

    # ──────────────────────────────────────────────
    #  Firma HTML (desde SKILL.md)
    # ──────────────────────────────────────────────

    @staticmethod
    def _read_signature_from_skill() -> str:
        """Lee la firma HTML desde config/SKILL.md.

        Busca el bloque entre DFGA_SIGNATURE_START y DFGA_SIGNATURE_END
        en el archivo SKILL.md. Si no encuentra, retorna string vacio
        para que el caller use el fallback (env var o default).

        Returns:
            El HTML de la firma, o '' si no se encuentra.
        """
        try:
            skill_path = "/app/config/SKILL.md"
            if not os.path.isfile(skill_path):
                return ""
            with open(skill_path, "r", encoding="utf-8") as f:
                content = f.read()
            match = re.search(
                r'DFGA_SIGNATURE_START\s*\n(.*?)\nDFGA_SIGNATURE_END',
                content,
                re.DOTALL
            )
            if match:
                return match.group(1).strip()
            return ""
        except Exception:
            return ""

    # ──────────────────────────────────────────────
    #  Brain triage
    # ──────────────────────────────────────────────

    async def _evaluate_with_brain(self, target: dict, content: str) -> dict | None:
        """Envia contenido al LLM para evaluacion de triage.

        Args:
            target: Dict con {mail_id, sender, subject}
            content: Texto del cuerpo del correo

        Returns:
            Dict con decision del LLM, o None si timeout.
        """
        safe_content = content[:45000]

        await self.broker.client.delete(self.queue_in)
        await self.broker.push_task(self.queue_out, {
            "action": "triage",
            "mail_id": target["mail_id"],
            "sender": target["sender"],
            "subject": target.get("subject", ""),
            "subject_source": "graph_api",  # Graph API subjects son autoritativos
            "dom_content": safe_content
        })

        start_wait = time.time()
        last_alert = 0.0
        timeout = 360

        while True:
            elapsed = time.time() - start_wait
            if elapsed > timeout:
                _json_log("tactical", "Brain triage timed out", {
                    "action": "brain_timeout",
                    "mail_id": target.get("mail_id", "unknown")[-20:]
                }, "WARNING")
                return None
            if elapsed > 300 and time.time() - last_alert > 120:
                await self.broker.push_task("queue:notifications", {
                    "type": "slow_operation",
                    "operation": f"brain_triage:{target.get('mail_id','unknown')[-20:]}",
                    "duration_seconds": round(elapsed),
                    "threshold_seconds": 300
                })
                last_alert = time.time()
            res = await self.broker.get_task(self.queue_in)
            if res and "decision" in res:
                return res
            await asyncio.sleep(2)

    # ──────────────────────────────────────────────
    #  Procesamiento de acciones (Graph API)
    # ──────────────────────────────────────────────

    async def process_action(self, decision: dict, mail_id: str, sender: str,
                             subject: str = "Sin asunto") -> None:
        """Ejecuta la accion determinada por el brain via Microsoft Graph API.

        Args:
            decision: Dict del brain con {decision, reasoning, draft}
            mail_id: ID del mensaje en Graph
            sender: Nombre del remitente
            subject: Asunto del correo
        """
        label = decision.get("decision", "Noise")
        reasoning = decision.get("reasoning", "")
        draft = decision.get("draft", "Estimado, recibido. Procedo con la gestion.")

        if label == "Noise":
            _json_log("tactical", "Archiving noise email via Graph API", {
                "action": "graph_noise_archive",
                "mail_id": mail_id[-20:]
            })
            await self.graph.archive_message(mail_id)

        elif label == "Meeting":
            _json_log("tactical", "Meeting invite, skipping", {
                "action": "graph_meeting_skip",
                "mail_id": mail_id[-20:],
                "subject": subject
            })

        elif label == "Action":
            _json_log("tactical", "Creating reply draft via Graph API", {
                "action": "graph_draft_create",
                "mail_id": mail_id[-20:],
                "subject": subject
            })

            # Limpiar saludo del draft
            patron_saludo = r'^(Buen(?:os)?\s+d[ii]as[^.,\n]*|Buenas\s+tardes[^.,\n]*|Cordial\s+saludo[^.,\n]*)[,.]?:?\s*'
            draft = re.sub(patron_saludo, r'\1,\n\n', draft.strip(), flags=re.IGNORECASE)

            # ═══ CONSTRUIR HTML COMPLETO (respuesta + firma interactiva + hilo original) ═══
            # SKILL.md indica que el draft NO debe incluir la firma — se agrega aquí.
            #
            # Estrategia: usar message.body.content con HTML para poder incluir:
            # 1. El texto de respuesta (convertido a HTML)
            # 2. La firma interactiva (HTML con estilos corporativos)
            # 3. El mensaje original embebido (thread preservado manualmente)
            
            # Obtener el cuerpo HTML del mensaje original para preservar el hilo
            original_msg = await self.graph.get_message_content(mail_id)
            original_body_html = (original_msg.get("body_html", "") if original_msg else "")
            
            # Convertir draft (texto plano del LLM) a HTML paragraph
            draft_html = draft.strip()\
                .replace('\n\n', '</p><p>')\
                .replace('\n', '<br>')
            draft_html = f'<p>{draft_html}</p>'
            
            # Firma HTML interactiva con estilo corporativo Outlook
            # Lee desde config/SKILL.md (bloque entre DFGA_SIGNATURE_START/END).
            # Fallback: DFGA_SIGNATURE_HTML env var, luego DFGA_SIGNATURE env var,
            # luego la firma default hardcodeada.
            signature = self._read_signature_from_skill()
            if not signature:
                signature = os.environ.get(
                    "DFGA_SIGNATURE_HTML",
                    os.environ.get(
                        "DFGA_SIGNATURE",
                        ''
                    )
                )
                if not signature or "<" not in signature or ">" not in signature:
                    signature = (
                        '<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#1F3864;">'
                        '<br><br>'
                        '<p style="margin:0 0 4px 0;">Cordialmente y atento a sus comentarios.</p>'
                        '<p style="margin:0;font-weight:bold;">Darlyn Fernedy González Arias</p>'
                        '</div>'
                    )
            
            # Quoted original message (thread history)
            # Separador estandar de Outlook para mensajes reenviados/respondidos
            if original_body_html:
                # Intentar extraer solo <body>...</body> si existe HTML completo
                body_match = re.search(r'<body[^>]*>(.*?)</body>', original_body_html, re.DOTALL | re.IGNORECASE)
                if body_match:
                    quoted_body = body_match.group(1)
                else:
                    quoted_body = original_body_html
                quoted_section = (
                    '<div style="border:none;border-top:solid #B3B3B3 1.0pt;padding:3.0pt 0cm 0cm 0cm;">'
                    '<p><strong>Mensaje original</strong></p>'
                    f'{quoted_body}'
                    '</div>'
                )
            else:
                # Si no hay HTML, intentar con body_text
                original_body_text = (original_msg.get("body_text", "") if original_msg else "")
                if original_body_text:
                    quoted_section = (
                        '<div style="border:none;border-top:solid #B3B3B3 1.0pt;padding:3.0pt 0cm 0cm 0cm;">'
                        '<p><strong>Mensaje original</strong></p>'
                        f'<p>{original_body_text.replace(chr(10), "<br>")}</p>'
                        '</div>'
                    )
                else:
                    quoted_section = ''
            
            # Ensamblar HTML completo
            full_html = (
                '<html><body style="font-family:Calibri,Arial,sans-serif;font-size:11pt;">'
                f'{draft_html}'
                f'{signature}'
                f'{quoted_section}'
                '</body></html>'
            )
            
            # Crear borrador vía Graph API — RESPONDER A TODOS (ReplyAll)
            draft_id = await self.graph.create_reply_all_draft(mail_id, full_html)
            if not draft_id:
                _json_log("tactical", "Graph API reply draft failed", {
                    "action": "graph_draft_failed",
                    "mail_id": mail_id[-20:]
                }, "ERROR")
                self.processed_history.add(mail_id)
                return

            _json_log("tactical", "Reply draft created via Graph API", {
                "action": "graph_draft_created",
                "mail_id": mail_id[-20:],
                "draft_id": draft_id[-20:]
            })

            # ── HITL (Human In The Loop) via Telegram ──
            await self.broker.client.delete("queue:user_decisions")
            to_recipients = decision.get("to_recipients", "")
            await self.broker.push_task("queue:notifications", {
                "type": "triage",
                "mail_id": mail_id,
                "sender": sender,
                "to_recipients": to_recipients,
                "subject": subject,
                "reasoning": reasoning,
                "draft": draft
            })

            _json_log("tactical", "Waiting for HITL decision", {
                "action": "hilt_wait",
                "mail_id": mail_id[-20:]
            })

            user_decision = None
            short_id = mail_id[-30:] if mail_id else "unknown"
            hilt_start = time.time()

            while True:
                elapsed = time.time() - hilt_start
                if elapsed > 600:
                    _json_log("tactical", "HITL timeout, auto-discarding", {
                        "action": "hilt_timeout",
                        "mail_id": mail_id[-20:]
                    }, "WARNING")
                    user_decision = "discard"
                    await self.broker.push_task("queue:notifications", {
                        "type": "system_alert",
                        "icon": "\u23f0",
                        "title": "Auto-Descartado por Timeout",
                        "message": f"El correo '{subject}' de {sender} fue auto-descartado."
                    })
                    break

                res = await self.broker.get_task("queue:user_decisions")
                if res and (res.get("mail_id") == mail_id or res.get("short_id") == short_id):
                    user_decision = res.get("decision")
                    break
                await asyncio.sleep(2)

            _json_log("tactical", f"HITL decision: {user_decision}", {
                "action": "hilt_decision",
                "mail_id": mail_id[-20:],
                "decision": user_decision
            })

            if user_decision == "accept":
                _json_log("tactical", "Sending draft via Graph API", {
                    "action": "graph_send",
                    "mail_id": mail_id[-20:],
                    "draft_id": draft_id[-20:]
                })
                sent = await self.graph.send_draft(draft_id)
                if sent:
                    payload = json.dumps({
                        "to": sender,
                        "subject": subject,
                        "body": draft,
                        "timestamp": time.time(),
                        "mail_id": mail_id
                    }, ensure_ascii=False)
                    await self.broker.client.rpush("queue:sent_emails", payload)
                    _json_log("tactical", "Email sent via Graph API", {
                        "action": "graph_sent",
                        "subject": subject
                    })
                else:
                    _json_log("tactical", "Failed to send draft via Graph API", {
                        "action": "graph_send_failed",
                        "mail_id": mail_id[-20:]
                    }, "ERROR")

            elif user_decision == "draft":
                _json_log("tactical", "Leaving as draft", {
                    "action": "graph_draft_keep",
                    "mail_id": mail_id[-20:]
                })
                label = "Executive_Decision"

            elif user_decision == "discard":
                _json_log("tactical", "Discarding draft via Graph API", {
                    "action": "graph_draft_discard",
                    "mail_id": mail_id[-20:]
                })
                await self.graph.delete_message(draft_id)
                label = "Noise"

        else:  # Executive_Decision
            _json_log("tactical", "Elevating to executive decision", {
                "action": "executive_elevate",
                "mail_id": mail_id[-20:],
                "subject": subject
            })
            await self.broker.client.delete(self.queue_in)
            await self.broker.push_task("queue:notifications", {
                "type": "triage",
                "mail_id": mail_id,
                "subject": subject,
                "reasoning": reasoning
            })

        # Trazar evaluacion
        AgentTracer.trace_evaluation({
            "mail_id": mail_id,
            "sender": sender,
            "subject": subject,
            "decision": label,
            "reason": extract_clean_reason(reasoning),
            "source": "graph_api"
        })

    # ──────────────────────────────────────────────
    #  Procesamiento de correos individuales
    # ──────────────────────────────────────────────

    async def _process_single(self, target: dict) -> None:
        """Procesa un correo individual via Microsoft Graph API.

        El sender/subject vienen del JSON estructurado de la API.
        """
        mail_id = target["mail_id"]

        # Obtener contenido completo via Graph API
        content = await self.graph.get_message_content(mail_id)
        if not content:
            _json_log("tactical", "Graph API content not available", {
                "action": "graph_content_failed",
                "mail_id": mail_id[-20:]
            }, "ERROR")
            self.processed_history.add(mail_id)
            return

        # Sender/subject NUNCA fallan (vienen del JSON estructurado)
        target["sender"] = content.get("sender_name", target.get("sender", ""))
        target["subject"] = content.get("subject", target.get("subject", "Sin Asunto"))
        target["sender_email"] = content.get("sender_email", "")

        # Obtener texto plano (body_text o body_html convertido)
        mail_text = content.get("body_text", "") or content.get("body_preview", "") or ""
        if not mail_text and content.get("body_html"):
            html = content["body_html"]
            mail_text = re.sub(r'<[^>]+>', ' ', html)
            mail_text = re.sub(r'\s+', ' ', mail_text).strip()

        _json_log("tactical", "Evaluating email via Graph API content", {
            "action": "graph_evaluate",
            "mail_id": mail_id[-20:],
            "sender": target["sender"][:60],
            "subject": target.get("subject", "")[:60],
            "body_length": len(mail_text)
        })

        # Enviar al brain para triage
        res = await self._evaluate_with_brain(target, mail_text)
        if res:
            llm_sender = res.get("sender", "").strip()
            sender = llm_sender if llm_sender else target["sender"]

            _json_log("tactical", "Email evaluated via Graph API", {
                "action": "single_evaluated",
                "mail_id": mail_id[-20:],
                "sender": sender,
                "decision": res["decision"]
            })

            await self.process_action(res, mail_id, sender, target.get("subject", "Sin asunto"))
            self.processed_history.add(mail_id)
        else:
            _json_log("tactical", "Brain timeout on Graph API email", {
                "action": "single_timeout",
                "mail_id": mail_id[-20:]
            }, "WARNING")
            await asyncio.sleep(15)

    async def _process_thread(self, target: dict) -> None:
        """Procesa un hilo de correos via Microsoft Graph API.

        Optimizado: combina TODOS los mensajes del hilo en UNA sola
        evaluacion del brain (no N+1 evaluaciones individuales).
        El LLM recibe el contexto completo del hilo para mejor decision.

        Usa conversationId para obtener todos los mensajes del hilo.
        """
        mail_id = target["mail_id"]

        # Obtener conversationId del mensaje principal
        main_content = await self.graph.get_message_content(mail_id)
        if not main_content:
            _json_log("tactical", "Graph thread: main content not available", {
                "action": "graph_thread_failed",
                "mail_id": mail_id[-20:]
            }, "ERROR")
            self.processed_history.add(mail_id)
            return

        conversation_id = main_content.get("conversation_id", "")
        target["sender"] = main_content.get("sender_name", target.get("sender", ""))
        target["subject"] = main_content.get("subject", target.get("subject", "Sin Asunto"))

        if not conversation_id:
            _json_log("tactical", "No conversationId, processing as single", {
                "action": "graph_thread_no_conv_id",
                "mail_id": mail_id[-20:]
            })
            return await self._process_single(target)

        # Obtener todos los mensajes del hilo
        thread_messages = await self.graph.get_messages_by_conversation(conversation_id)
        if not thread_messages or len(thread_messages) < 2:
            _json_log("tactical", "Thread has only 1 message, processing as single", {
                "action": "graph_thread_single_msg",
                "mail_id": mail_id[-20:]
            })
            return await self._process_single(target)

        _json_log("tactical", f"Processing thread of {len(thread_messages)} messages as one context", {
            "action": "graph_thread_messages",
            "mail_id": mail_id[-20:],
            "count": len(thread_messages)
        })

        # ═══ CONSOLIDAR: TODOS los mensajes del hilo en UN solo contexto ═══
        # IMPORTANTE: El mensaje mas reciente + instruccion van PRIMERO en el
        # contenido para evitar que _evaluate_with_brain (que trunca a 45000
        # chars desde el FINAL) los corte silenciosamente.
        #
        # El cuerpo completo del mensaje bajo evaluacion va primero, luego
        # el contexto del hilo (mensajes anteriores con previews cortos).
        
        # Texto del mensaje principal (el que estamos clasificando)
        # Reusamos main_content que ya obtuvimos arriba, evitando API call doble.
        first_text = (main_content.get("body_text", "")
                     or main_content.get("body_preview", "")
                     or "")
        if not first_text and main_content.get("body_html"):
            first_text = re.sub(r'<[^>]+>', ' ', main_content["body_html"])
            first_text = re.sub(r'\s+', ' ', first_text).strip()
        
        # Preview cortos de mensajes anteriores del hilo (contexto)
        thread_sections = []
        for i, msg in enumerate(thread_messages):
            # Saltar el mensaje principal (lo incluimos primero aparte)
            if msg.get("id", "") == mail_id:
                continue
            sender_name = msg.get("sender_name", "Desconocido")
            subject = msg.get("subject", "Sin asunto")
            preview = msg.get("preview", "") or msg.get("body_preview", "")
            if not preview:
                continue
            # Truncar cada preview a 500 chars para evitar sobrepasar el limite
            preview = preview[:500]
            section = (
                f"--- Mensaje {i+1} ---\n"
                f"De: {sender_name}\n"
                f"Asunto: {subject}\n"
                f"Contenido:\n{preview}\n"
            )
            thread_sections.append(section)

        # Construir contenido: PRIMERO el mensaje actual, LUEGO el contexto
        final_content = (
            "═" * 50 + "\n"
            "MENSAJE PRINCIPAL A EVALUAR:\n"
            f"{first_text[:10000]}\n\n"
            "═" * 50 + "\n"
        )
        
        if thread_sections:
            final_content += (
                f"CONTEXTO DEL HILO ({len(thread_sections)} mensajes anteriores):\n"
                + "\n\n".join(thread_sections)
                + "\n\n"
            )
        
        final_content += (
            "═" * 50 + "\n"
            "INSTRUCCION: Con base en el MENSAJE PRINCIPAL arriba y el contexto del hilo,\n"
            "clasifica como Noise, Action o Executive_Decision.\n"
            "═" * 50
        )
        final_res = await self._evaluate_with_brain(target, final_content)

        if final_res:
            llm_sender = final_res.get("sender", "").strip()
            sender = llm_sender if llm_sender else target["sender"]

            _json_log("tactical", "Thread verdict (single consolidated evaluation)", {
                "action": "graph_thread_verdict",
                "mail_id": mail_id[-20:],
                "decision": final_res["decision"],
                "sender": sender
            })
            await self.process_action(final_res, mail_id, sender,
                                       target.get("subject", "Sin asunto"))
            self.processed_history.add(mail_id)
        else:
            _json_log("tactical", "Brain timeout on consolidated thread", {
                "action": "graph_thread_timeout",
                "mail_id": mail_id[-20:]
            }, "WARNING")
            await asyncio.sleep(15)

    # ──────────────────────────────────────────────
    #  Scheduler de auto-aprendizaje
    # ──────────────────────────────────────────────

    async def _auto_learning_scheduler(self, interval_minutes: int = 60):
        """Ejecuta AutoLearner cada `interval_minutes` minutos.

        Usa GraphClient para obtener correos enviados.
        """
        if self._learner is None:
            self._learner = AutoLearner(
                graph_client=self.graph,
                router=self._llm_router,
            )

        _json_log("action", "Auto-learning scheduler started", {
            "interval_minutes": interval_minutes
        })

        while True:
            try:
                try:
                    await asyncio.wait_for(
                        self._trigger_inbox_clean.wait(),
                        timeout=interval_minutes * 60
                    )
                    self._trigger_inbox_clean.clear()
                    _json_log("action", "Auto-learning triggered by inbox clean event", {})
                except asyncio.TimeoutError:
                    _json_log("action", "Auto-learning triggered by timer", {
                        "interval_minutes": interval_minutes
                    })

                if self._learning_lock.locked():
                    _json_log("action", "Auto-learning lock held, skipping", {})
                    continue

                async with self._learning_lock:
                    self._learning_paused.set()
                    await asyncio.sleep(2)

                    success, patterns, behavioral_counts, analysis_mode = await self._learner.run(limit=5)

                    if success:
                        _json_log("action", "Auto-learning cycle completed", {
                            "success": True,
                            "emails_analyzed": patterns.sample_count if patterns else 0,
                            "mode": analysis_mode,
                            "behavioral_rules": behavioral_counts
                        })
                        await self.broker.push_task("queue:notifications", {
                            "type": "learning_result",
                            "icon": "\U0001f9e0",
                            "title": "Auto-Aprendizaje Completado",
                            "success": True,
                            "sample_count": patterns.sample_count if patterns else 0,
                            "greeting_patterns": patterns.greeting_patterns if patterns else [],
                            "signoff_patterns": patterns.signoff_patterns if patterns else [],
                            "dominant_tone": patterns.dominant_tone if patterns else "",
                            "avg_length_chars": patterns.avg_length_chars if patterns else 0,
                            "common_phrases": patterns.common_phrases if patterns else [],
                            "analysis_mode": analysis_mode,
                            "behavioral_counts": behavioral_counts,
                        })
                    else:
                        _json_log("action", "Auto-learning cycle completed (no output)", {
                            "success": False
                        }, "WARNING")

            except asyncio.CancelledError:
                _json_log("action", "Auto-learning scheduler cancelled", {}, "WARNING")
                break
            except Exception as e:
                _json_log("action", f"Auto-learning error: {e}", {
                    "error": str(e)[:200]
                }, "ERROR")
            finally:
                self._learning_paused.clear()

    # ──────────────────────────────────────────────
    #  Health check writer (Redis)
    # ──────────────────────────────────────────────

    async def _health_writer(self):
        """Escribe estado de salud de Graph API a Redis cada 300s (5 min).

        Frecuencia reducida de 60s → 300s para evitar rate limiting (429)
        en el endpoint /me de Graph API.
        """
        while True:
            try:
                health_data = {
                    "timestamp": time.time(),
                    "status": "unknown",
                    "user": "",
                    "latency_ms": 0,
                    "error": "",
                }

                # Health check via Graph API
                try:
                    hc = await self.graph.health_check()
                    health_data["status"] = hc.get("status", "error")
                    health_data["user"] = hc.get("user", "")
                    health_data["latency_ms"] = hc.get("latency_ms", 0)
                except Exception as e:
                    health_data["status"] = "error"
                    health_data["error"] = str(e)[:100]

                await self.broker.client.set(
                    "graph_api:health",
                    json.dumps(health_data, ensure_ascii=False),
                    ex=180
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                _json_log("tactical", f"Health writer error: {e}", {}, "WARNING")
            await asyncio.sleep(300)  # 5 min para evitar rate limiting 429

    # ──────────────────────────────────────────────
    #  Command listener (manual triggers)
    # ──────────────────────────────────────────────

    async def _command_listener(self):
        """Escucha comandos manuales desde queue:commands."""
        _json_log("action", "Command listener started", {"queue": "queue:commands"})

        poll_delay = 2.0
        while True:
            try:
                cmd = await self.broker.get_task("queue:commands")
                if cmd is None:
                    await asyncio.sleep(poll_delay)
                    poll_delay = min(poll_delay * 1.5, 10.0)
                    continue

                poll_delay = 2.0
                cmd_type = cmd.get("type", "")
                _json_log("action", f"Received command: {cmd_type}", {
                    "command": cmd_type,
                    "source": cmd.get("source", "unknown")
                })

                if cmd_type == "force_auto_learning":
                    if self._learning_lock.locked():
                        await self.broker.push_task("queue:notifications", {
                            "type": "system_alert",
                            "icon": "\u23f3",
                            "title": "Auto-Aprendizaje ya en progreso",
                            "message": "El scheduler ya esta ejecutando un ciclo."
                        })
                        continue

                    async with self._learning_lock:
                        self._learning_paused.set()
                        try:
                            if self._learner is None:
                                self._learner = AutoLearner(
                                    graph_client=self.graph,
                                    router=self._llm_router,
                                )
                            success, patterns, behavioral_counts, analysis_mode = await self._learner.run(limit=5)
                            if success:
                                await self.broker.push_task("queue:notifications", {
                                    "type": "learning_result",
                                    "icon": "\U0001f9e0",
                                    "title": "Auto-Aprendizaje Completado",
                                    "success": True,
                                    "sample_count": patterns.sample_count if patterns else 0,
                                    "greeting_patterns": patterns.greeting_patterns if patterns else [],
                                    "signoff_patterns": patterns.signoff_patterns if patterns else [],
                                    "dominant_tone": patterns.dominant_tone if patterns else "",
                                    "avg_length_chars": patterns.avg_length_chars if patterns else 0,
                                    "common_phrases": patterns.common_phrases if patterns else [],
                                    "analysis_mode": analysis_mode,
                                    "behavioral_counts": behavioral_counts,
                                })
                            else:
                                await self.broker.push_task("queue:notifications", {
                                    "type": "learning_result",
                                    "icon": "\u26a0\ufe0f",
                                    "title": "Auto-Aprendizaje Fallo",
                                    "success": False,
                                })
                        finally:
                            self._learning_paused.clear()

                else:
                    _json_log("action", f"Unknown command: {cmd_type}", {}, "WARNING")

            except asyncio.CancelledError:
                break
            except Exception as e:
                _json_log("action", f"Command listener error: {e}", {}, "ERROR")
                await asyncio.sleep(5)

    # ──────────────────────────────────────────────
    #  Triage loop (principal)
    # ──────────────────────────────────────────────

    async def _triage_loop(self):
        """Ciclo principal de triage.

        SIN Playwright. SIN Chromium. SIN OWA REST API.
        TODO via Microsoft Graph API (HTTP Bearer).
        """
        loop_counter = 0

        while True:
            try:
                if self._learning_paused.is_set():
                    _json_log("tactical", "Auto-learning active, triage waiting", {
                        "action": "triage_paused"
                    })
                    await self._learning_paused.wait()
                    await asyncio.sleep(1)
                    continue

                await self.broker.client.set("heartbeat:vision", time.time(), ex=120)

                # ── Verificar que el token siga siendo valido ──
                # Client Credentials: la renovacion es desatendida (MSAL).
                token_ok = await self.auth.is_authenticated() if self.auth else False
                if not token_ok:
                    _json_log("tactical", "Token no disponible, renovando via Client Credentials", {
                        "action": "triage_token_refresh"
                    }, "WARNING")
                    token = await self.auth.refresh_token() if self.auth else None
                    if not token:
                        _json_log("tactical",
                                  "Renovacion de token fallo, reintentando en 5 min",
                                  {"action": "triage_auth_failed"}, "ERROR")
                        await asyncio.sleep(300)
                        continue

                # ── Obtener correos no leidos via Graph API ──
                emails = await self.email_reader.read_unread_emails(limit=5)

                if not emails:
                    _json_log("tactical", "Inbox clean, no unread emails", {
                        "action": "inbox_empty"
                    })
                    self.processed_history.clear()

                    if not self._was_inbox_empty:
                        self._was_inbox_empty = True
                        self._trigger_inbox_clean.set()

                    await asyncio.sleep(60)
                    continue

                # Encontrar el primer correo no procesado
                target = next(
                    (e for e in emails if e["mail_id"] not in self.processed_history),
                    None
                )
                if not target:
                    await asyncio.sleep(30)
                    continue

                self._was_inbox_empty = False

                # Determinar si es hilo
                is_thread = bool(target.get("conversation_id", ""))
                _json_log("tactical", "Processing email via Graph API", {
                    "action": "graph_process",
                    "mail_id": target["mail_id"][-20:],
                    "sender": target["sender"][:60],
                    "subject": target.get("subject", "")[:60],
                    "is_thread": is_thread
                })

                if is_thread:
                    await self._process_thread(target)
                else:
                    await self._process_single(target)

                # GC periodico
                loop_counter += 1
                if loop_counter % 5 == 0:
                    _json_log("oom", "Periodic cleanup", {
                        "action": "periodic_gc",
                        "processed_count": loop_counter
                    })

                await asyncio.sleep(2)

            except Exception as e:
                _json_log("tactical", f"Triage loop error: {e}", {
                    "action": "triage_error",
                    "error": str(e)[:200]
                }, "ERROR")
                await asyncio.sleep(5)

    # ──────────────────────────────────────────────
    #  Orchestrador principal
    # ──────────────────────────────────────────────

    async def main_loop(self):
        """Arranca todos los loops concurrentes.

        Flujo de autenticacion (GraphAPIAuth, Client Credentials):
        1. GraphAPIAuth.get_token() -> adquiere token via MSAL (app-only)
        2. Token valido -> iniciar triage loop
        3. Si el token expira -> renovacion automatica desatendida
        """
        _json_log("tactical", f"VisionWorker starting for tenant: {self.tenant_id}", {"action": "vision_start", "tenant_id": self.tenant_id})
        _json_log("tactical", f"Cleaning residual Redis queues for tenant: {self.tenant_id}", {"action": "clean_slate", "tenant_id": self.tenant_id})
        await self.broker.client.delete(self.queue_in)
        await self.broker.client.delete(self.queue_out)
        await self.broker.client.delete("queue:notifications")
        await self.broker.client.delete("queue:user_decisions")
        await self.broker.client.delete("queue:commands")

        # ── Autenticacion via Graph API (Client Credentials, desatendida) ──
        _json_log("tactical", "GraphAPIAuth: adquiriendo token (Client Credentials)", {
            "action": "auth_client_credentials"
        })

        token = await self.auth.get_token() if self.auth else None

        if not token:
            _json_log("tactical",
                "CRITICAL: Autenticacion Graph API fallo. Triage deshabilitado.",
                {"action": "auth_failed"}, "CRITICAL")

            await self.broker.push_task("queue:notifications", {
                "type": "system_alert",
                "icon": "\u274c",
                "title": "CRITICAL: Autenticacion fallida",
                "message": (
                    "No se pudo autenticar con Microsoft Graph API (Client Credentials). "
                    "Verifique GRAPH_TENANT_ID, GRAPH_CLIENT_ID y el secret. "
                    "El triage de correos esta deshabilitado. "
                    "Usa /status para ver el estado y reinicia el contenedor."
                )
            })
            return  # Worker se detiene si no puede autenticar

        # ── Health check inicial de Graph API ──
        hc = await self.graph.health_check()
        _json_log("tactical", f"Graph API health: {hc.get('status')}", {
            "action": "graph_health_initial",
            "user": hc.get("user", ""),
            "email": hc.get("email", ""),
            "latency_ms": hc.get("latency_ms", 0)
        })

        # Notificar exito
        await self.broker.push_task("queue:notifications", {
            "type": "system_alert",
            "icon": "\u2705",
            "title": "JarvisMail listo",
            "message": (
                f"Autenticado como: {hc.get('user', 'desconocido')}\n"
                f"Email: {hc.get('email', 'desconocido')}\n"
                f"Estado: Triage de correos activo"
            )
        })

        # Health writer background task
        self._health_task = asyncio.create_task(self._health_writer())
        _json_log("tactical", "Graph API health writer started", {
            "action": "health_writer_started"
        })

        try:
            await asyncio.gather(
                self._triage_loop(),
                self._auto_learning_scheduler(interval_minutes=60),
                self._command_listener(),
            )
        finally:
            if self._health_task and not self._health_task.done():
                self._health_task.cancel()
            await self.graph.close()


async def run():
    from graph.graph_auth import GraphAPIAuth
    from graph.graph_client import GraphClient

    broker = RedisTaskBroker()
    retries = 0
    max_delay = 30

    while True:
        try:
            await broker.connect()
            _json_log("tactical", "Vision worker connected to Redis", {
                "action": "vision_redis_connected"
            })
            break
        except Exception as e:
            retries += 1
            delay = min(5 * retries, max_delay)
            _json_log("tactical", "Waiting for Redis", {
                "action": "redis_retry",
                "retry": retries,
                "delay": delay
            }, "WARNING")
            await asyncio.sleep(delay)

    auth = GraphAPIAuth()
    graph = GraphClient(auth)
    worker = VisionWorker(graph, broker)
    worker.auth = auth
    await worker.main_loop()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
