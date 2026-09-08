# CONTEXT — Sistema de Triage Automatizado de Correos (v6.0)

> **Última actualización:** 2026-09-07
>
> **v6.0 (actual):** Graph API Client Credentials único + LLM Router Gateway embebido
>
> ## ✅ CAMBIOS ESTRUCTURALES v6.0
>
> ### 1. Conexión 100% Microsoft Graph API (Client Credentials)
> - **Eliminado por completo el Device Code Flow** (`src/graph/msal_auth.py` fuera).
> - Nueva autenticación `GraphAPIAuth` (`src/graph/graph_auth.py`): OAuth2
>   Client Credentials vía `msal.ConfidentialClientApplication`. 100% desatendida.
> - Configuración por variables de entorno / Docker Secrets:
>   `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`
>   (o `GRAPH_CLIENT_SECRET_FILE` → secret `graph_client_secret`), `GRAPH_MAILBOX_UPN`.
> - El `GraphClient` traduce automáticamente `/me/...` → `/users/{GRAPH_MAILBOX_UPN}/...`
>   (en app-only no existe contexto de usuario).
> - Token cache en memoria con renovación automática 120s antes de expirar.
> - Eliminados: códigos por Telegram, caché `msal_token_cache.json` en disco,
>   handler `device_code_required` y handlers MFA legacy del notifier.
>
> ### 2. LLM Router Gateway (embebido) reemplaza al LLM Router externo
> - **Eliminado** el microservicio LLM Router Standalone (`:8101`) y el cliente
>   thin `src/brain/llm_router.py`.
> - Nuevo `src/brain/llm_router_gateway.py`: gateway embebido que:
>   1. **Detecta la intención** de cada solicitud (`detect_intent`): `email_triage`,
>      `ui_action`, `profile_analysis`, `general`.
>   2. **Elige el modelo** automáticamente según la intención (env `LLM_GATEWAY_MODEL_*`).
>   3. **Siempre** usa integración tipo API OpenAI: `OPENAI_API_KEY` + `OPENAI_BASE_URL`.
> - Override en caliente: Redis `hive:config:model` (aplica a triage/uso general).
> - El heartbeat del brain publica el estado del gateway en
>   `hive:status:router_health` (misma clave, compatible con Grafana).
> - `docker-compose.yml`: fuera `LLM_ROUTER_URL`; dentro `OPENAI_*`, `LLM_GATEWAY_*` y `GRAPH_*`.

---

