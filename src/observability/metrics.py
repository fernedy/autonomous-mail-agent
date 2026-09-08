import json
import time
import logging
import os

logger = logging.getLogger("metrics")

COST_PER_MODEL = {
    "gemini-2.5-flash": {"input": 0.30e-6, "output": 2.50e-6},
    "gemini-2.5-pro": {"input": 1.25e-6, "output": 10.00e-6},
    "gemini-2.0-flash": {"input": 0.10e-6, "output": 0.40e-6},
    "gemini-2.0-flash-001": {"input": 0.10e-6, "output": 0.40e-6},
    "gemini-2.0-flash-lite-001": {"input": 0.075e-6, "output": 0.30e-6},
    "gemini-2.0-flash-lite": {"input": 0.075e-6, "output": 0.30e-6},
    "gemini-2.5-flash-preview-tts": {"input": 0.30e-6, "output": 2.50e-6},
    "gemini-2.5-pro-preview-tts": {"input": 1.25e-6, "output": 10.00e-6},
    "gemma-4-26b-a4b-it": {"input": 0.0, "output": 0.0},
    "gemma-4-31b-it": {"input": 0.0, "output": 0.0},
    "gemini-flash-latest": {"input": 0.075e-6, "output": 0.30e-6},
    "gemini-flash-lite-latest": {"input": 0.075e-6, "output": 0.30e-6},
    "gemini-pro-latest": {"input": 1.25e-6, "output": 10.00e-6},
    "gemini-2.5-flash-lite": {"input": 0.10e-6, "output": 0.40e-6},
    "gemini-2.5-flash-image": {"input": 0.30e-6, "output": 2.50e-6},
    "gemini-3-pro-preview": {"input": 2.00e-6, "output": 12.00e-6},
    "gemini-3-flash-preview": {"input": 0.50e-6, "output": 3.00e-6},
    "gemini-3.1-pro-preview": {"input": 2.00e-6, "output": 12.00e-6},
    "gemini-3.1-pro-preview-customtools": {"input": 2.00e-6, "output": 12.00e-6},
    "gemini-3.1-flash-lite-preview": {"input": 0.25e-6, "output": 1.50e-6},
    "gemini-3.1-flash-lite": {"input": 0.25e-6, "output": 1.50e-6},
    "gemini-3-pro-image-preview": {"input": 2.00e-6, "output": 12.00e-6},
    "nano-banana-pro-preview": {"input": 0.10e-6, "output": 0.40e-6},
    "gemini-3.1-flash-image-preview": {"input": 0.25e-6, "output": 1.50e-6},
    "gemini-3.5-flash": {"input": 0.75e-6, "output": 4.50e-6},
    "lyria-3-clip-preview": {"input": 0.0, "output": 0.0},
    "lyria-3-pro-preview": {"input": 0.0, "output": 0.0},
    "gemini-3.1-flash-tts-preview": {"input": 0.25e-6, "output": 1.50e-6},
    "gemini-robotics-er-1.5-preview": {"input": 1.25e-6, "output": 10.00e-6},
    "gemini-robotics-er-1.6-preview": {"input": 2.00e-6, "output": 12.00e-6},
    "gemini-2.5-computer-use-preview-10-2025": {"input": 0.30e-6, "output": 2.50e-6},
    "antigravity-preview-05-2026": {"input": 0.75e-6, "output": 4.50e-6},
    "deep-research-max-preview-04-2026": {"input": 4.00e-6, "output": 24.00e-6},
    "deep-research-preview-04-2026": {"input": 1.50e-6, "output": 9.00e-6},
    "deep-research-pro-preview-12-2025": {"input": 2.00e-6, "output": 12.00e-6},
    "gemini-2.0-flash-exp": {"input": 0.10e-6, "output": 0.40e-6},
    "gemini-1.5-flash": {"input": 0.075e-6, "output": 0.30e-6},
    "llama-3.3-70b-versatile": {"input": 0.59e-6, "output": 0.79e-6},
    "llama3.1:8b": {"input": 0.0, "output": 0.0},
    "llama3:8b": {"input": 0.0, "output": 0.0},
    "gpt-oss:120b": {"input": 0.0, "output": 0.0},
}

DEFAULT_COST = {"input": 0.10e-6, "output": 0.40e-6}

# v5.3k: Costos por provider del LLM Router externo.
# El router maneja 8 providers free. El costo real es $0 para todos,
# pero registramos el costo evitado (ahorro) vs línea base de pago.
AUTHORITATIVE_COST = {
    "gemini": {"input": 0.10e-6, "output": 0.40e-6},
    "groq": {"input": 0.59e-6, "output": 0.79e-6},
    "ollama_cloud": {"input": 0.0, "output": 0.0},
    "ollama_local": {"input": 0.0, "output": 0.0},
    "open_router": {"input": 0.0, "output": 0.0},  # Depende del modelo
    "qwen": {"input": 0.0, "output": 0.0},
    "llm7": {"input": 0.0, "output": 0.0},
    "aihubmix": {"input": 0.0, "output": 0.0},
    "auto": {"input": 0.0, "output": 0.0},  # Router auto-selección
}

