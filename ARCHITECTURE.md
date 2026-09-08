# ARCHITECTURE — JarvisMail System Design (v6.0)

> **Última actualización:** 2026-09-07
>
> **v6.0 (actual):** Graph API Client Credentials único + LLM Router Gateway embebido
>
> ## ✅ CAMBIOS v6.0
>
> ### 1. Conexión 100% Graph API (Client Credentials)
> - **Eliminado el Device Code Flow** por completo (`msal_auth.py` fuera)
> - Nueva autenticación `GraphAPIAuth` (`src/graph/graph_auth.py`): OAuth2
>   Client Credentials con `ConfidentialClientApplication`, 100% desatendida
> - Configuración: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`
>   (o Docker Secret `graph_client_secret`), `GRAPH_MAILBOX_UPN`
> - Las rutas `/me` se traducen automáticamente a `/users/{GRAPH_MAILBOX_UPN}`
> - Sin MFA interactivo, sin códigos por Telegram, sin caché de tokens en disco
>
> ### 2. LLM Router Gateway (embebido)
> - **Eliminado el LLM Router externo** (`:8101`) y el cliente thin `llm_router.py`
> - Nuevo `src/brain/llm_router_gateway.py`: gateway embebido que **detecta la
>   intención** (email_triage, ui_action, profile_analysis, general) y **elige
>   el modelo** automáticamente según variables de entorno `LLM_GATEWAY_MODEL_*`
> - Integración **siempre tipo API OpenAI**: `OPENAI_API_KEY` + `OPENAI_BASE_URL`
> - Override en caliente: clave Redis `hive:config:model` (triage/general)
> - Health publicado por el heartbeat del brain en `hive:status:router_health`

---

## 📐 Arquitectura General

```
┌──────────────────────────────────────────────────────────────────────┐
│                     ECOSISTEMA COMPLETO v6.0                         │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │  AGENTE JARVISMAIL (este proyecto, un solo Docker Compose)    │   │
│  │                                                               │   │
│  │  ┌──────────────┐    ┌────────────────────────────────────┐   │   │
│  │  │ Brain Worker │───▶│ LLM ROUTER GATEWAY (embebido)      │   │   │
│  │  │ (guardrails) │    │ • Detección de intención           │   │   │
│  │  └──────────────┘    │ • Selección de modelo por intent   │   │   │
│  │                       │ • API OpenAI (OPENAI_BASE_URL)     │   │   │
│  │  ┌──────────────┐    └────────────────┬───────────────────┘   │   │
│  │  │ Vision Worker│                     │ HTTPS                 │   │
│  │  │ (Graph API)  │◀──── Microsoft Graph API v1.0 ──────────┐   │   │
│  │  └──────────────┘    OAuth2 Client Credentials (MSAL)     │   │
│  │  ┌──────────────┐                                         │   │
│  │  │ Notifier     │    ┌────────────────────────────────┐   │   │
│  │  │ (Telegram)   │    │ Redis (colas) + Loki + Grafana │   │   │
│  │  └──────────────┘    └────────────────────────────────┘   │   │
│  └───────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 1. Principios Arquitectónicos

### Separación de Responsabilidades

| Componente | Responsabilidad |
|-----------|----------------|
| **LLM Router Gateway** (embebido) | Detección de intención, selección de modelo, llamada LLM vía API OpenAI |
| **Brain Worker** | Triage, guardrails, pipeline de decisión |
| **Vision Worker** | Autenticación MSAL (Client Credentials) + Graph API, lectura de correos, acciones |
| **Notifier** | Interfaz humano (Telegram), HITL, comandos |
| **Loki/Grafana** | Observabilidad del agente (métricas de triage, costos evitados) |

### Comunicación

- **Gateway → LLM:** HTTPS (OpenAI SDK) via `OPENAI_BASE_URL` (default `https://api.openai.com/v1`)
- **Workers → Graph API:** HTTPS con Bearer token (OAuth2 Client Credentials)
- **Workers → Redis:** Colas internas (pub/sub)
- **Workers → Loki:** HTTP directo (`urllib`)

---

## 2. Brain Worker (`src/brain/`)

### LLM Router Gateway (embebido)

El gateway reemplaza al router externo. Ya no existe microservicio de LLM:
- ❌ KeyManager multi-provider (8 providers free)
- ❌ Circuit breaker / failover / RPM-RPD tracking
- ❌ `LLM_ROUTER_URL` / admin panel en `:8101`

