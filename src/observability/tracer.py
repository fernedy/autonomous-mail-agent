import json
import time
import logging
import os
import urllib.request
import urllib.error
import threading
import socket
from copy import deepcopy
from observability.metrics import OperationalMetrics

logger = logging.getLogger("tracer")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

LOKI_ENDPOINT = os.getenv("LOKI_ENDPOINT", "http://localhost:3100/loki/api/v1/push")
WORKER_ID = os.getenv("HOSTNAME", socket.gethostname())
TENANT_ID = os.getenv("TENANT_ID", "default")

# Cache de resolved LOKI endpoint — se resuelve una vez y se reusa
_LOKI_RESOLVED_CACHE = {"endpoint": None, "timestamp": 0.0}
_LOKI_RESOLVED_TTL = 300  # Re-resolver cada 5 min
_LOKI_RESOLVED_LOCK = threading.Lock()

_metrics = OperationalMetrics()

def _stdout_json(event_type, message, data=None, level="INFO", component="brain"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": component,
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }, ensure_ascii=False))

def _resolve_loki_endpoint() -> str:
    """Resuelve el endpoint de Loki con resolución robusta de host.docker.internal.

    Estrategia:
    1. Cache de la IP resuelta (5 min TTL) para evitar DNS flapping.
    2. Si host.docker.internal no resuelve via socket.gethostbyname,
       probar Docker bridge gateway (172.17.0.1).
    3. Si todo falla, devolver raw (urllib reportará error gracefulmente).

    En Docker Desktop/WSL2, host.docker.internal resuelve a 192.168.65.254
    via el DNS mágico de Docker. Ocasionalmente, cuando el contenedor
    recién arranca, este DNS no está listo (Errno -5 intermitente).
    Cacheando la IP resuelta eliminamos el problema.
    """
    global _LOKI_RESOLVED_CACHE

    # Si host.docker.internal NO está en la URL, no hay nada que resolver
    if "host.docker.internal" not in LOKI_ENDPOINT:
        return LOKI_ENDPOINT

    now = time.time()

    # Cache hit con double-check locking
    with _LOKI_RESOLVED_LOCK:
        cached = _LOKI_RESOLVED_CACHE["endpoint"]
        cache_time = _LOKI_RESOLVED_CACHE["timestamp"]
        if cached is not None and (now - cache_time) < _LOKI_RESOLVED_TTL:
            return cached

    # Fallo de cache: resolver ahora (fuera del lock para no bloquear otros hilos)
    candidates = []

    # 1. Intentar resolución vía socket.gethostbyname
    try:
        ip = socket.gethostbyname("host.docker.internal")
        candidates.append(ip)
    except socket.gaierror:
        pass

    # 2. Docker bridge gateway (Linux/WSL2 - siempre funciona)
    candidates.append("172.17.0.1")

    # 3. loopback (último recurso para casos donde Loki corre local)
    candidates.append("127.0.0.1")

    # Tomar el primer candidato (sin probar conectividad — _push_to_loki maneja errores)
    resolved_ip = candidates[0]
    resolved_endpoint = LOKI_ENDPOINT.replace("host.docker.internal", resolved_ip)

    # Almacenar en cache (doble check dentro del lock)
    with _LOKI_RESOLVED_LOCK:
        # Verificar si otro hilo ya cacheó mientras resolvíamos
        if _LOKI_RESOLVED_CACHE["endpoint"] is not None and (now - _LOKI_RESOLVED_CACHE["timestamp"]) < _LOKI_RESOLVED_TTL:
            return _LOKI_RESOLVED_CACHE["endpoint"]
        _LOKI_RESOLVED_CACHE["endpoint"] = resolved_endpoint
        _LOKI_RESOLVED_CACHE["timestamp"] = now

    _stdout_json("loki_resolved", f"Loki endpoint resolved to {resolved_ip}", {
        "resolved_ip": resolved_ip,
        "endpoint": resolved_endpoint
    })
    return resolved_endpoint