# Línea base para calcular COSTO EVITADO (ahorro) de modelos gratuitos.
# Representa lo que habría costado usar el proveedor pago más económico
# como alternativa (Gemini Flash 2.0 para modelos locales, Groq para cloud).
AVOIDED_COST_BASELINE = {
    "ollama_cloud": {"input": 0.59e-6, "output": 0.79e-6},   # Equivalente Groq
    "ollama_local": {"input": 0.075e-6, "output": 0.30e-6},  # Equivalente Gemini Flash Lite
    "gemini": {"input": 0.10e-6, "output": 0.40e-6},         # Los modelos gemma gratis se comparan vs Flash
    "open_router": {"input": 0.59e-6, "output": 0.79e-6},    # Equivalente Groq (modelos gratuitos)
}

def compute_cost(input_tokens: int, output_tokens: int, rates: dict) -> float:
    return (input_tokens * rates["input"]) + (output_tokens * rates["output"])

def compute_avoided_cost(provider: str, input_tokens: int, output_tokens: int, actual_rate: dict) -> float:
    """Calcula cuánto se habría gastado si este proveedor no fuera gratuito."""
    # Si el proveedor ya tiene costo > 0, no hay costo evitado
    if actual_rate.get("input", 0) > 0 or actual_rate.get("output", 0) > 0:
        return 0.0
    baseline = AVOIDED_COST_BASELINE.get(provider)
    if baseline is None:
        # Fallback: usar el rate más bajo disponible (Gemini Flash Lite)
        baseline = {"input": 0.075e-6, "output": 0.30e-6}
    return compute_cost(input_tokens, output_tokens, baseline)

def _strict_schema(level, component, event_type, message, data):
    return {
        "timestamp": time.time(),
        "level": level,
        "component": component,
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }

class OperationalMetrics:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.emails_processed = 0
            cls._instance._hour_slot = 0
            cls._instance.total_prompt_tokens = 0
            cls._instance.total_completion_tokens = 0
            cls._instance.estimated_cost_usd = 0.0
        return cls._instance

    def track_llm_usage(self, provider: str, input_tokens: int, output_tokens: int, model: str = None) -> tuple:
        """
        Retorna (cost_usd, cost_avoided_usd).
        cost_usd: costo real en USD.
        cost_avoided_usd: ahorro estimado si el proveedor es gratuito vs línea base.
        """
        rates = COST_PER_MODEL.get(model) if model else None
        if rates is None:
            rates = AUTHORITATIVE_COST.get(provider, DEFAULT_COST)
        cost = compute_cost(input_tokens, output_tokens, rates)
        cost_avoided = compute_avoided_cost(provider, input_tokens, output_tokens, rates)

        self.total_prompt_tokens += input_tokens
        self.total_completion_tokens += output_tokens
        self.estimated_cost_usd += cost

        record = _strict_schema(
            "INFO", "brain", "clevel",
            f"LLM usage: {provider}",
            {
                "provider": provider,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": round(cost, 8),
                "cost_avoided_usd": round(cost_avoided, 8),
                "cumulative_cost_usd": round(self.estimated_cost_usd, 6)
            }
        )
        logger.info(json.dumps(record, ensure_ascii=False))
        return cost, cost_avoided

    def track_email_processed(self):
        self.emails_processed += 1
        slot = int(time.time() / 3600)
        if slot != self._hour_slot:
            record = _strict_schema(
                "INFO", "vision", "tactical",
                "Email processed",
                {
                    "metric": "emails_processed_hour",
                    "value": self.emails_processed,
                    "hour_slot": self._hour_slot
                }
            )
            logger.info(json.dumps(record, ensure_ascii=False))
            self.emails_processed = 0
            self._hour_slot = slot

    def _emit_clevel(self, metric: str, value, data: dict = None):
        record = _strict_schema(
            "INFO", "brain", "clevel",
            f"C-Level metric: {metric}",
            {
                "metric": metric,
                "value": value,
                **(data or {})
            }
        )
        logger.info(json.dumps(record, ensure_ascii=False))

    def flush_hourly(self):
        self._emit_clevel("emails_processed_hour", self.emails_processed)
        self.emails_processed = 0
        self._hour_slot = int(time.time() / 3600)

    def flush_cost(self):
        self._emit_clevel("operational_cost_usd", round(self.estimated_cost_usd, 6), {
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_completion_tokens": self.total_completion_tokens
        })

    def profile_update_success(self, rules_added: int = 0, tone_shifts: list = None):
        self._emit_clevel("profile_update_success", 1, {
            "rules_added": rules_added,
            "tone_shifts": tone_shifts or []
        })

    def emit_raw_clevel(self, metric: str, value, data: dict = None):
        self._emit_clevel(metric, value, data)
