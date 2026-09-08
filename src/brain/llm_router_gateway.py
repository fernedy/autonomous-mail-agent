# [src/brain/llm_router_gateway.py]
"""
LLM Router Gateway — Gateway único de acceso a LLM con detección de intención.

Reemplaza al LLM Router externo (microservicio standalone) por un gateway
embebido que:

  1. DETECTA LA INTENCIÓN de cada solicitud (triage de correo, acción de UI,
     análisis de perfil, análisis de aprendizaje, uso general).
  2. ELIGE EL MODELO automáticamente según la intención (cada intención puede
     mapear a un modelo distinto vía variables de entorno).
  3. SIEMPRE usa integración tipo API OpenAI (Chat Completions) configurada
     por variables de entorno — sin proveedores hardcodeados.

Variables de entorno:
  OPENAI_API_KEY             — API key del endpoint OpenAI-compatible (obligatoria)
  OPENAI_BASE_URL            — Base URL OpenAI-compatible
                               (default: https://api.openai.com/v1)
  LLM_GATEWAY_MODEL          — Modelo por defecto (default: gpt-4o-mini)
  LLM_GATEWAY_MODEL_TRIAGE   — Modelo para triage de correos
  LLM_GATEWAY_MODEL_UI       — Modelo para acciones de UI/DOM
  LLM_GATEWAY_MODEL_PROFILE  — Modelo para análisis de perfil/estilo
  LLM_GATEWAY_ENGINE         — Etiqueta del engine para telemetría
                               (default: openai)

Override en caliente: la clave Redis hive:config:model sobrescribe el modelo
por defecto (convención hive:config:* del proyecto).
"""

import os
import re
import json
import time
import logging
import traceback
from typing import Dict, Any, Optional
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from observability.tracer import AgentTracer

logger = logging.getLogger("brain.gateway")


def _json_log(event_type, message, data=None, level="INFO"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": "brain",
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }, ensure_ascii=False))


# ═══════════════════════════════════════════════
#  Modelos Pydantic para salida estructurada
# ═══════════════════════════════════════════════

class EmailDecision(BaseModel):
    decision: str = Field(description="Debe ser: 'Noise', 'Action', o 'Executive_Decision'")
    reasoning: str = Field(description="Justificacion tecnica breve en Espanol")
    sender: str = Field(description="REMITENTE = QUIEN ENVÍA el correo. Reglas ESTRICTAS: 1) Busca SOLO líneas 'De:', 'From:', 'Remitente:' al INICIO del mensaje. 2) Si encuentras 'De: Nombre', usa 'Nombre' exactamente. 3) IGNORA 'Para:', 'To:', 'CC:' — son DESTINATARIOS, no remitente. 4) CRÍTICO: NUNCA uses texto del CUERPO del mensaje como sender. 5) NUNCA uses el subject como sender. 6) Si NO hay línea 'De:' explícita, usa EXACTAMENTE el sender_hint que se pasa en las instrucciones. 7) REGLA DE ORO: si el texto que pondrías en 'sender' APARECE en el cuerpo del mensaje, está MAL — es body text, no remitente.")
    to_recipients: str = Field(description="Extrae los nombres de los DESTINATARIOS del correo desde las líneas 'Para:', 'To:', 'CC:', 'Cc:'. Lista separada por punto y coma (;). Si hay múltiples, incluye todos. Si solo hay un destinatario, pon solo ese nombre. NUNCA pongas el mismo valor de 'sender' aquí. Si no hay línea explícita, pon una cadena vacía.")
    draft: Optional[str] = Field(description="Borrador de respuesta o null")


class UIAction(BaseModel):
    action: str = Field(description="Accion: 'click', 'type', 'wait', o 'error'")
    selector: Optional[str] = Field(description="Selector CSS preciso para Playwright")
    reasoning: str = Field(description="Explicacion tecnica del elemento elegido")


# ═══════════════════════════════════════════════
#  LLMRouterGateway
# ═══════════════════════════════════════════════

