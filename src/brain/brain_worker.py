import os
import asyncio
import json
import logging
import re
import time
from shared.redis_client import RedisTaskBroker
from brain.llm_router_gateway import LLMRouterGateway
from brain.profile_updater import ProfileUpdater
from observability.tracer import AgentTracer
from observability.metrics import OperationalMetrics
from shared.utils import extract_clean_reason


logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("brain.core")

_metrics = OperationalMetrics()

def _json_log(event_type, message, data=None, level="INFO"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": "brain",
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }, ensure_ascii=False))

class BrainWorker:
    def __init__(self) -> None:
        self.broker = RedisTaskBroker()
        self.router = LLMRouterGateway(self.broker)
        self._learning_paused = asyncio.Event()
        self.profile_updater = ProfileUpdater(self.broker, self.router, learning_paused=self._learning_paused)
        # Multi-tenant: usar TENANT_ID si está configurado, sino 'default'
        self.tenant_id = os.environ.get("TENANT_ID", "default")
        self.queue_in = f"queue:{self.tenant_id}:raw_emails"
        self.queue_out = f"queue:{self.tenant_id}:vision_tasks"

    async def _perform_preflight(self) -> bool:
        await self.broker.connect()
        _json_log("tactical", "Redis connection established", {
            "action": "redis_connected",
            "status": "connected"
        })
        while True:
            provider = await self.broker.client.get("hive:config:provider")
            model = await self.broker.client.get("hive:config:model")
            if provider and model:
                # v6.0: El gateway selecciona el modelo por intención; el
                # override de hive:config:* solo ajusta el modelo por defecto.
                self.router.engine = provider
                AgentTracer.trace_preflight_check({
                    "provider": provider,
                    "model": model,
                    "status": "operational"
                })
                return True
            print("FALTA CONFIGURACION. Ejecuta: docker exec -it jarvis_brain python -m src.brain.cli")
            await asyncio.sleep(10)

    async def run(self) -> None:
        try:
            if not await self._perform_preflight():
                return

            _json_log("tactical", "Initializing ProfileUpdater background tasks", {
                "action": "profile_updater_boot",
                "collect_interval_seconds": 3.0,
                "analyze_interval_seconds": 120.0
            })

            # Intervalos corregidos:
            # - Collect: cada 30s (era 3s, demasiado agresivo)
            # - Analyze: cada 10800s = 3 horas (era 120s, causaba race condition con triage)
            async def profile_collect_wrapper():
                await self.profile_updater._collect_loop(interval=30.0)

            async def profile_analyze_wrapper():
                await self.profile_updater._analyze_loop(interval=10800.0)

            def _run_guardrails(data: dict) -> dict | None:
                content = data.get("dom_content", data.get("snippet", ""))
                mail_id = data.get("mail_id", "unknown")
                sender = data.get("sender", "")
                subject = data.get("subject", "")

                if len(content) > 100000:
                    AgentTracer.log_guardrail_block("max_size_exceeded", mail_id, sender, subject, f"Content exceeds 100KB ({len(content)} chars)")
                    return {"decision": "Noise", "reasoning": "Guardrail: content exceeds maximum allowed size", "guardrail": "max_size_exceeded"}

                pii_patterns = [
                    (r'\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b', "credit_card"),
                    (r'\b\d{3}-\d{2}-\d{4}\b', "ssn"),
                    (r'\b(?:4[0-9]{12}(?:[0-9]{3})?|5[1-5][0-9]{14}|3[47][0-9]{13})\b', "credit_card2"),
                ]
                for pattern, rule_name in pii_patterns:
                    if re.search(pattern, content):
                        AgentTracer.log_guardrail_block(rule_name, mail_id, sender, subject, f"PII detected: {rule_name}")
                        return {"decision": "Noise", "reasoning": f"Guardrail: PII detected ({rule_name})", "guardrail": rule_name}

                injection_patterns = [
                    r'ignore\s+(all\s+)?(previous|prior|above)\s+instructions',
                    r'system\s+prompt',
                    r'you\s+are\s+(now|actually)\s+',
                    r'forget\s+(about\s+)?your\s+(instructions|prompt|rules)',
                ]
                for pattern in injection_patterns:
                    if re.search(pattern, content, re.IGNORECASE):
                        AgentTracer.log_guardrail_block("prompt_injection", mail_id, sender, subject, f"Prompt injection pattern detected")
                        return {"decision": "Noise", "reasoning": "Guardrail: prompt injection attempt detected", "guardrail": "prompt_injection"}

                return None

            def _extract_subject_from_headers(content_text):
                """Extrae el subject real desde los headers del correo (Asunto:/Subject:)."""
                for line in content_text.split('\n'):
                    m = re.match(r'(?:Asunto|Subject)\s*[:]\s*(.+)', line.strip(), re.IGNORECASE)
                    if m:
                        return m.group(1).strip()
                return None

            async def triage_loop():
                while True:
                    try:
                        # ── Respetar pausa de auto-aprendizaje ──
                        if self._learning_paused.is_set():
                            _json_log("tactical", "Auto-learning in progress, triage waiting", {
                                "action": "triage_paused_profile_update"
                            })
                            await self._learning_paused.wait()
                            await asyncio.sleep(1)
                            continue

                        data = await self.broker.get_task(self.queue_in)
                        if not data:
                            continue

                        mail_id = data.get("mail_id", "unknown")
                        sender = data.get("sender", "")
                        subject = data.get("subject", "")
                        AgentTracer.trace_triage_progress("triage_started", mail_id, {
                            "sender": sender,
                            "subject": subject
                        })

                        guardrail_result = _run_guardrails(data)
                        if guardrail_result:
                            AgentTracer.trace_evaluation({
                                "mail_id": mail_id,
                                "decision": guardrail_result.get("decision"),
                                "sender": sender,
                                "subject": subject,
                                "reason": extract_clean_reason(guardrail_result.get("reasoning", ""))
                            })
                            _json_log("tactical", "Email blocked by guardrails", {
                                "action": "guardrail_blocked",
                                "mail_id": mail_id,
                                "rule": guardrail_result.get("guardrail"),
                                "sender": sender
                            })
                            await self.broker.push_task(self.queue_out, guardrail_result)

                            # ── Salud del LLM Router Gateway ──
                            # El gateway es embebido: si la API OpenAI falla,
                            # el triage fallará y entrará en reintentos/HITL.
                            _json_log("tactical", "Guardrail passed, awaiting gateway triage", {
                                "action": "guardrail_passed_awaiting_router",
                                "engine": self.router.engine,
                                "mail_id": mail_id
                            })

                            continue

                        router_retries = 0
                        while True:
                            analysis = await self.router.triage(data)

                            if "error" in analysis:
                                router_retries = router_retries + 1

                                _json_log("tactical", "Infrastructure failure in triage", {
                                    "action": "triage_failed",
                                    "engine": analysis.get("engine"),
                                    "error": analysis.get("details", "")[:200],
                                    "attempt": router_retries,
                                    "max_attempts": 3
                                }, "ERROR")

                                if router_retries >= 3:
                                    # 3 intentos fallidos consecutivos → notificar al operador
                                    _json_log("tactical", "Max retries reached, notifying operator", {
                                        "action": "triage_retry_exhausted",
                                        "mail_id": mail_id
                                    })
                                    await self.broker.push_task("queue:notifications", {
                                        "type": "system_alert",
                                        "icon": "⚠️",
                                        "title": "LLM Router Gateway no disponible",
                                        "message": (
                                            "El LLM Router Gateway no respondió tras "
                                            "3 intentos consecutivos. El correo será "
                                            "reintentado más tarde.\n\n"
                                            "💡 Verifica OPENAI_API_KEY y OPENAI_BASE_URL "
                                            "en el archivo .env."
                                        )
                                    })
                                    await self.broker.push_task(self.queue_in, data)
                                    break
                                
                                _json_log("tactical", "Router unavailable, retrying in 60s", {
                                    "action": "triage_retry_backoff",
                                    "mail_id": mail_id,
                                    "attempt": router_retries,
                                    "max_attempts": 3
                                })
                                await asyncio.sleep(60)
                                continue

                            # ── GUARDRAIL POST-LLM: validar sender contra alucinaciones ──
                            #
                            # El LLM a veces alucina el sender extrayendo texto del CUERPO del
                            # mensaje cuando no encuentra una línea 'De:' explícita.
                            #
                            # Estrategia de 3 niveles:
                            # N1: Si el sender está VACÍO → usar sender_hint original
                            # N2: Si el sender APARECE en el body content (y NO es 'De:' header)
                            #     → es una alucinación, usar sender_hint original
                            # N3: Si incluso sender_hint es body text → extraer del subject
                            #
                            brain_sender = analysis.get("sender", "")
                            original_sender = data.get("sender", "")
                            dom_content = data.get("dom_content", data.get("snippet", ""))
                            subject = data.get("subject", "")

                            def _looks_like_body_text(s, content):
                                """True si 's' aparece en 'content' y NO está precedido por De:/From:"""
                                s_lower = s.lower().strip()
                                content_lower = content.lower()
                                if not s_lower or len(s_lower) <= 5:
                                    return False
                                if s_lower not in content_lower:
                                    return False
                                de_pattern = re.compile(
                                    r'(?:de|from)\s*[:]\s*' + re.escape(s_lower),
                                    re.IGNORECASE
                                )
                                return not bool(de_pattern.search(content_lower))

                            def _extract_sender_from_subject(subj):
                                """Extrae posible sender del inicio del subject.

                                Salta prefijos de sistema como [Borrador], Mencionado,
                                Re:, Fwd:, etc. para encontrar el primer nombre real.
                                """
                                if not subj:
                                    return "Remitente Desconocido"
                                raw_parts = subj.strip().split()
                                # Filtrar prefijos de sistema antes de buscar nombres
                                parts = []
                                for p in raw_parts:
                                    if re.match(r'^\[', p):  # [Borrador], [Fwd], etc.
                                        continue
                                    if re.match(r'^[A-Za-z]+[:]$', p):  # Re:, Fwd:, Rv:, etc.
                                        continue
                                    if p.lower() in ('mencionado', 'borrador', 'reenviado', 'responder'):
                                        continue
                                    parts.append(p)
                                if not parts:
                                    return "Remitente Desconocido"
                                name_parts = []
                                for p in parts:
                                    if re.match(r'^[A-ZÁÉÍÓÚÑ][a-záéíóúñ]', p):
                                        # ⚠️ NO extraer palabras genéricas como "Validación" o "Reporte"
                                        # aunque empiecen con mayúscula y tengan acentos.
                                        if p.lower() in _SPANISH_FALSE_NAMES:
                                            break
                                        name_parts.append(p)
                                        if len(name_parts) >= 3:
                                            break
                                    else:
                                        break
                                if name_parts:
                                    return ' '.join(name_parts)
                                return parts[0] if parts else "Remitente Desconocido"

                            # N1: Empty sender guardrail
                            if not brain_sender:
                                analysis["sender"] = original_sender
                                AgentTracer.trace_sender_guardrail(
                                    "sender_guardrail_empty", mail_id,
                                    {"original_sender": original_sender}
                                )

                            # N2: Anti-hallucination guardrail
                            #
                            # OJO: Solo dispara cuando el sender_hint ORIGINAL (del DOM/panel)
                            # es un nombre de persona VALIDO. Si el hint original es un label
                            # de sistema (ej: "Solución Caso IM26-390076"), NO rechazar el sender
                            # del LLM aunque aparezca en el body — porque en emails de sistema,
                            # el humano real SOLO aparece en el body (no en headers).
                            #
                            # Esto previene que N2 rechace "Elbert Yoang" (extraído por el LLM
                            # del body) y lo reemplace con "Solución Caso IM26-390076" (hint
                            # incorrecto del sistema de tickets).
                            elif brain_sender and dom_content and original_sender:
                                # Solo corregir si el sender_hint original es VALIDO
                                # (pasa is_valid → es nombre de persona real)
                                from vision.sender_extractor import SenderExtractor
                                original_is_valid = original_sender and SenderExtractor.is_valid(original_sender, subject)

                                if original_is_valid and _looks_like_body_text(brain_sender, dom_content):
                                    AgentTracer.trace_sender_guardrail(
                                        "sender_guardrail_hallucination", mail_id,
                                        {"brain_sender": brain_sender[:80], "original_sender": original_sender},
                                        "WARNING"
                                    )
                                    analysis["sender"] = original_sender
                                elif not original_is_valid:
                                    # sender_hint es label de sistema → confiar en LLM
                                    _json_log("tactical", "Guardrail N2: hint is system label, trusting LLM sender", {
                                        "action": "sender_guardrail_system_label_trust_llm",
                                        "mail_id": mail_id,
                                        "original_sender": original_sender[:60],
                                        "brain_sender": brain_sender[:60]
                                    })

                            # N3: Si el sender_hint está vacío (DOM rechazó), extraer del subject.
                            #
                            # Cuando looksLikeRealName rechaza un nombre de sistema/proyecto
                            # (ej: "Reporte Audiovillas Oficinas"), el sender queda vacío.
                            # N3 extrae el sender REAL desde el inicio del subject
                            # ("Maria Cristina Santofimio Trujillo - Reunión:" → "Maria Cristina Santofimio").
                            #
                            # OJO: Solo dispara cuando NO hay sender del DOM. NO checkea body text
                            # porque final_sender ya viene del DOM y no tiene sentido validarlo
                            # contra el body (nombres reales aparecen en la conversación).
                            final_sender = analysis.get("sender", "")
                            if not final_sender:
                                subject_sender = _extract_sender_from_subject(subject)
                                AgentTracer.trace_sender_guardrail(
                                    "sender_guardrail_subject", mail_id,
                                    {
                                        "final_sender": final_sender[:80] if final_sender else "(empty)",
                                        "subject_sender": subject_sender
                                    },
                                    "WARNING"
                                )
                                analysis["sender"] = subject_sender

                            # ── N8: Guardrail para SENDER que es label de sistema ──
                            #
                            # Detecta cuando TODAS las fuentes devuelven un label de sistema/ticket
                            # (ej: "Solución Caso IM26-390076", "Seguimiento Proyectos de TI 22-04-2026")
                            # en vez de un nombre humano real.
                            #
                            # Estrategia (en orden):
                            # 1. Extraer nombre humano del subject (si el subject tiene un nombre real)
                            # 2. Si no, buscar en body content ("solicitado por:", "usuario:", etc.)
                            # 3. Si nada funciona, al menos limpiar fechas del sender
                            #
                            n8_triggered = False
                            final_sender_n8 = analysis.get("sender", "")
                            if final_sender_n8 and subject:
                                from vision.sender_extractor import SenderExtractor
                                if not SenderExtractor.is_valid(final_sender_n8, subject):
                                    AgentTracer.trace_sender_guardrail(
                                        "sender_guardrail_system_label", mail_id,
                                        {"sender": final_sender_n8[:60], "subject": subject[:80]},
                                        "WARNING"
                                    )
                                    _json_log("tactical", "N8: sender is system label, extracting from subject", {
                                        "action": "sender_guardrail_n8",
                                        "mail_id": mail_id,
                                        "sender": final_sender_n8[:60],
                                        "subject": subject[:80]
                                    })

                                    # E1: Extraer nombre humano del subject (limpiando fecha primero)
                    
                                    clean_subject = _DATE_SUFFIX_RE.sub('', subject).strip()
                                    subject_name = _extract_sender_from_subject(clean_subject)
                                    # Validar que el nombre extraído sea razonable (no palabra suelta genérica)
                                    if subject_name and subject_name != "Remitente Desconocido":
                                        lower_name = subject_name.lower().strip()
                                        is_generic = (
                                            lower_name in _SPANISH_FALSE_NAMES
                                            or (len(subject_name.split()) == 1 and len(subject_name) < 8)
                                        )
                                        if not is_generic:
                                            analysis["sender"] = subject_name
                                            n8_triggered = True

                                    # E2: Fallback a body content
                                    if not n8_triggered and dom_content:
                                        name_match = re.search(
                                            r'(?:solicitado por|solicita|usuario|empleado|funcionario|colaborador|responsable|contacto|asignado a)[:\s]+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,3})',
                                            dom_content[:2000],
                                            re.IGNORECASE
                                        )
                                        if name_match:
                                            analysis["sender"] = name_match.group(1).strip()
                                            n8_triggered = True

                                    # E3: Al menos limpiar fechas del sender como último recurso
                                    if not n8_triggered:
                                        cleaned = _DATE_SUFFIX_RE.sub('', final_sender_n8).strip()
                                        if cleaned and cleaned != final_sender_n8:
                                            _json_log("tactical", "N8 E3: stripped date from sender", {
                                                "action": "sender_strip_date",
                                                "mail_id": mail_id,
                                                "before": final_sender_n8[:60],
                                                "after": cleaned[:60]
                                            })
                                            analysis["sender"] = cleaned
                                            n8_triggered = True

                            # ── N6: Guardrail para SENDER que parece body text ──
                            #
                            # El LLM a veces extrae un saludo o frase del CUERPO del correo
                            # como sender cuando no encuentra 'De:' explícito y sender_hint
                            # está vacío. Esto produce senders como:
                            #   "Buenos días m, Juan. Aprobadas. Saludos."
                            #   "Cordialmente, Maria Perez"
                            #
                            # Estrategia:
                            # Detecta patrones de saludo/firma en español al inicio del sender.
                            #
                            n6_triggered = False
                            final_sender_n6 = analysis.get("sender", "")
                            if final_sender_n6:
                                sender_stripped = final_sender_n6.strip()
                                sender_lower_check = sender_stripped.lower()

                                # Patrones de saludo que indican body text
                                # NOTA: NO incluimos ^para\s+ porque es demasiado agresivo y
                                # puede marcar nombres reales extraídos desde el subject (N3).
                                _GREETING_PATTERNS = [
                                    r'^buenos\s+d[ií]as',
                                    r'^buenas\s+tardes',
                                    r'^buenas\s+noches',
                                    r'^cordial\s+(saludo|mente)',
                                    r'^atentamente',
                                    r'^saludos?\s+(cordiales?|desde)',
                                    r'^hola\s+',
                                    r'^estimados?\s+(todos|compañeros|señores|colegas)',
                                    r'^reciban\s+un\s+(cordial|afectuoso)',
                                    r'^muchas\s+gracias',
                                    r'^quedo\s+atento',
                                    r'^quedamos\s+atentos',
                                    r'^feliz\s+(d[ií]a|tarde|noche|semana)',
                                    r'^sr\.?\s+',
                                    r'^sra\.?\s+',
                                ]
                                for pattern in _GREETING_PATTERNS:
                                    if re.match(pattern, sender_lower_check):
                                        AgentTracer.trace_sender_guardrail(
                                            "sender_guardrail_body_text_greeting", mail_id,
                                            {"brain_sender": final_sender_n6[:80], "original_sender": original_sender, "pattern": pattern},
                                            "WARNING"
                                        )
                                        analysis["sender"] = original_sender
                                        n6_triggered = True
                                        break

                                # Si el sender parece una oración completa (múltiples verbos, puntos)
                                if not n6_triggered and len(sender_stripped) > 30:
                                    # Detectar patrones de oración: verbo conjugado + coma + siguiente oración
                                    sentence_patterns = [
                                        r'(?:aprobadas?|revisadas?|confirmadas?)\s+\.?\s',
                                        r'(?:me\s+)?comunico\s+',
                                        r'gracias\s+por\s+',
                                    ]
                                    for pattern in sentence_patterns:
                                        if re.search(pattern, sender_lower_check):
                                            AgentTracer.trace_sender_guardrail(
                                                "sender_guardrail_sentence", mail_id,
                                                {"brain_sender": final_sender_n6[:80], "original_sender": original_sender, "pattern": pattern},
                                                "WARNING"
                                            )
                                            analysis["sender"] = original_sender
                                            n6_triggered = True
                                            break

                                # Si el sender está en mayúscula/minúscula mezclada y tiene palabras
                                # de uso común en español que NO son nombres
                                if not n6_triggered and len(sender_stripped) > 40:
                                    # Palabras de alta frecuencia en español que no son nombres
                                    _SPANISH_COMMON_WORDS = {
                                        'el', 'la', 'los', 'las', 'de', 'del', 'en', 'un', 'una',
                                        'con', 'por', 'para', 'y', 'e', 'o', 'a', 'su', 'que',
                                        'se', 'no', 'lo', 'como', 'más', 'mas', 'pero', 'es',
                                    }
                                    words_in_sender = set(sender_lower_check.split())
                                    common_count = len(words_in_sender & _SPANISH_COMMON_WORDS)
                                    if common_count >= 3:
                                        AgentTracer.trace_sender_guardrail(
                                            "sender_guardrail_common_words", mail_id,
                                            {"brain_sender": final_sender_n6[:80], "original_sender": original_sender, "common_count": common_count},
                                            "WARNING"
                                        )
                                        analysis["sender"] = original_sender
                                        n6_triggered = True

                            # ── N7: Guardrail para SENDER que es una dirección de email ──
                            #
                            # El LLM a veces extrae el email address como sender cuando no
                            # encuentra un nombre humano en los encabezados.
                            # El prompt dice explícitamente "Debe ser el NOMBRE, NO la direccion de email"
                            # pero a veces el LLM ignora esta instrucción.
                            #
                            final_sender_n7 = analysis.get("sender", "")
                            if final_sender_n7 and not n6_triggered:
                                if '@' in final_sender_n7:
                                    AgentTracer.trace_sender_guardrail(
                                        "sender_guardrail_email", mail_id,
                                        {"brain_sender": final_sender_n7[:80], "original_sender": original_sender},
                                        "WARNING"
                                    )
                                    # Si el sender_hint original también es un email, no podemos
                                    # reemplazar con él (es el mismo valor). Extraer del DOM/body.
                                    if '@' not in original_sender:
                                        analysis["sender"] = original_sender
                                    elif dom_content:
                                        # Buscar nombre real antes del email en el contenido
                                        # Patrón: "Nombre Apellido" <email>
                                        email_name_match = re.search(
                                            r'["\u201C]?(?P<name>[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,3})["\u201D]?\s*<' + re.escape(final_sender_n7),
                                            dom_content
                                        )
                                        if not email_name_match:
                                            # Patrón más flexible: texto antes del <email>
                                            email_name_match = re.search(
                                                r'(?P<name>[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,3})\s*<' + re.escape(final_sender_n7.split('@')[0]),
                                                dom_content
                                            )
                                        if email_name_match:
                                            analysis["sender"] = email_name_match.group('name').strip()
                                            _json_log("tactical", "Guardrail N7: extracted name from email context", {
                                                "action": "sender_guardrail_email_name",
                                                "mail_id": mail_id,
                                                "email_sender": final_sender_n7[:60],
                                                "extracted_name": analysis["sender"]
                                            })
                                        else:
                                            # Si no encontramos nombre, usar el email truncado (antes del @)
                                            local_part = final_sender_n7.split('@')[0]
                                            # Limpiar puntos y guiones para que se vea como nombre
                                            clean_local = local_part.replace('.', ' ').replace('_', ' ').replace('-', ' ').title()
                                            if len(clean_local) > 3:
                                                analysis["sender"] = clean_local
                                                _json_log("tactical", "Guardrail N7: using local part of email as sender", {
                                                    "action": "sender_guardrail_email_local",
                                                    "mail_id": mail_id,
                                                    "email_sender": final_sender_n7[:60],
                                                    "clean_local": clean_local
                                                })

                            # ── N5: Guardrail para SENDER contra firmas/disclaimers ──
                            #
                            # El LLM a veces extrae sender desde el CUERPO del correo cuando
                            # no encuentra un 'De:' explícito. Esto produce senders que son
                            # en realidad firmas, disclaimers legales o texto de cuerpo.
                            #
                            # Estrategia:
                            # N5a: Si sender > 100 chars → es firma/signature, usar original
                            # N5b: Si sender contiene patrones de disclaimer legal → rechazar
                            # N5c: Si sender contiene punto y coma con múltiples nombres → rechazar
                            # N5d: Si sender contiene palabras de signature (Cordialmente, Atentamente, etc.) → rechazar
                            #
                            final_sender = analysis.get("sender", "")
                            if final_sender:
                                sender_lower = final_sender.lower().strip()
                                n5_triggered = False

                                # N5a: Longitud excesiva → firma
                                if len(final_sender) > 100:
                                    AgentTracer.trace_sender_guardrail(
                                        "sender_guardrail_too_long", mail_id,
                                        {"brain_sender": final_sender[:100], "original_sender": original_sender},
                                        "WARNING"
                                    )
                                    analysis["sender"] = original_sender
                                    n5_triggered = True

                                # N5b: Disclaimer patterns
                                if not n5_triggered:
                                    for pattern in _DISCLAIMER_PATTERNS:
                                        if pattern in sender_lower:
                                            AgentTracer.trace_sender_guardrail(
                                                "sender_guardrail_disclaimer", mail_id,
                                                {"pattern": pattern, "brain_sender": final_sender[:100], "original_sender": original_sender},
                                                "WARNING"
                                            )
                                            analysis["sender"] = original_sender
                                            n5_triggered = True
                                            break

                                # N5c: Punto y coma con múltiples nombres
                                if not n5_triggered and ";" in final_sender and len(final_sender) > 30:
                                    analysis["sender"] = original_sender
                                    AgentTracer.trace_sender_guardrail(
                                        "sender_guardrail_multiple_names", mail_id,
                                        {"brain_sender": final_sender[:100], "original_sender": original_sender},
                                        "WARNING"
                                    )
                                    n5_triggered = True

                                # N5d: Signature keywords + larga
                                if not n5_triggered and len(final_sender) > 60:
                                    sig_match_count = sum(1 for kw in _SIGNATURE_KEYWORDS if kw in sender_lower)
                                    if sig_match_count >= 2:
                                        analysis["sender"] = original_sender
                                        AgentTracer.trace_sender_guardrail(
                                            "sender_guardrail_signature_kw", mail_id,
                                            {"sig_match_count": sig_match_count, "brain_sender": final_sender[:100], "original_sender": original_sender},
                                            "WARNING"
                                    )
                                        n5_triggered = True

                            # ── N4a: Limpiar prefijos de sistema en subject ──
                            #
                            # El DOM de Outlook a veces extrae preview text como subject,
                            # incluyendo prefijos de sistema como "Borrador ", "Mencionado ",
                            # o combinaciones de nombre+ticket.
                            #
                            # Estrategia:
                            # 1. Detectar prefijos de sistema al inicio del subject
                            # 2. Extraer el subject REAL de los headers del correo (si existe)
                            # 3. Si los headers no tienen subject, limpiar el subject DOM
                            #
                            original_subject = data.get("subject", "")
                            # ── N4b: Guardrail para SUBJECT contra body text ──
                            #
                            # El subject extraído por el DOM de Outlook a veces contiene
                            # texto del preview del cuerpo (body text) en vez del asunto real.
                            #
                            # IMPORTANTE: Cuando subject_source es "graph_api", el subject
                            # viene DIRECTAMENTE de Microsoft Graph API (campo subject del
                            # JSON estructurado) y es AUTORITATIVO. NO aplicar guardrail.
                            # El guardrail N4b solo aplica cuando el subject viene de
                            # extracción DOM (PWA/OWA), que puede fallar.
                            #
                            subject_source = data.get("subject_source", "")
                            if original_subject and dom_content and subject_source != "graph_api":
                                # ── Limpiar prefijos de sistema ──
                                cleaned_subject = original_subject
                                cleaned_subject = re.sub(r'^borrador\s+', '', cleaned_subject, flags=re.IGNORECASE).strip()
                                cleaned_subject = re.sub(r'^mencionado\s+', '', cleaned_subject, flags=re.IGNORECASE).strip()
                                cleaned_subject = re.sub(r'^reenviado\s*[:]?\s*', '', cleaned_subject, flags=re.IGNORECASE).strip()
                                cleaned_subject = re.sub(r'^responder\s*[:]?\s*', '', cleaned_subject, flags=re.IGNORECASE).strip()

                                if cleaned_subject != original_subject:
                                    _json_log("tactical", "Guardrail N4a: stripped system prefix from subject", {
                                        "action": "subject_strip_prefix",
                                        "mail_id": mail_id,
                                        "original": original_subject[:80],
                                        "cleaned": cleaned_subject[:80]
                                    })
                                    data["subject"] = cleaned_subject
                                    subject = cleaned_subject
                                    original_subject = cleaned_subject

                                # N4b Solo para subjects de DOM (no Graph API)
                                headers_subject = _extract_subject_from_headers(dom_content)
                                if headers_subject:
                                    if headers_subject != original_subject:
                                        _json_log("tactical", "Guardrail N4b-i: using subject from headers (authoritative)", {
                                            "action": "subject_guardrail_headers_authoritative",
                                            "mail_id": mail_id,
                                            "original_subject": original_subject[:80],
                                            "headers_subject": headers_subject[:80]
                                        })
                                        data["subject"] = headers_subject
                                        subject = headers_subject
                                elif _is_likely_body_text_subject(original_subject):
                                    _json_log("tactical", "Guardrail N4b-ii: DOM subject looks like body text, marking unknown", {
                                        "action": "subject_guardrail_body_text",
                                        "mail_id": mail_id,
                                        "original_subject": original_subject[:80],
                                        "reason": "subject appears to be body text, no headers found"
                                    }, "WARNING")
                                    data["subject"] = "Sin Asunto"
                                    subject = "Sin Asunto"
                                else:
                                    _json_log("tactical", "Guardrail N4b-iii: DOM subject is valid, keeping it", {
                                        "action": "subject_guardrail_keep",
                                        "mail_id": mail_id,
                                        "original_subject": original_subject[:80],
                                        "reason": "subject is short/valid, keeping DOM value"
                                    })

                            # ── N9: Post-LLM Subject Hallucination Guardrail ──
                            #
                            # VA DESPUES DE N4 porque N4 ya corrigió el subject (prefijos,
                            # headers autoritativos, body text → "Sin Asunto").
                            # N9 solo detecta si el LLM alucinó un asunto diferente en
                            # su razonamiento vs el subject YA CORREGIDO por N4.
                            #
                            # Estrategia:
                            # 1. Extraer el subject del razonamiento del LLM (Asunto: ...)
                            # 2. Comparar contra el subject YA CORREGIDO por N4
                            # 3. Si difieren significativamente, INYECTAR el subject correcto
                            #
                            reasoning_text = analysis.get("reasoning", "")
                            original_subj = data.get("subject", "")  # subject YA corregido por N4
                            if reasoning_text and original_subj:
                                # Extraer el asunto que el LLM puso en su razonamiento
                                asunto_match = re.search(
                                    r'Asunto:\s*(.+?)\s*Urgencia:',
                                    reasoning_text,
                                    re.IGNORECASE | re.DOTALL
                                )
                                if asunto_match:
                                    llm_subject = asunto_match.group(1).strip()
                                    
                                    # Limpiar ambos para comparación
                                    clean_llm = re.sub(r'^(Re|Fwd|RV|RES|Enc|FW)[:\s]*', '', llm_subject, flags=re.IGNORECASE).strip().lower()
                                    clean_orig = re.sub(r'^(Re|Fwd|RV|RES|Enc|FW)[:\s]*', '', original_subj, flags=re.IGNORECASE).strip().lower()
                                    
                                    # Detectar alucinación: si el LLM puso un subject
                                    # completamente diferente (no coincide substring)
                                    is_hallucinated = (
                                        clean_llm not in clean_orig
                                        and clean_orig not in clean_llm
                                        and len(clean_llm) > 5
                                        and len(clean_orig) > 5
                                    )
                                    
                                    if is_hallucinated:
                                        AgentTracer.trace_sender_guardrail(
                                            "subject_guardrail_n9_hallucination", mail_id,
                                            {
                                                "llm_subject": llm_subject[:80],
                                                "original_subject": original_subj[:80]
                                            },
                                            "WARNING"
                                        )
                                        _json_log("tactical", "N9: LLM hallucinated subject, injecting correct one", {
                                            "action": "subject_guardrail_n9",
                                            "mail_id": mail_id,
                                            "llm_subject": llm_subject[:80],
                                            "original_subject": original_subj[:80]
                                        })
                                        # Reemplazar el asunto alucinado en el razonamiento
                                        analysis["reasoning"] = reasoning_text.replace(
                                            llm_subject,
                                            original_subj,
                                            1  # solo el primer match (Asunto:)
                                        )

                            # ── AGREGAR trace_evaluation SIEMPRE para Loki/Grafana ──
                            # El path guardrail ya tiene su propio trace_evaluation arriba.
                            # Este es para el path principal (con LLM).
                            AgentTracer.trace_evaluation({
                                "mail_id": mail_id,
                                "decision": analysis.get("decision"),
                                "sender": analysis.get("sender"),
                                "subject": subject,
                                "reason": extract_clean_reason(analysis.get("reasoning", ""))
                            })

                            AgentTracer.trace_triage_progress("triage_completed", mail_id, {
                                "decision": analysis.get("decision")
                            })

                            # ── TRAZABILIDAD COMPLETA DEL PIPELINE ──
                            # Emitir un resumen completo de todas las etapas
                            # del procesamiento: guardrails, sender extraction,
                            # LLM call, y decisión final.
                            pipeline_steps = []
                            pipeline_steps.append({
                                "step": "Guardrails",
                                "action": "Pre-LLM validation",
                                "reasoning": "Validaciones: tamaño, PII, injection, sender N1-N7, subject N4",
                            })
                            pipeline_steps.append({
                                "step": "LLM Triage",
                                "action": f"Engine: {self.router.engine}",
                                "reasoning": analysis.get("reasoning", "")[:300],
                            })
                            if analysis.get("sender"):
                                pipeline_steps.append({
                                    "step": "Post-LLM Sender Guardrails",
                                    "action": f"Sender final: {analysis.get('sender', '')}",
                                    "reasoning": "Validaciones N1-N7 aplicadas al sender",
                                })
                            AgentTracer.trace_mail_pipeline(
                                mail_id=mail_id,
                                pipeline_steps=pipeline_steps,
                                final_decision={
                                    "decision": analysis.get("decision"),
                                    "sender": analysis.get("sender"),
                                    "confidence": analysis.get("reasoning", "").split("Confianza: ")[-1][:20] if "Confianza:" in analysis.get("reasoning", "") else "N/A"
                                }
                            )

                            await self.broker.push_task(self.queue_out, analysis)
                            break

                    except Exception as e:
                        AgentTracer.log_infrastructure_error("brain_loop", str(e))
                        try:
                            await self.broker.publish("queue:notifications", {
                                "message": f"JARVIS ERROR: {str(e)[:100]}"
                            })
                        except Exception:
                            pass
                        await asyncio.sleep(5)

            # ── Constantes para guardrails (hoisteadas fuera del loop por eficiencia) ──
            _SIGNATURE_KEYWORDS = [
                "cordialmente", "atentamente", "saludos", "saludo",
                "analista", "coordinador", "director", "gerente",
                "vicepresidencia", "vicepresidente", "presidencia",
                "telecomunicaciones", "telemática", "telematica",
                "operaciones", "tecnología", "tecnologia",
            ]
            _DISCLAIMER_PATTERNS = [
                "si usted sospecha", "aviso de confidencial", "confidencialidad",
                "este mensaje es", "este correo es", "procedencia y/o contenido",
                "la informacion contenida", "propiedad del remitente",
            ]
            # Palabras genéricas en español que NUNCA deben tratarse como nombre de persona
            # aunque empiecen con mayúscula. Previene que el N3 guardrail extraiga
            # "Validación", "Reporte", etc. como remitente desde el subject.
            _SPANISH_FALSE_NAMES = {
                "validación", "validacion", "reporte", "informe",
                "solicitud", "notificación", "notificacion",
                "sistema", "proyecto", "servicio", "gestión", "gestion",
                "aplicación", "aplicacion", "actualización", "actualizacion",
                "configuración", "configuracion", "implementación", "implementacion",
                "migración", "migracion", "integración", "integracion",
                "despliegue", "mantenimiento", "revisión", "revision",
                "monitoreo", "avance", "estado", "respuesta",
                "soporte", "incidente", "problema", "caso",
                # Listas de distribución / grupos / proyectos
                "seguimiento", "proyectos", "proyecto",
            }

            # Regex para limpiar fechas al FINAL del sender (ej: "Seguimiento Proyectos de TI 22-04-2026")
            _DATE_SUFFIX_RE = re.compile(
                r'\s+\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\s*$'
            )

            def _is_likely_body_text_subject(s: str) -> bool:
                """True si 's' es claramente body text, no un subject real."""
                if not s or len(s) < 2:
                    return True
                if len(s) > 80:
                    return True
                if "\n" in s:
                    return True
                if s.endswith('.') and len(s) > 60:
                    return True
                if (s.count(',') + s.count(';') + s.count(':')) > 3:
                    return True
                return False

            async def heartbeat_loop():
                while True:
                    await self.broker.client.set("heartbeat:brain", time.time(), ex=120)

                    # ── Estado de salud del LLM Router Gateway ──
                    # El gateway es embebido (sin microservicio externo): se
                    # publica su configuración efectiva. La clave Redis se
                    # conserva para compatibilidad con el panel de Grafana.
                    try:
                        gateway_health = json.dumps({
                            "status": "online" if self.router.api_key else "misconfigured",
                            "gateway": "llm-router-gateway",
                            "base_url": self.router.base_url,
                            "default_model": self.router.model,
                            "engine": self.router.engine,
                            "intent_models": dict(self.router._intent_models),
                            "interactions": self.router.interaction_count,
                        }, ensure_ascii=False)
                        await self.broker.client.set(
                            "hive:status:router_health",
                            gateway_health,
                            ex=180
                        )
                    except Exception:
                        try:
                            await self.broker.client.set(
                                "hive:status:router_health",
                                '{"status":"offline"}',
                                ex=180
                            )
                        except Exception:
                            pass

                    await asyncio.sleep(60)

            async def queue_monitor():
                thresholds = {
                    self.queue_in: 10,
                    self.queue_out: 10,
                    "queue:notifications": 5,
                }
                last_alert = {}
                while True:
                    await asyncio.sleep(300)
                    for q, threshold in thresholds.items():
                        length = await self.broker.client.llen(q)
                        if length > threshold:
                            last = last_alert.get(q, 0)
                            if time.time() - last > 600:
                                await self.broker.push_task("queue:notifications", {
                                    "type": "queue_backlog",
                                    "queue": q,
                                    "length": length,
                                    "threshold": threshold
                                })
                                last_alert[q] = time.time()

            await asyncio.gather(
                triage_loop(),
                profile_collect_wrapper(),
                profile_analyze_wrapper(),
                heartbeat_loop(),
                queue_monitor()
            )

        finally:
            if self.broker.client:
                await self.broker.client.aclose()

if __name__ == "__main__":
    worker = BrainWorker()
    asyncio.run(worker.run())