def _push_to_loki(log_data: dict):
    labels = {
        "job": "jarvis-agent",
        "worker": WORKER_ID,
        "level": log_data.get("level", "info").lower(),
        "tenant_id": TENANT_ID,
    }
    if "model" in log_data.get("data", {}):
        labels["model"] = log_data["data"]["model"]

    payload = {
        "streams": [
            {
                "stream": labels,
                "values": [
                    [str(int(log_data.get("timestamp", time.time()) * 1e9)), json.dumps(log_data, ensure_ascii=False)]
                ]
            }
        ]
    }

    # Obtener endpoint resuelto (usa cache si está fresco)
    resolved_endpoint = _resolve_loki_endpoint()

    # Reintentar una vez en caso de error transitorio
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                resolved_endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 204:
                    return  # Éxito — Loki aceptó
                return
        except urllib.error.HTTPError as e:
            # HTTP 400 con timestamp too old = Loki rechazó por data antigua
            # pero el servidor está vivo. No reintentar.
            error_body = e.read().decode('utf-8')
            if "timestamp too old" in error_body:
                return  # Datos muy viejos, no fatal
            if attempt == 0:
                # Forzar refresco del cache y reintentar
                with _LOKI_RESOLVED_LOCK:
                    _LOKI_RESOLVED_CACHE["endpoint"] = None
                    _LOKI_RESOLVED_CACHE["timestamp"] = 0.0
                resolved_endpoint = _resolve_loki_endpoint()
                _stdout_json("loki_retry", f"Retrying Loki push (attempt 2)", {}, "WARNING")
                continue
            _stdout_json("loki_push_error", f"Loki HTTP {e.code}", {
                "error_body": error_body
            }, "ERROR")
            return
        except Exception as e:
            warning_msg = str(e)[:200]
            if attempt == 0:
                # Forzar refresco del cache y reintentar
                with _LOKI_RESOLVED_LOCK:
                    _LOKI_RESOLVED_CACHE["endpoint"] = None
                    _LOKI_RESOLVED_CACHE["timestamp"] = 0.0
                resolved_endpoint = _resolve_loki_endpoint()
                _stdout_json("loki_retry", f"Retrying Loki push after: {warning_msg}", {}, "WARNING")
                continue
            _stdout_json("loki_push_error", "Network error pushing to Loki", {
                "error": warning_msg
            }, "ERROR")
            return