En su lugar (`llm_router_gateway.py`):
- ✅ Detección de intención determinista (`detect_intent`)
- ✅ Selección de modelo por intención (env `LLM_GATEWAY_MODEL_*`)
- ✅ Única integración tipo API OpenAI: `AsyncOpenAI(OPENAI_API_KEY, OPENAI_BASE_URL)`
- ✅ Override en caliente desde Redis (`hive:config:model`) para triage/general
- ✅ Truncación inteligente de prompts (25K chars email, 8K SKILL.md, 6K profile)

### Pipeline de Triage

```
Email entrante (JSON estructurado desde Graph API)
    │
    ├── 1. _get_manifests()
    │   ├── Lee SKILL.md (truncado a 8K chars)
    │   ├── Lee LEARNED_PROFILE.md (truncado a 6K chars)
    │   ├── Extrae behavioral_rules
    │   └── Retorna XML contextual
    │
    ├── 2. _build_prompt(mail_data)
    │   ├── Manifests (contexto)
    │   ├── Guardrails (instrucciones)
    │   ├── sender_hint + subject_hint
    │   └── Email content (truncado a 25K chars)
    │
    ├── 3. LLM Call (vía Router externo)
    │   ├── client = AsyncOpenAI(base_url=LLM_ROUTER_URL)
    │   ├── model = "auto" (router selecciona provider)
    │   ├── response_format = {"type": "json_object"}
    │   └── Timeout: 120s
    │
    ├── 4. _safe_extract_content(resp)
    │   └── Maneja NoneType, errores de parseo
    │
    └── 5. Guardrails Post-LLM (N1-N9)
        ├── N1: Empty sender → sender_hint
        ├── N2: Anti-hallucination
        ├── N3: Sender vacío → subject
        ├── N4: Subject body vs header
        ├── N5: Disclaimer/signature
        ├── N6: Saludo en sender
        ├── N7: Email address en sender
        ├── N8: System label → humano
        └── N9: Subject hallucination → original Graph API subject
```

### Configuración vía Redis

```python
# Config actual (óptima)
hive:config:provider = "auto"
hive:config:model = "auto"
```

Cuando el brain detecta cambio en Redis (pub/sub), sale automáticamente de HITL.

### Truncación de Prompts (v5.3k)

Para respetar límites de contexto de providers free:

| Componente | Límite | Razón |
|-----------|--------|-------|
| Email content | 25,000 chars | Antes: 50,000. Groq: 32K tokens |
| SKILL.md | 8,000 chars | El archivo real es ~6.2K |
| LEARNED_PROFILE.md | 6,000 chars | El archivo real es ~45.6K |
| **Total prompt** | **~40K chars ≈ 13K tokens** | Cabe en Groq (32K tokens) |

---

## 3. Vision Worker (`src/vision/`)

### Autenticación — Graph API Client Credentials (único método)

**Sin device code, sin MFA interactivo, sin navegador.** La autenticación es
OAuth2 Client Credentials (app-only) contra Azure AD/Entra ID:

**Requisitos previos (una sola vez, en Azure Portal):**
1. Registrar una aplicación en Azure AD
2. Conceder permisos de **aplicación**: `Mail.ReadWrite`, `Mail.Send`, `User.Read.All`
3. Aprobar el admin consent
4. Crear un client secret

**Flujo de autenticación (100% desatendido):**

```
vision-worker arranca
    ├── GraphAPIAuth.get_token()
    │   ├── MSAL ConfidentialClientApplication
    │   │   └── acquire_token_for_client(scope=.default) → token
    │   └── Token cache en memoria (renueva 120s antes de expirar)
    └── Triage loop
        └── Si el token expira → refresh_token() desatendido
```

Configuración: `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`
(o Docker Secret `graph_client_secret`), `GRAPH_MAILBOX_UPN`.

### GraphClient — Microsoft Graph API v1.0

En modo app-only no existe `/me`: el client traduce automáticamente
`/me/...` → `/users/{GRAPH_MAILBOX_UPN}/...`.

| Operación | Endpoint |
|-----------|----------|
| Listar no leídos | `GET /users/{upn}/mailFolders('Inbox')/messages?$filter=isRead eq false` |
| Contenido completo | `GET /users/{upn}/messages/{id}?$expand=attachments` |
| Crear borrador | `POST /users/{upn}/messages/{id}/createReply` (sin `comment`) |
| Enviar | `POST /users/{upn}/messages/{draftId}/send` |
| Archivar | `POST /users/{upn}/messages/{id}/move` |
| Marcar leído | `PATCH /users/{upn}/messages/{id}` |
| Eliminar | `DELETE /users/{upn}/messages/{id}` |

