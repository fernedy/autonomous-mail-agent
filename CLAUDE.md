# AGENTS.md — JarvisMail

## Package Manager
No package manager. Python 3.12+ with system deps.

## Build & Run
| Task | Command |
|------|---------|
| Build all | `docker compose build --no-cache` |
| Start all | `docker compose up -d` |
| Stop all | `docker compose down` |
| Logs (brain) | `docker logs jarvis_brain --tail 50` |
| Logs (vision) | `docker logs jarvis_vision --tail 50` |
| Logs (notifier) | `docker logs jarvis_notifier --tail 50` |
| Syntax check | `python3 -c "import ast; ast.parse(open('src/brain/brain_worker.py').read()); print('OK')"` |
| CLI config | `docker exec -it jarvis_brain python -m src.brain.cli` |

## Project Structure
- `src/brain/` — LLM Router Gateway (detección de intención + selección de modelo) + guardrails
- `src/graph/` — Microsoft Graph API: OAuth2 Client Credentials (`graph_auth.py`) + cliente HTTP (`graph_client.py`)
- `src/vision/` — Triage de correos vía Graph API (único método de conexión)
- `src/notifier/` — Telegram HITL interface
- `src/shared/` — Redis client + utils (extract_clean_reason)
- `src/observability/` — Loki tracing + metrics
- `config/SKILL.md` — LLM classification instructions (read before modifying)
- `config/LEARNED_PROFILE.md` — Auto-learned user writing patterns

## Key Conventions
- **Idioma**: Español Colombia corporativo. Nada en inglés en output.
- **Output JSON**: Siempre `{"decision", "reasoning", "sender", "to_recipients", "draft"}`. Sin markdown.
- **Guardrails**: N1-N7 post-LLM en `brain_worker.py`. No modificar sin revisar todos.
- **Sender**: Siempre nombre de persona, nunca email. `extract_clean_reason` en `shared/utils.py`.
- **Docker Secrets**: API keys en `/run/secrets/`. No hardcodear.
- **Conexión de correo**: SIEMPRE Microsoft Graph API con OAuth2 Client Credentials (`GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `graph_client_secret`, `GRAPH_MAILBOX_UPN`). Prohibido Device Code Flow o cualquier flujo interactivo.
- **Redis**: Colas `queue:raw_emails`, `queue:vision_tasks`, `queue:notifications`, `queue:user_decisions`, `queue:commands`.
- **LLM**: Siempre vía LLM Router Gateway (`llm_router_gateway.py`) con integración tipo API OpenAI (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `LLM_GATEWAY_MODEL_*`). El gateway detecta la intención y elige el modelo.
- **Provider/Model**: Override en vivo via `hive:config:provider` y `hive:config:model` (aplica a triage/uso general).
- **Docs**: Ver `CONTEXT.md` (visión general) y `ARCHITECTURE.md` (arquitectura técnica).

## Tracing & Observability
Todos los eventos de tracing van a Loki (`job="jarvis-agent"`) y se visualizan en Grafana (dashboard `agent-panel-01`). Eventos clave:
- `action=email_evaluation` — Decisión de clasificación
- `action=llm_telemetry` — Tokens, costos, latencia (por intención del gateway)
- `action=sender_guardrail_*` — Pipeline de extracción de sender
- `action=guardrail_blocked` — Regla de seguridad activada
- `hive:status:router_health` — Estado del gateway publicado por el heartbeat del brain

## Avoid
- No modificar `src/observability/tracer.py` sin verificar compatibilidad con dashboard Grafana
- No hardcodear modelos LLM — usar variables de entorno `LLM_GATEWAY_MODEL_*` y override Redis `hive:config:*`
- No eliminar `forceOverwrite: false` en provisioning — preserva cambios UI en Grafana
- No reintroducir Device Code Flow ni clientes de correo distintos de Graph API