> ## ¿Qué es este sistema?
>
> **JarvisMail** es un agente autónomo de triage de correos electrónicos diseñado para
> el **Director de Telecomunicaciones, Monitoreo y Mesa de Ayuda TI** de **Banco AV Villas
> (Colombia)**. Clasifica correos entrantes en 4 categorías y redacta borradores de respuesta
> para aprobación humana antes de enviarlos.
>
> ## Perfil del Usuario (Director de TI)
>
> - **Cargo:** Director de Telecomunicaciones, Monitoreo y Mesa de Ayuda TI
> - **Industria:** Banca (Colombia)
> - **Idioma:** Español (Colombia) — corporativo
> - **Volumen de correos:** Alto (decenas a cientos por día)
> - **Necesidad principal:** No perder tiempo en correos informativos (Noise);
>   enfocarse solo en los que requieren acción o decisión ejecutiva.
> - **Estilo de redacción:** Formal, estructura saludo-cuerpo-despedida.
> - **Zona horaria:** UTC-5 (Colombia)
>
> ## Stack Tecnológico
>
> | Componente | Tecnología |
> |-----------|-----------|
> | API de Correo | **Microsoft Graph API v1.0** (oficial, NO deprecada) — ÚNICO método de conexión |
> | Autenticación | **OAuth2 Client Credentials** (app-only) vía MSAL ConfidentialClientApplication |
> | Cliente HTTP | **httpx** (HTTP directo con Bearer token) |
> | Token Cache | En memoria, renovación automática desatendida |
> | Procesamiento | GraphClient + EmailReader |
> | Orquestación | Redis (colas + pub/sub) |
> | LLM | **LLM Router Gateway** (embebido, detección de intención + selección de modelo) |
> | Cliente LLM | `AsyncOpenAI(OPENAI_API_KEY, OPENAI_BASE_URL)` — integración tipo API OpenAI |
> | Notificaciones | Telegram Bot API |
> | Observabilidad | Loki (logs), Grafana (dashboards) — propios del agente |
> | Contenedores | Docker / Docker Compose |
> | Lenguaje | Python 3.11+ |
> | **Recursos** | **CPU 0.5, RAM 512MB** (sin Chromium, sin VNC) |
>
> ---
>
> ## Arquitectura Actual (v6.0)
>
> ```
> ┌──────────────────────────────────────────────────────────────────────┐
> │                        ARQUITECTURA v6.0                             │
> │                                                                      │
> │  Todo en un solo Docker Compose (sin microservicio LLM externo)      │
> │                                                                      │
> │  ┌────────────┐    ┌──────────────┐    ┌──────────────────┐         │
> │  │ Vision     │───>│ Redis Queues │───>│ Brain Worker     │         │
> │  │ (Graph API)│    │ (pub/sub)    │    │ (guardrails)     │         │
> │  └─────┬──────┘    └──────────────┘    └────────┬─────────┘         │
> │        │                                        │                   │
> │        │ OAuth2 Client Credentials              ▼                   │
> │        │ (MSAL, desatendido)        ┌───────────────────────┐       │
> │        ▼                            │ LLM ROUTER GATEWAY    │       │
> │  ┌───────────────────────┐          │ (embebido)            │       │
> │  │ Microsoft Graph API   │          │ • detect_intent()     │       │
> │  │ v1.0 HTTP Bearer      │          │ • modelo por intención│       │
> │  │ /users/{UPN}/...      │          │ • API OpenAI          │       │
> │  └───────────────────────┘          └──────────┬────────────┘       │
> │                                                │ HTTPS              │
> │          ┌────────────────┐                    ▼                    │
> │          │ Loki + Grafana │  ←←← eventos   OPENAI_BASE_URL           │
> │          │ (del agente)   │                                         │
> │          └────────────────┘        ┌──────────────────┐             │
> │                                    │ Notifier (HITL)  │───> Telegram│
> │                                    │ (Telegram Bot)   │             │
> │                                    └──────────────────┘             │
> └──────────────────────────────────────────────────────────────────────┘
> ```
>
> ### Flujo de Llamada LLM
>
> ```
> Brain Worker / Vision Worker
>   │
>   ├── gateway.detect_intent(data)  →  "email_triage" | "ui_action"
>   │
>   ├── gateway._select_model(intent)  →  LLM_GATEWAY_MODEL_TRIAGE (o override Redis)
>   │
>   ├── resp = await client.chat.completions.create(model=..., response_format=json_object)
>   │   └── AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
>   │
>   └── Brain procesa respuesta JSON (guardrails N1-N9)
> ```
>
> ### Autenticación Graph API (únicos pasos manuales, en Azure Portal)
>
> 1. App registration en Azure AD/Entra ID
> 2. Permisos de **aplicación**: `Mail.ReadWrite`, `Mail.Send`, `User.Read.All` + admin consent
> 3. Client secret → `secrets/graph_client_secret.txt`
> 4. `.env`: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_MAILBOX_UPN`
>
> Después de eso, la autenticación es 100% desatendida (sin device code, sin MFA).
>
> ---
>
> ## Estado del Sistema (Sep 2026)
>
> ### LLM Router Gateway — Intenciones y Modelos
>
> | Intención | Cuándo | Variable de entorno (modelo) |
> |-----------|--------|------------------------------|
> | `email_triage` | Clasificación de correos (Noise/Action/Executive) | `LLM_GATEWAY_MODEL_TRIAGE` |
> | `ui_action` | Acciones sobre DOM (legacy Playwright) | `LLM_GATEWAY_MODEL_UI` |
> | `profile_analysis` | Auto-aprendizaje / perfil de estilo | `LLM_GATEWAY_MODEL_PROFILE` |
> | `general` | Cualquier otra solicitud | `LLM_GATEWAY_MODEL` |
>
> - Override en caliente: Redis `hive:config:model` (solo triage/general).
> - Telegram `/setprovider` → modo Auto del gateway (borra el override).
>
> ### AutoLearner v5.2 — Behavioral Rules ✅
>
> El AutoLearner extrae **dos tipos de patrones**:
>
> | Tipo | Qué aprende | Dónde se guarda |
> |------|------------|----------------|
> | **StylePatterns** | Saludos, despedidas, frases comunes, tono, longitud | `# AUTO LEARNING` (acumulativo) |
> | **BehavioralPatterns** | Decisiones, aprobaciones, delegaciones, escalamientos | `# BEHAVIORAL RULES` (reemplazo) |
>
> **Behavioral Rules**:
> - `decision_patterns` — Cómo toma decisiones
> - `approval_keywords` — Palabras clave de aprobación
> - `delegation_patterns` — Cómo asigna tareas
> - `response_speed` — Velocidad estimada de respuesta
> - `escalation_triggers` — Situaciones que disparan urgencia
>
> ---
>
> ## Observabilidad
>
> | Componente | Propósito |
> |-----------|-----------|
> | `tracer.py` | AgentTracer → Loki + stdout (telemetría por intención del gateway) |
> | `metrics.py` | OperationalMetrics → cost tracking |
> | `telegram_bot.py` | Alertas vía Telegram |
> | `hive:status:router_health` | Estado del gateway publicado por el heartbeat del brain (Grafana) |
> | **Loki** (contenedor) | Almacenamiento de logs |
> | **Grafana** (contenedor) | Dashboards tácticos + C-Level |
>
> ---
>
> ## Cómo Reanudar (para el próximo agente/sesión)
>
> ### Si el sistema ya está corriendo:
>
> ```bash
> # Ver estado de contenedores del agente
> docker ps --format 'table {{.Names}}\t{{.Status}}'
>
> # Verificar que el gateway esté configurado (publicado por el heartbeat del brain)
> docker exec jarvis_broker redis-cli -a "$(cat secrets/redis_pass.txt)" \
>   GET hive:status:router_health
>
> # Ver logs del brain
> docker logs jarvis_brain --tail 50 -f
> ```
>
> ### Si necesitas reiniciar:
>
> ```bash
> # Parar todo
> docker compose down
>
> # Forzar rebuild sin caché
> docker compose build --no-cache brain
>
> # Iniciar
> docker compose up -d
> ```
>
> ### Para cambiar modelo LLM
>
> ```bash
> # Opción 1 (permanente): editar LLM_GATEWAY_MODEL* en .env y reiniciar
> # Opción 2 (en caliente): override en Redis
> docker exec jarvis_brain python3 -c "
> import asyncio
> from shared.redis_client import RedisTaskBroker
>
> async def fix():
>     broker = RedisTaskBroker()
>     await broker.connect()
>     await broker.client.set('hive:config:provider', 'gateway')
>     await broker.client.set('hive:config:model', 'gpt-4o-mini')
>     print('Override: provider=gateway, model=gpt-4o-mini')
>     await broker.client.aclose()
>
> asyncio.run(fix())
> "
> # Opción 3: Telegram /setprovider → 'Auto (Gateway por intención)' borra el override
> ```
>
> ---
>
> ## Archivos Clave
>
> | Archivo | Propósito |
> |---------|-----------|
> | `src/graph/graph_auth.py` | OAuth2 Client Credentials (MSAL, app-only, desatendido) |
> | `src/graph/graph_client.py` | Cliente HTTP Graph API (traduce /me → /users/{UPN}) |
> | `src/brain/llm_router_gateway.py` | Gateway LLM: intención + selección de modelo (API OpenAI) |
> | `src/vision/vision_worker.py` | Orquestador principal |
> | `src/auto_learning.py` | Auto-aprendizaje de patrones |
> | `src/brain/brain_worker.py` | Triage + guardrails |
> | `src/notifier/notifier_worker.py` | Telegram HITL + comandos |
> | `src/observability/tracer.py` | Trazabilidad a Loki |
> | `src/observability/metrics.py` | Cost tracking |
> | `src/shared/redis_client.py` | Queue push/pop con trazabilidad Loki |
>
> ---
>
> ## Reglas de Oro
>
> 1. **NO hay aprobaciones autónomas** — Siempre preguntar al humano
> 2. **Sin firma en el borrador** — La firma se inyecta automáticamente
> 3. **No inventar información** — Si no hay contexto, clasificar como Noise
> 4. **Sender es NOMBRE, no email** — Extraer persona/empresa
> 5. **Idioma: Español Colombia** — Corporativo, formal
> 6. **Output siempre JSON válido** — Sin markdown, sin viñetas fuera del JSON
> 7. **SIEMPRE actualizar CONTEXT.md y ARCHITECTURE.md**
> 8. **Conexión de correo: SIEMPRE Graph API Client Credentials** — Sin device code ni flujos interactivos
> 9. **LLM: siempre vía LLM Router Gateway** — Integración tipo API OpenAI por variables de entorno (`OPENAI_*`, `LLM_GATEWAY_MODEL_*`)
> 10. **No hardcodear secretos** — Docker Secrets (`/run/secrets/`) y `.env` (no versionado)