---

## 4. Notifier Worker (`src/notifier/`)

### Comandos Telegram

| Comando | Acción |
|---------|--------|
| `/status` | Estado del sistema + health del router |
| `/setprovider` | Cambiar modelo LLM (teclado dinámico del router) |
| `/learn` | Forzar auto-aprendizaje |
| `/help` | Ayuda |

### `/setprovider` — Flujo actual (v5.3k)

```
Usuario: /setprovider
    ├── Notifier consulta /v1/models/free del router
    │   └── Cache 60s (evita rate limiting del router)
    ├── Construye teclado inline con modelos disponibles
    ├── Usuario selecciona modelo
    ├── Notifier guarda en Redis: hive:config:model = "groq/llama-3.3-70b-versatile"
    ├── Notifier guarda en Redis: hive:config:provider = "groq" (o "auto")
    └── Brain detecta cambio via pub/sub y aplica
```

**Ya no existen:** `_get_api_keys()`, `fetch_provider_models()`, `PROVIDER_KEYBOARD`.

---

## 5. AutoLearner (`src/auto_learning.py`)

### Flujo

1. Cada 60 min (o por comando `/learn`)
2. `GraphClient.get_sent_messages(limit=5)` — últimos correos enviados
3. Análisis de estilo:
   - Si router disponible: análisis LLM con prompt estructurado
   - Si router caído: heurísticas regex (extracción de saludos, despedidas, frases)
4. Extrae: saludos, despedidas, frases comunes, tono dominante
5. Persiste en `LEARNED_PROFILE.md` bajo `# AUTO LEARNING`
6. El `llm_router.py` lee este perfil en cada triage

### Behavioral Rules (v5.2)

Además de patrones de estilo, extrae patrones de comportamiento:
- `decision_patterns` — Cómo toma decisiones
- `approval_keywords` — Palabras clave de aprobación
- `delegation_patterns` — Cómo asigna tareas a otros
- `response_speed` — Velocidad estimada de respuesta
- `escalation_triggers` — Situaciones que disparan urgencia

---

## 6. Observabilidad (`src/observability/`)

### Propia del Agente (independiente del router)

| Archivo | Propósito |
|---------|-----------|
| `tracer.py` | AgentTracer → Loki + stdout |
| `metrics.py` | OperationalMetrics |
| `telegram_bot.py` | Alertas vía Telegram |

### Eventos a Loki

| Evento | Método | Propósito |
|--------|--------|-----------|
| `router_call` | `trace_router_call()` | Llamada al router: modelo, status, latency, prompt_length |
| `router_providers_status` | `trace_router_providers()` | Estado de providers del router |
| `email_evaluation` | `trace_evaluation()` | Decisión de triage |
| `llm_telemetry` | `log_telemetry()` | Tokens, costo, modelo (router-proxied) |
| `guardrail_blocked` | `log_guardrail_block()` | Correos bloqueados |
| `infrastructure_error` | `log_infrastructure_error()` | Errores de infra |
| `preflight_check` | `trace_preflight_check()` | Health check |
| `agent_thought` | `trace_thought()` | Razonamiento del agente |
| `mail_pipeline_trace` | `trace_mail_pipeline()` | Trazabilidad completa |
| `sender_guardrail_*` | `trace_sender_guardrail()` | Calidad de sender |

### Labels Loki

```python
labels = {
    "job": "jarvis-agent",       # Filtro principal de Grafana
    "worker": WORKER_ID,          # Hostname del contenedor
    "level": log_level,           # info/warning/error
    "tenant_id": TENANT_ID,       # Multi-tenant support
}
```

### Resolución de host.docker.internal en Loki

```python
def _resolve_loki_endpoint() -> str:
    """Python's urllib a veces no resuelve host.docker.internal.
    Fallback: socket.gethostbyname() manual."""
    if "host.docker.internal" in endpoint:
        try:
            ip = socket.gethostbyname("host.docker.internal")
            endpoint = endpoint.replace("host.docker.internal", ip)
        except socket.gaierror:
            pass  # Dejar que urllib intente
```

---

## 7. Colas Redis