class LLMRouterGateway:
    """Gateway de LLM con detección de intención y selección de modelo.

    Toda salida pasa por una única integración tipo API OpenAI
    (AsyncOpenAI + OPENAI_BASE_URL + OPENAI_API_KEY). El gateway:
      - detecta la intención de cada solicitud
      - selecciona el modelo configurado para esa intención
      - registra telemetría por intent

    Interface compatible con el antiguo LLMRouter: triage(data),
    analyze_profile(prompt), propiedades model/engine.
    """

    # Intenciones soportadas por el gateway
    INTENT_TRIAGE = "email_triage"
    INTENT_UI = "ui_action"
    INTENT_PROFILE = "profile_analysis"
    INTENT_GENERAL = "general"

    def __init__(self, broker=None):
        self.broker = broker
        self.interaction_count = 0

        # ── Integración tipo API OpenAI (100% por variables de entorno) ──
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        # Compat: heartbeat de brain_worker publica health desde esta URL
        self.router_url = self.base_url

        self._client = AsyncOpenAI(
            api_key=self.api_key or "missing-openai-api-key",
            base_url=self.base_url,
        )

        # ── Selección de modelo por intención ──
        default_model = os.getenv("LLM_GATEWAY_MODEL", "gpt-4o-mini")
        self._intent_models: Dict[str, str] = {
            self.INTENT_TRIAGE: os.getenv("LLM_GATEWAY_MODEL_TRIAGE", default_model),
            self.INTENT_UI: os.getenv("LLM_GATEWAY_MODEL_UI", default_model),
            self.INTENT_PROFILE: os.getenv("LLM_GATEWAY_MODEL_PROFILE", default_model),
            self.INTENT_GENERAL: default_model,
        }
        self.model = default_model
        self._engine = os.getenv("LLM_GATEWAY_ENGINE", "openai")

        _json_log("llm_gateway", "LLM Router Gateway initialized", {
            "action": "gateway_init",
            "base_url": self.base_url,
            "default_model": default_model,
            "intent_models": dict(self._intent_models),
            "api_key_configured": bool(self.api_key),
        })

    # ──────────────────────────────────────────────
    #  Detección de intención y selección de modelo
    # ──────────────────────────────────────────────

    def detect_intent(self, data: Dict[str, Any]) -> str:
        """Detecta la intención de una solicitud de triage.

        Reglas (deterministas, sin costo de LLM):
          - action != 'triage' Y contiene dom_content -> ui_action
          - action == 'triage' o contiene snippet/dominio de correo -> email_triage
          - caso contrario -> general
        """
        action = data.get("action")
        if action and action != "triage" and "dom_content" in data:
            return self.INTENT_UI
        if action == "triage" or "snippet" in data or "dom_content" in data:
            return self.INTENT_TRIAGE
        return self.INTENT_GENERAL

    def _select_model(self, intent: str, redis_override: Optional[str] = None) -> str:
        """Elige el modelo configurado para la intención.

        Prioridad: override en caliente (Redis hive:config:model, aplicado a
        email_triage/general) > variable de entorno por intención.
        """
        model = self._intent_models.get(intent, self.model)

        # v6.0: override manual en caliente (convención hive:config:*)
        # Solo aplica a triage/general; las intenciones especializadas
        # conservan su modelo dedicado de variable de entorno.
        if redis_override and intent in (self.INTENT_TRIAGE, self.INTENT_GENERAL):
            model = redis_override

        if "/" in model:
            self._engine = model.split("/")[0]
        else:
            self._engine = os.getenv("LLM_GATEWAY_ENGINE", "openai")
        self.model = model
        return model

    @property
    def engine(self) -> str:
        """Devuelve el engine activo (para telemetría y paneles)."""
        return self._engine

    @engine.setter
    def engine(self, value: str) -> None:
        """Setter para compatibilidad con brain_worker.py."""
        self._engine = value

    # ──────────────────────────────────────────────
    #  Manifests (SKILL.md + perfil aprendido)
    # ──────────────────────────────────────────────

    def _get_manifests(self):
        try:
            with open("/app/config/SKILL.md", "r") as f:
                s = f.read()
            with open("/app/config/LEARNED_PROFILE.md", "r") as f:
                p = f.read()
            max_skill_len = 8000
            max_profile_len = 6000
            if len(s) > max_skill_len:
                s = s[:max_skill_len] + "\n\n[...truncated...]"
            if len(p) > max_profile_len:
                p = p[:max_profile_len] + "\n\n[...perfil truncado, ver archivo completo para historial...]"
            behavioral_rules = ""
            behavioral_match = re.search(r'# BEHAVIORAL RULES.*?(?=\n# |\Z)', p, re.DOTALL)
            if behavioral_match:
                behavioral_rules = behavioral_match.group(0).strip()
                _json_log("tactical", "Behavioral rules loaded into prompt", {
                    "action": "behavioral_rules_loaded",
                    "behavioral_rules_length": len(behavioral_rules),
                    "impact": "calibrando_decision"
                }, "INFO")
                AgentTracer.trace_event("behavioral_rules", {
                    "action": "prompt_injected",
                    "rules_length": len(behavioral_rules),
                    "impact": "calibrando_decision"
                })
                return f"<context>{s}\n{p}</context>\n\n[IMPORTANTE: A continuacion las reglas de comportamiento aprendidas del usuario. USALAS para calibrar tu decision.]\n{behavioral_rules}"
            return f"<context>{s}\n{p}</context>"
        except Exception:
            return ""

    # ──────────────────────────────────────────────
    #  Triage (intención: email_triage / ui_action)
    # ──────────────────────────────────────────────

    async def triage(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Evalúa un correo y decide si es Noise, Action o Executive_Decision.

        El gateway detecta la intención, elige el modelo y envía el prompt
        por la integración OpenAI. Si falla, retorna infrastructure_failure
        para que brain_worker.py active HITL.
        """
        start = time.time()
        self.interaction_count += 1

        intent = self.detect_intent(data)

        # Override en caliente desde Redis (hive:config:model)
        redis_model = None
        if self.broker and intent in (self.INTENT_TRIAGE, self.INTENT_GENERAL):
            try:
                stored = await self.broker.client.get("hive:config:model")
                if stored:
                    model_raw = stored if isinstance(stored, str) else stored.decode()
                    if model_raw and model_raw != "auto":
                        redis_model = model_raw
            except Exception:
                pass

        model_to_use = self._select_model(intent, redis_override=redis_model)
        is_ui = (intent == self.INTENT_UI)
        prompt = self._build_prompt(data, is_ui)

        _json_log("tactical", "Gateway detected intent and selected model", {
            "action": "gateway_intent_selected",
            "intent": intent,
            "model": model_to_use,
            "prompt_length": len(prompt)
        })

        try:
            return await self._call_structured(prompt, is_ui, intent, model_to_use, start)
        except Exception as e:
            error_str = str(e)
            _json_log("tactical", "LLM Router Gateway call failed", {
                "action": "gateway_failed",
                "intent": intent,
                "model": model_to_use,
                "error": error_str[:200]
            }, "ERROR")
            return self._fallback_regex(error_str, start, self._engine)

    async def _call_structured(self, prompt: str, is_ui: bool, intent: str,
                               model_to_use: str, start: float) -> dict:
        """Envía el prompt con formato JSON estructurado por la API OpenAI.

        Args:
            prompt: El prompt completo para el LLM.
            is_ui: Si es UI action o triage de correo.
            intent: Intención detectada (para telemetría).
            model_to_use: Modelo seleccionado por el gateway.
            start: Timestamp de inicio para métricas.

        Raises:
            Exception: Si la API no responde o retorna contenido inválido.
        """
        schema_name = "UIAction" if is_ui else "EmailDecision"

        resp = await self._client.chat.completions.create(
            model=model_to_use,
            messages=[
                {"role": "system", "content": "You output JSON that strictly matches the schema."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=2048,
            timeout=90
        )

        resp_text = self._safe_extract_content(resp)
        if not resp_text:
            raise ValueError(f"Gateway returned empty response for {schema_name}")

        clean_text = self._extract_json(resp_text)
        if not clean_text or clean_text == "{}":
            raise ValueError(f"Gateway returned empty/invalid JSON: {resp_text[:200]}")

        result = json.loads(clean_text)
        p_tokens = resp.usage.prompt_tokens if resp.usage else 0
        c_tokens = resp.usage.completion_tokens if resp.usage else 0

        self._trace(result, model_to_use, start, is_ui, p_tokens, c_tokens,
                    f"llm_gateway_{intent}")
        return result

    # ──────────────────────────────────────────────
    #  Análisis de perfil (intención: profile_analysis)
    # ──────────────────────────────────────────────

    async def analyze_profile(self, prompt: str) -> str:
        """Analiza el perfil de usuario (texto plano, no JSON estructurado).

        Usado por ProfileUpdater y AutoLearner para extraer patrones de
        estilo de comunicación.

        Args:
            prompt: El prompt para analizar el perfil.

        Returns:
            Texto con el análisis, o "ERROR: ..." si falla.
        """
        _json_log("tactical", "Starting profile analysis via gateway", {
            "prompt_length": len(prompt),
            "action": "profile_analysis_start",
            "model": self.model
        })
        start = time.time()

        # Override en caliente desde Redis
        redis_model = None
        if self.broker:
            try:
                m = await self.broker.client.get("hive:config:model")
                if m:
                    model_raw = m if isinstance(m, str) else m.decode()
                    if model_raw and model_raw != "auto":
                        redis_model = model_raw
            except Exception:
                pass

        model_to_use = self._select_model(self.INTENT_PROFILE, redis_override=redis_model)

        try:
            resp = await self._client.chat.completions.create(
                model=model_to_use,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=8192,
                timeout=120
            )

            result = self._safe_extract_content(resp) or ""
            p_tokens = resp.usage.prompt_tokens if resp.usage else 0
            c_tokens = resp.usage.completion_tokens if resp.usage else 0

            AgentTracer.log_telemetry(
                f"{model_to_use}_profile", p_tokens, c_tokens,
                (time.time() - start) * 1000, "SUCCESS", "llm_gateway_profile", self._engine
            )

            _json_log("tactical", "Profile analysis completed via gateway", {
                "action": "profile_analysis_done",
                "model": model_to_use,
                "latency_ms": round((time.time() - start) * 1000, 2),
                "output_length": len(result)
            })
            return result

        except Exception as e:
            error_str = str(e)
            _json_log("tactical", "Profile analysis failed via gateway", {
                "action": "profile_analysis_failed",
                "model": model_to_use,
                "error": error_str[:300]
            }, "ERROR")
            traceback.print_exc()

            return f"ERROR: profile analysis failed - {model_to_use}: {error_str[:200]}"

    # ──────────────────────────────────────────────
    #  Construcción del prompt de triage
    # ──────────────────────────────────────────────

    def _build_prompt(self, data: Dict[str, Any], is_ui: bool) -> str:
        if is_ui:
            return f"Analiza este DOM y busca {data.get('goal')}: {data.get('dom_content')[:60000]}"

        content = data.get("dom_content", data.get("snippet", ""))
        sender_hint = data.get("sender", "")
        subject_hint = data.get("subject", "")[:200]
        subject_source = data.get("subject_source", "dom")
        sep = "\u2550" * 55

        # El subject proveniente de Graph API es AUTORITATIVO: el LLM NO debe
        # cuestionarlo ni reemplazarlo.
        if subject_source == "graph_api":
            subject_header = (
                f"{sep}\n"
                f"  GUARDRAILS OBLIGATORIOS - ASUNTO (subject)\n"
                f"{sep}\n"
                f"El asunto viene DIRECTAMENTE de Microsoft Graph API (AUTORITATIVO).\n"
                f"  Asunto real: '{subject_hint}'\n\n"
                f"[REGLA ESTRICTA]:\n"
                f"  NO modifiques, inventes ni reemplaces el asunto en tu razonamiento.\n"
                f"  USA EXACTAMENTE este asunto en el campo 'Asunto:' del razonamiento.\n"
                f"  Si el asunto parece tener prefijos (Re:, Fwd:, etc.), LIMPIALOS pero\n"
                f"  conserva el resto exactamente como está.\n"
                f"  NO extraigas un asunto diferente del body del correo.\n\n"
                f"[DETECCION DE URGENCIA EN ASUNTO]:\n"
                f"  Busca palabras de urgencia en el asunto REAL (arriba).\n"
                f"  Si hay indicador de urgencia -> INCLUYE en el razonamiento\n"
                f"  Si NO hay indicador -> NO inventes urgencia.\n"
                f"{sep}\n"
            )
        else:
            subject_header = (
                f"{sep}\n"
                f"  GUARDRAILS OBLIGATORIOS - ASUNTO (subject)\n"
                f"{sep}\n"
                f"El subject_hint extraido del DOM de Outlook: '{subject_hint}'\n\n"
                f"[LIMPIEZA - APLICA SIEMPRE]:\n"
                f"  Elimina prefijos: 'Re:', 'Fwd:', 'RV:', 'RES:', 'Enc:', 'FW:'\n"
                f"  Elimina etiquetas: [SPAM], [POSIBLE SPAM], [EXTERNO], [EXTERNAL]\n"
                f"  Normaliza espacios multiples.\n"
                f"  Ejemplo: 'Re: [EXTERNO] Informe' -> 'Informe'\n\n"
                f"[CROSS-VALIDACION]:\n"
                f"  Busca 'Asunto:', 'Subject:', 'Subj:' en el contenido.\n"
                f"  Si DIFIERE de subject_hint -> el HEADER es AUTORITATIVO.\n"
                f"  Si subject_hint >80 chars o parece body -> IGNORALO, usa headers.\n"
                f"{sep}\n"
            )

        return (
            f"{self._get_manifests()}\n"
            f"Analiza el siguiente correo y decide si es 'Noise', "
            f"'Action' o 'Executive_Decision'.\n\n"
            f"{sep}\n"
            f"  GUARDRAILS OBLIGATORIOS - REMITENTE (sender)\n"
            f"{sep}\n"
            f"El campo 'sender' debe contener UNICAMENTE la PERSONA O EMPRESA QUE ENVIO.\n\n"
            f"[REQUISITO ABSOLUTO] El campo 'sender' NUNCA debe estar vacio ''.\n"
            f"  Si no encuentras 'De:' explicito, USA EL sender_hint.\n"
            f"  El sender_hint ({sender_hint}) es mas confiable que el body.\n\n"
            f"[PASO 1] Busca SOLO encabezados al INICIO: 'De:', 'From:', 'Remitente:', 'De parte de:'\n\n"
            f"[PASO 2] IGNORA (son DESTINATARIOS): 'Para:', 'To:', 'CC:', 'Bcc:', 'CCO:'\n\n"
            f"[PASO 3] SI encuentras 'via', 'a traves de', 'en nombre de', 'on behalf of':\n"
            f"  Extrae SOLO la PERSONA REAL que envia (antes del 'via'), NO el sistema.\n"
            f"  Ejemplo: 'Juan Perez via Outlook' -> sender='Juan Perez'\n"
            f"  Ejemplo: 'Sistema de Notificaciones en nombre de Maria Lopez' -> sender='Maria Lopez'\n\n"
            f"[PASO 4] FORMATO DEL SENDER:\n"
            f"  Debe ser el NOMBRE de la persona/empresa, NO la direccion de email.\n"
            f"  Si SOLO ves un email (ej: juan@empresa.com) y no hay nombre:\n"
            f"    Extrae la ORGANIZACION o el NOMBRE LOCAL\n"
            f"  LIMPIA el nombre: quita comillas, corchetes, etiquetas HTML.\n"
            f"  Si el nombre incluye cargo/area en parentesis, conserva el nombre.\n"
            f"  Ejemplo: '\"Juan Perez\" <juan@empresa.com>' -> sender='Juan Perez'\n\n"
            f"[PASO 5 - VALIDACION FINAL DEL SENDER]:\n"
            f"  a) El sender es exactamente igual al subject_hint? RECHAZADO (es el asunto, no el remitente)\n"
            f"  b) El sender es exactamente igual a una linea 'Para:'/'To:'? RECHAZADO (es destinatario)\n"
            f"  c) El sender APARECE textual en el body del mensaje y NO esta en linea 'De:'? RECHAZADO\n"
            f"  d) El sender es 'No Reply', 'notificaciones@...' o similar? USA el emisor humano si existe\n\n"
            f"[PROHIBICIONES ABSOLUTAS]:\n"
            f"  NUNCA uses texto del CUERPO del mensaje como remitente.\n"
            f"  NUNCA uses el SUBJECT como sender.\n"
            f"  NUNCA copies la respuesta de un correo anterior como sender.\n"
            f"  NUNCA uses el 'to_recipients' como sender.\n\n"
            f"{subject_header}"
            f"  GUARDRAILS OBLIGATORIOS - CLASIFICACION (Contexto)\n"
            f"{sep}\n"
            f"Antes de clasificar, responde estas 5 preguntas en tu razonamiento:\n\n"
            f"[PREGUNTA 1 - VENTANA TEMPORAL]:\n"
            f"  El correo menciona fechas, plazos o vencimientos?\n"
            f"  Requiere accion HOY, esta SEMANA, o no hay urgencia temporal?\n"
            f"  Si hay fecha explicita y es cercana -> sesga hacia Action/Executive_Decision\n"
            f"  Si no hay fechas -> probablemente NOISE (a menos que haya accion explicita)\n\n"
            f"[PREGUNTA 2 - ACCION REQUERIDA]:\n"
            f"  El remitente SOLICITA explicitamente algo? Busca verbos:\n"
            f"    'adjunto', 'revisar', 'aprobar', 'autorizar', 'confirmar', 'enviar',\n"
            f"    'responder', 'validar', 'completar', 'subir', 'cargar', 'pagar'\n"
            f"  Hay un 'POR FAVOR' o 'FAVOR DE'? -> indica accion solicitada\n"
            f"  Si hay accion explicita -> Action o Executive_Decision (depende de jerarquia)\n"
            f"  Si hay una pregunta directa que requiere respuesta -> Action\n\n"
            f"[PREGUNTA 3 - JERARQUIA Y ALCANCE]:\n"
            f"  El correo es DIRECTAMENTE al destinatario (Para:/To:) o esta en CC?\n"
            f"  Si esta en CC -> menor prioridad, probablemente informativo (Noise)\n"
            f"  Si esta en Para:/To: y pide accion -> Action\n"
            f"  El remitente es un superior, un par, o un subordinado?\n"
            f"  Afecta decisiones estrategicas o financieras? -> Executive_Decision\n\n"
            f"[PREGUNTA 4 - HILO / CONTEXTO PREVIO]:\n"
            f"  El correo incluye historico de mensajes anteriores (citas, forwarded)?\n"
            f"  Parece ser una RESPUESTA a un correo anterior?\n"
            f"  Si hay contexto previo NO INCLUIDO, mencionarlo en razonamiento.\n"
            f"  Si es parte de una cadena larga -> probablemente NOISE si la ultima respuesta\n"
            f"    no requiere accion.\n\n"
            f"[PREGUNTA 5 - FALSA URGENCIA - DETECCION CRITICA]:\n"
            f"  ATENCION: Muchos correos AUTOMATICOS usan palabras de urgencia\n"
            f"    ('Notificacion importante', 'Alerta', 'Vencimiento pronto') pero\n"
            f"    son informativos, NO requieren accion.\n"
            f"  DISTINGUE entre:\n"
            f"    a) Urgencia REAL: persona pidiendo accion -> Action/Executive\n"
            f"    b) Urgencia de SISTEMA: alerta automatica -> evaluar si requiere accion humana\n"
            f"    c) Urgencia FALSA: newsletter, promocion, recordatorio generico -> Noise\n"
            f"  Si el correo es un newsletter, boletin, comunicado masivo -> NOISE\n\n"
            f"[GUIA DE CLASIFICACION]:\n"
            f"  NOISE: informativo, CC, newsletter, automatico sin accion requerida,\n"
            f"    notificacion de sistema, respuesta automatica, fuera de horas habiles\n"
            f"  ACTION: requiere tarea concreta, respuesta, adjunto, aprobacion simple,\n"
            f"    seguimiento, recordatorio de pago, solicitud de informacion\n"
            f"  EXECUTIVE_DECISION: impacto financiero, decision estrategica,\n"
            f"    aprobacion de alto nivel, riesgo legal, oportunidad de negocio,\n"
            f"    comunicacion con cliente importante, propuesta, cotizacion,\n"
            f"    requiere evaluacion de alternativas\n\n"
            f"[RAZONAMIENTO - FORMATO OBLIGATORIO]:\n"
            f"Escribe tu razonamiento en este formato:\n"
            f"  Sender: [nombre extraido] Validacion: [paso/fallo check XYZ]\n"
            f"  Asunto: [asunto limpio] Urgencia: [si/no - indicador encontrado]\n"
            f"  Temporal: [fechas/plazos detectados]\n"
            f"  Accion: [verbos de accion encontrados]\n"
            f"  Jerarquia: [Para/CC - nivel aproximado]\n"
            f"  Hilo: [correo nuevo / respuesta / cadena]\n"
            f"  Decision: [Noise / Action / Executive_Decision]\n"
            f"  Confianza: [Alta / Media / Baja]\n"
            f"  Explicacion: [OBLIGATORIO - ESCRIBE 1-2 FRASES EN LENGUAJE NATURAL\n"
            f"    explicando POR QUE tomaste esa decision. SIN ESTO EL SISTEMA FALLA.]\n"
            f"\n"
            f"═══ INSTRUCCION CRITICA ═══\n"
            f"Despues de completar los campos estructurados (Sender, Asunto, Temporal, Accion,\n"
            f"Jerarquia, Hilo, Decision, Confianza, Explicacion), DEBES escribir la\n"
            f"explicacion en lenguaje natural. Sin la explicacion, el sistema no puede\n"
            f"registrar el motivo de la decision y el contexto aparece vacio en los\n"
            f"paneles de monitoreo y en las notificaciones a Telegram.\n"
            f"\n"
            f"Ejemplos de explicacion correcta:\n"
            f"  - 'El correo requiere coordinar la configuracion del rotador con ACH Colombia.'\n"
            f"  - 'El usuario informa que la alerta por baja de enlace fue resuelta, solo notifica.'\n"
            f"  - 'Notificacion automatica de Grafana sobre alerta resuelta, no requiere accion.'\n"
            f"  - 'Solicitud de aprobacion de factura por servicios de conectividad del mes.'\n"
            f"\n"
            f"--- INICIO DEL CORREO ---\n"
            # Truncado a 25000 chars para caber en contextos de 32K tokens.
            f"{content[:25000]}\n"
            f"--- FIN DEL CORREO ---"
        )

    # ──────────────────────────────────────────────
    #  Utilidades
    # ──────────────────────────────────────────────

    def _extract_json(self, text: Optional[str]) -> str:
        """Extrae un objeto JSON de un texto (seguro con None/vacío)."""
        if not text:
            return "{}"
        match = re.search(r'\{.*\}', text, re.DOTALL)
        return match.group(0) if match else text

    def _trace(self, result, model, start, is_ui, p_tokens, c_tokens, api_key_id):
        latency = (time.time() - start) * 1000
        total_tokens = p_tokens + c_tokens
        if is_ui:
            AgentTracer.trace_thought(
                result.get('action', 'click'),
                result.get('reasoning', ''),
                model, total_tokens, latency
            )
        else:
            AgentTracer.log_telemetry(model, p_tokens, c_tokens, latency, "SUCCESS", api_key_id, self._engine)

    @staticmethod
    def _safe_extract_content(resp) -> Optional[str]:
        """Extrae el contenido de una respuesta del OpenAI SDK de forma segura.

        Previene el error 'NoneType' object is not subscriptable cuando
        resp.choices está vacío o resp.choices[0].message es None.
        """
        try:
            if not resp or not hasattr(resp, 'choices'):
                return None
            if not resp.choices or len(resp.choices) == 0:
                return None
            choice = resp.choices[0]
            if not choice or not hasattr(choice, 'message'):
                return None
            if not choice.message or not hasattr(choice.message, 'content'):
                return None
            return choice.message.content
        except (IndexError, AttributeError, TypeError):
            return None

    def _fallback_regex(self, text, start, mod):
        clean_mod = mod.replace("-fail", "")
        AgentTracer.log_infrastructure_error(clean_mod, text)
        return {"error": "infrastructure_failure", "engine": clean_mod, "details": text}