class AgentTracer:
    @staticmethod
    def _emit(data: dict):
        safe_data = deepcopy(data)
        level = safe_data.get("level", "INFO")
        logger.log(getattr(logging, level), json.dumps(safe_data, ensure_ascii=False))
        threading.Thread(target=_push_to_loki, args=(safe_data,), daemon=True).start()

    @staticmethod
    def trace_preflight_check(results: dict):
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "INFO",
            "component": "brain",
            "event_type": "tactical",
            "message": "Preflight check completed",
            "data": {
                "action": "preflight_check",
                "provider": results.get("provider"),
                "model": results.get("model"),
                "status": results.get("status")
            }
        })

    @staticmethod
    def trace_evaluation(results: dict):
        _metrics.track_email_processed()
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "INFO",
            "component": "brain",
            "event_type": "tactical",
            "message": "Email evaluated",
            "data": {
                "action": "email_evaluation",
                "mail_id": results.get("mail_id"),
                "decision": results.get("decision"),
                "sender": results.get("sender"),
                "subject": results.get("subject"),
                "reason": results.get("reason")
            }
        })

    @staticmethod
    def log_telemetry(model: str, prompt_tokens: int, completion_tokens: int, latency_ms: float, status: str, api_key_id: str, provider: str = None):
        resolved_provider = provider
        if resolved_provider is None:
            resolved_provider = api_key_id.split("_api_key")[0] if "_api_key" in api_key_id else model.split("_")[0] if "_" in model else model.split("-")[0] if "-" in model else model
        cost_usd, cost_avoided_usd = _metrics.track_llm_usage(resolved_provider, prompt_tokens, completion_tokens, model)
        total_tokens = prompt_tokens + completion_tokens
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "INFO",
            "component": "brain",
            "event_type": "tactical",
            "message": "LLM telemetry",
            "data": {
                "action": "llm_telemetry",
                "model": model,
                "provider": resolved_provider,
                "cost_usd": round(cost_usd, 8),
                "cost_avoided_usd": round(cost_avoided_usd, 8),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "api_key_id": api_key_id,
                "latency_ms": round(latency_ms, 2),
                "status": status
            }
        })

    @staticmethod
    def trace_thought(action: str, reasoning: str, model: str, token_count: int, latency_ms: float):
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "INFO",
            "component": "brain",
            "event_type": "tactical",
            "message": "Agent thought",
            "data": {
                "action": "agent_thought",
                "ui_action": action,
                "reasoning": reasoning,
                "model": model,
                "token_count": token_count,
                "latency_ms": round(latency_ms, 2)
            }
        })

    @staticmethod
    def log_guardrail_block(rule: str, mail_id: str, sender: str, subject: str, reason: str):
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "WARNING",
            "component": "brain",
            "event_type": "tactical",
            "message": f"Guardrail blocked: {rule}",
            "data": {
                "action": "guardrail_blocked",
                "rule": rule,
                "mail_id": mail_id,
                "sender": sender,
                "subject": subject,
                "reason": reason
            }
        })

    @staticmethod
    def trace_sender_guardrail(action: str, mail_id: str, details: dict, level: str = "INFO"):
        """Traza eventos del pipeline de extracción de sender (N1, N3, N5).
        
        Permite que los paneles de 'Sender Quality' en Grafana tengan datos
        consultables desde Loki, no solo en stdout.
        
        Args:
            action: Código del guardrail (sender_guardrail_empty, sender_guardrail_subject, etc.)
            mail_id: ID del correo
            details: Dict con detalles específicos del guardrail
            level: Nivel de log (INFO, WARNING)
        """
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": level,
            "component": "brain",
            "event_type": "tactical",
            "message": f"Guardrail: {action}",
            "data": {
                "action": action,
                "mail_id": mail_id,
                **details
            }
        })

    @staticmethod
    def trace_triage_progress(action: str, mail_id: str, details: dict):
        """Traza eventos del flujo de triage (triage_started, triage_completed)."""
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "INFO",
            "component": "brain",
            "event_type": "tactical",
            "message": f"Triage: {action}",
            "data": {
                "action": action,
                "mail_id": mail_id,
                **details
            }
        })

    @staticmethod
    def log_infrastructure_error(engine: str, message: str):
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "ERROR",
            "component": "brain",
            "event_type": "tactical",
            "message": "Infrastructure error",
            "data": {
                "action": "infrastructure_error",
                "engine": engine,
                "error": message
            }
        })

    @staticmethod
    def trace_mail_pipeline(mail_id: str, pipeline_steps: list, final_decision: dict = None):
        """Emite un registro completo de toda la cadena de razonamiento
        y acciones para un correo analizado.

        Este método consolida TODOS los pasos del pipeline en un solo
        evento de trazabilidad que incluye:
        - Cada etapa del procesamiento (guardrails, sender extraction, LLM call)
        - El razonamiento completo en cada etapa
        - Las acciones tomadas
        - El resumen final

        Esto permite:
        1. Ver en Grafana/Loki la trazabilidad completa por mail_id
        2. Enviar al notifier un resumen ejecutivo
        3. Depurar problemas de razonamiento

        Args:
            mail_id: ID único del correo.
            pipeline_steps: Lista ordenada de dicts con:
                - step: Nombre del paso (str)
                - action: Acción tomada (str)
                - reasoning: Razonamiento/justificación (str)
                - duration_ms: Duración en ms (float, opcional)
                - tokens_used: Tokens consumidos (int, opcional)
            final_decision: Dict con la decisión final del pipeline.
        """
        total_duration = sum(s.get("duration_ms", 0) for s in pipeline_steps)
        total_tokens = sum(s.get("tokens_used", 0) for s in pipeline_steps)

        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "INFO",
            "component": "brain",
            "event_type": "tactical",
            "message": f"Mail pipeline trace: {mail_id}",
            "data": {
                "action": "mail_pipeline_trace",
                "mail_id": mail_id,
                "steps": pipeline_steps,
                "total_duration_ms": round(total_duration, 2),
                "total_tokens": total_tokens,
                "final_decision": final_decision or {},
                "summary": AgentTracer._build_pipeline_summary(pipeline_steps, final_decision)
            }
        })

    @staticmethod
    def _build_pipeline_summary(pipeline_steps: list, final_decision: dict = None) -> str:
        """Construye un resumen legible de la cadena de trazabilidad."""
        lines = ["📋 TRAZABILIDAD COMPLETA DEL ANÁLISIS", "═" * 40]

        for i, step in enumerate(pipeline_steps, 1):
            name = step.get("step", f"Paso {i}")
            action = step.get("action", "")
            reasoning = step.get("reasoning", "")
            duration = step.get("duration_ms", 0)
            tokens = step.get("tokens_used", 0)

            lines.append(f"\n🔹 Paso {i}: {name}")
            if action:
                lines.append(f"   Acción: {action}")
            if reasoning:
                lines.append(f"   Razonamiento: {reasoning[:200]}")
            if duration > 0:
                lines.append(f"   Duración: {duration:.0f}ms")
            if tokens > 0:
                lines.append(f"   Tokens: {tokens}")

        if final_decision:
            lines.append("\n" + "═" * 40)
            lines.append("📊 DECISIÓN FINAL")
            lines.append(f"   Clasificación: {final_decision.get('decision', 'N/A')}")
            lines.append(f"   Remitente: {final_decision.get('sender', 'N/A')}")
            lines.append(f"   Confianza: {final_decision.get('confidence', 'N/A')}")

        return "\n".join(lines)

    @staticmethod
    def trace_event(event_type: str, details: dict):
        """Traza eventos genéricos del sistema para telemetría en Grafana.

        Args:
            event_type: Tipo de evento (behavioral_rules, config_change, etc.)
            details: Dict con detalles específicos del evento
        """
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "INFO",
            "component": "brain",
            "event_type": "tactical",
            "message": f"Event: {event_type}",
            "data": {
                "action": event_type,
                **details
            }
        })

    @staticmethod
    def trace_router_call(model: str, status: str, latency_ms: float, prompt_length: int = 0):
        """Traza llamadas al LLM Router externo.

        v5.3k: El LLM Router externo maneja providers, keys y failover.
        Este método registra métricas de la llamada al router.

        Args:
            model: Modelo solicitado (ej: auto, groq/llama-3.3-70b-versatile)
            status: Estado (success, error, timeout)
            latency_ms: Latencia en milisegundos
            prompt_length: Tamaño del prompt en caracteres
        """
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "INFO",
            "component": "brain",
            "event_type": "tactical",
            "message": f"Router call: {model} - {status}",
            "data": {
                "action": "router_call",
                "model": model,
                "status": status,
                "latency_ms": round(latency_ms, 2),
                "prompt_length": prompt_length
            }
        })

    @staticmethod
    def trace_router_providers(providers: dict):
        """Traza el estado de los providers del LLM Router.

        v5.3k: Consulta periódica al /v1/providers del router.

        Args:
            providers: Dict con estado de cada provider del router
        """
        active = sum(1 for p in providers.values() if p.get("status") == "operational")
        total = len(providers)
        AgentTracer._emit({
            "timestamp": time.time(),
            "level": "INFO",
            "component": "brain",
            "event_type": "tactical",
            "message": f"Router providers: {active}/{total} operational",
            "data": {
                "action": "router_providers_status",
                "active": active,
                "total": total,
                "providers": list(providers.keys()),
                "details": providers
            }
        })