| Cola | Producer | Consumer | Propósito |
|------|----------|----------|-----------|
| `queue:default:raw_emails` | VisionWorker | BrainWorker | Triage input |
| `queue:default:vision_tasks` | BrainWorker | VisionWorker | Decisión + borrador |
| `queue:notifications` | Ambos | NotifierWorker | Telegram msgs |
| `queue:user_decisions` | NotifierWorker | VisionWorker | HITL respuesta |
| `queue:commands` | NotifierWorker | VisionWorker | Comandos manuales |

Todas las colas emiten eventos `queue_push`/`queue_pop` a Loki.

---

## 8. Infraestructura Docker

| Servicio | Imagen | Puerto | Recursos | Dependencias |
|----------|--------|--------|----------|-------------|
| `jarvis_brain` | `jarvis-brain:latest` | - | CPU 0.5, RAM 256M | redis, loki |
| `jarvis_vision` | `jarvis-vision:latest` | - | CPU 0.5, RAM 512M | redis, loki, notifier |
| `jarvis_notifier` | `mail-notifier:latest` | - | CPU 0.25, RAM 256M | redis |
| `jarvis_broker` | `redis:7-alpine` | 6379 | CPU 0.5, RAM 256M | - |
| `jarvis_loki` | `grafana/loki:latest` | 3100 | CPU 1.0, RAM 1G | - |
| `jarvis_grafana` | `grafana/grafana-enterprise` | 3000 | CPU 0.25, RAM 256M | loki |
| `jarvis_postgres` | `postgres:16-alpine` | 5432 | CPU 0.5, RAM 512M | - |
| `jarvis_web` | `jarvis-web:latest` | 8080 | CPU 0.5, RAM 256M | postgres, redis |

### Todo corre en ESTE Docker Compose (v6.0)

El LLM Router Gateway es embebido (dentro de brain y vision workers). No hay
microservicio externo de LLM ni admin panel en `:8101`.

### Variables de Entorno Clave

| Variable | Valor | Propósito |
|----------|-------|-----------|
| `GRAPH_TENANT_ID` | — | Tenant de Azure AD (Client Credentials) |
| `GRAPH_CLIENT_ID` | — | Client ID del app registration |
| `GRAPH_CLIENT_SECRET` / `GRAPH_CLIENT_SECRET_FILE` | — | Secret del app registration (Docker Secret recomendado) |
| `GRAPH_MAILBOX_UPN` | — | Buzón a operar en modo app-only |
| `OPENAI_API_KEY` | — | API key del endpoint OpenAI-compatible |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL tipo API OpenAI del gateway |
| `LLM_GATEWAY_MODEL` | `gpt-4o-mini` | Modelo por defecto del gateway |
| `LLM_GATEWAY_MODEL_TRIAGE` / `_UI` / `_PROFILE` | — | Modelos dedicados por intención |
| `LOKI_ENDPOINT` | `http://host.docker.internal:3100/loki/api/v1/push` | Loki del agente |
| `TENANT_ID` | `default` | Multi-tenant (colas Redis) |
| `DFGA_SIGNATURE_HTML` | HTML | Firma interactiva (fallback a SKILL.md) |

---

## 9. Decisiones de Diseño

### 1. LLM Router Gateway embebido vs Router externo (v6.0)

| Aspecto | Antes (router externo :8101) | Ahora (gateway embebido) |
|---------|------------------------------|--------------------------|
| Componentes | Agente + microservicio separado | Un solo Docker Compose |
| Selección de modelo | `model=auto` del router | Detección de intención por el gateway |
| Configuración | Catálogo por provider | Variables de entorno OpenAI (`OPENAI_*`, `LLM_GATEWAY_MODEL_*`) |
| API keys | En el router (Docker secrets) | `OPENAI_API_KEY` (un solo endpoint) |
| Fallos | 503 del router + HITL | Reintentos con backoff + HITL |

### 2. Graph API Client Credentials vs Device Code (v6.0)

| Aspecto | Antes (Device Code Flow) | Ahora (Client Credentials) |
|---------|--------------------------|----------------------------|
| Interacción humana | Código en microsoft.com/devicelogin + MFA | Ninguna (100% desatendido) |
| App registration | Ninguna (client ID first-party de Office) | Propia, con permisos de aplicación + admin consent |
| Buzón | Delegado (`/me`) | App-only (`/users/{GRAPH_MAILBOX_UPN}`) |
| Tokens | Caché en disco (msal_token_cache.json) | Memoria, renovación automática |
| Expiración | Bloqueaba el worker esperando MFA | Renovación silenciosa vía MSAL |

### 3. Observabilidad

- **Loki + Grafana del agente**: métricas de negocio (triage, costos evitados, decisiones)
- El heartbeat del brain publica el estado del gateway en `hive:status:router_health`
- Telemetría por intención: `AgentTracer.log_telemetry(..., f"llm_gateway_{intent}")`

### 4. Truncación de Prompts

- **Email content:** 25K chars (suficiente para headers + cuerpo inicial)
- **SKILL.md:** 8K chars (contiene las reglas de negocio completas)
- **LEARNED_PROFILE.md:** 6K chars (solo behavioral rules + últimas sesiones)

### 5. Thread Consolidation (v5.2g)

- **Antes:** N+1 llamadas LLM por hilo de correo
- **Ahora:** 1 llamada LLM con todo el hilo consolidado
- Mensaje principal: primero, truncado a 10000 chars
- Previews de hilo: 500 chars cada uno
- Instrucción al final

### 6. HITL (Human-in-the-Loop)

- Ninguna acción se ejecuta sin aprobación humana
- Autenticación Graph API desatendida (no notifica códigos ni MFA)
- Firma HTML inyectada automáticamente (desde SKILL.md o env var)

---

## 10. Estructura de Archivos

```
src/
├── auto_learning.py              # Auto-aprendizaje de patrones
├── brain/
│   ├── brain_worker.py           # Triage + guardrails N1-N8
│   ├── cli.py                    # CLI interactiva
│   ├── llm_router_gateway.py     # Gateway LLM: intención + selección de modelo (API OpenAI)
│   ├── profile_updater.py        # Auto-aprendizaje (versión brain)
│   └── __init__.py
├── graph/
│   ├── graph_auth.py             # OAuth2 Client Credentials (MSAL, app-only)
│   ├── graph_client.py           # Cliente HTTP Graph API
│   └── __init__.py
├── vision/
│   ├── email_reader.py           # Coordinador de lectura
│   ├── sender_extractor.py       # Validación de sender
│   ├── vision_worker.py          # Loop de triage
│   └── __init__.py
├── notifier/
│   ├── notifier_worker.py        # Telegram HITL + comandos
│   └── __init__.py
├── observability/
│   ├── __init__.py
│   ├── metrics.py                # Cost tracking (router-proxied)
│   ├── telegram_bot.py           # Alertas Telegram
│   └── tracer.py                 # Trazabilidad a Loki + stdout
├── shared/
│   ├── __init__.py
│   ├── redis_client.py           # Queue tracing a Loki
│   └── utils.py                  # Utilidades
├── main.py
└── web/
    └── ...                       # Dashboard web multi-tenant

config/
├── SKILL.md                      # Reglas de negocio + firma HTML
├── LEARNED_PROFILE.md            # Perfil aprendido (auto-learning)
├── grafana/
│   ├── dashboards/
│   │   ├── agent_dashboard.json
│   │   └── c-level-tactical.json
│   └── provisioning/
│       ├── dashboards/main.yaml
│       └── datasources/loki.yaml

docker/
├── brain.Dockerfile
├── vision.Dockerfile
├── notifier.Dockerfile
└── web.Dockerfile
```

## 11. Archivos Eliminados / Legacy

| Archivo | Versión | Estado |
|---------|---------|--------|
| `src/vision/pwa_manager.py` | v2.0 | Eliminado |
| `src/vision/utils/dom_scripts.js` | v2.0 | Eliminado |
| `src/vision/token_manager.py` | v2.0 | Eliminado |
| `src/vision/graph_api_client.py` | v2.0 | Eliminado |
| `src/graph/browser_auth.py` | v4.0 | Eliminado |
| `secrets/gemini_api_key.txt` | v5.2 | Eliminado (router maneja keys) |
| `secrets/groq_api_key.txt` | v5.2 | Eliminado |
| `secrets/ollama_cloud_key.txt` | v5.2 | Eliminado |
| `secrets/openrouter_api_key.txt` | v5.2 | Eliminado |
| `secrets/llm7_api_key.txt` | v5.2 | Eliminado |
| `secrets/qwen_api_key.txt` | v5.2 | Eliminado |
| `secrets/aihubmix_key.txt` | v5.2 | Eliminado |
| `secrets/admin_auth_token.txt` | v5.2 | Eliminado |
| `secrets/grafana_pass.txt` | v5.2 | Eliminado |
