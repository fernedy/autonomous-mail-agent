# Arquitectura Multi-Tenant — JARVISMAIL

> **Versión:** v1.0 — Diseño inicial
> **Fecha:** 2026-07-02
> **Estado:** Borrador

---

## 1. Problema

Actualmente JARVISMAIL opera con **1 instancia = 1 buzón**. La autenticación
v6.0 usa **Graph API Client Credentials** (`GraphAPIAuth`, app-only) con
`GRAPH_TENANT_ID` explícito, lo que ya permite apuntar a cualquier tenant M365,
pero:

- `GraphClient` tiene 1 credencial → 1 buzón (`GRAPH_MAILBOX_UPN`)
- `VisionWorker` procesa 1 cola → 1 buzón
- `BrainWorker` no tiene contexto de tenant en el triage
- No hay aislamiento de datos entre tenants

Para dar servicio multi-tenant real (varias empresas/clientes con su propio M365), se necesita:

---

## 2. Arquitectura Propuesta

```
┌─────────────────────────────────────────────────────────────┐
│                    JARVISMAIL Multi-Tenant                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                │
│  │ Tenant A  │   │ Tenant B  │   │ Tenant C  │  ...         │
│  │ Vision    │   │ Vision    │   │ Vision    │              │
│  │ Worker    │   │ Worker    │   │ Worker    │              │
│  │ (pool)    │   │ (pool)    │   │ (pool)    │              │
│  └─────┬─────┘  └─────┬─────┘  └─────┬─────┘              │
│        │               │               │                    │
│        └───────────────┼───────────────┘                    │
│                        │                                    │
│               ┌────────▼────────┐                           │
│               │   Redis Broker   │                           │
│               │  (colas por      │                           │
│               │   tenant)        │                           │
│               └────────┬────────┘                           │
│                        │                                    │
│               ┌────────▼────────┐                           │
│               │   Brain Pool     │                           │
│               │  (stateless,     │                           │
│               │   con tenant_id  │                           │
│               │   en contexto)   │                           │
│               └────────┬────────┘                           │
│                        │                                    │
│               ┌────────▼────────┐                           │
│               │   Notifier       │                           │
│               │  (filtra por     │                           │
│               │   tenant_id)     │                           │
│               └─────────────────┘                           │
│                                                              │
│  ┌──────────────────────────────────────────────────┐       │
│  │  Web Dashboard (Postgres)                        │       │
│  │  - CRUD Tenants                                  │       │
│  │  - Status (active/inactive)                      │       │
│  │  - Tokens por tenant                             │       │
│  │  - Config individual                             │       │
│  └──────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Componentes

### 3.1 Tenant Registry (Postgres)

El web dashboard ya tiene un modelo `Tenant` con:
- `id` (UUID)
- `name` (nombre de la organización)
- `domain` (dominio M365)
- `status` (active/inactive)
- `config` (JSONB con settings específicos)

**Campos adicionales necesarios:**
```json
{
  "graph_client_id": "00000000-0000-0000-0000-000000000000",
  "graph_tenant_id": "{tenant_id}",
  "graph_client_secret": "(almacenado cifrado, nunca en claro)",
  "mailbox_upn": "usuario@empresa.com",
  "max_emails_per_hour": 100,
  "timezone": "America/Bogota",
  "telegram_chat_id": "-1001234567890"
}
```

### 3.2 Vision Worker Pool

**Opción recomendada: Pool de workers con tenant_id como variable de entorno.**

```yaml
# docker-compose.yml
vision-worker-tenant-a:
  image: jarvis-vision:v6.0
  environment:
    - GRAPH_TENANT_ID=tenant-a
    - GRAPH_CLIENT_ID=...
    - GRAPH_MAILBOX_UPN=usuario-a@empresa.com
  secrets: [graph_client_secret_tenant_a]
  
vision-worker-tenant-b:
  image: jarvis-vision:v6.0
  environment:
    - GRAPH_TENANT_ID=tenant-b
    - GRAPH_CLIENT_ID=...
    - GRAPH_MAILBOX_UPN=usuario-b@empresa.com
  secrets: [graph_client_secret_tenant_b]
```

**Cambios en VisionWorker:**
```python
class VisionWorker:
    def __init__(self):
        self.tenant_id = os.getenv("TENANT_ID", "default")
        # Cada worker tiene su propio GraphAPIAuth y GraphClient
        self.auth = GraphAPIAuth()  # lee GRAPH_TENANT_ID/GRAPH_CLIENT_ID/secret
        self.graph = GraphClient(auth=self.auth)  # usa GRAPH_MAILBOX_UPN
        # Cola con prefijo de tenant
        self.queue_in = f"queue:{self.tenant_id}:vision_tasks"
        self.queue_out = f"queue:{self.tenant_id}:raw_emails"
```

### 3.3 Brain Worker — Contexto de Tenant

El brain debe recibir `tenant_id` en cada tarea para:
1. Cargar el SKILL.md específico del tenant
2. Cargar el LEARNED_PROFILE.md específico del tenant
3. Enviar notificaciones al chat de Telegram correcto

**Formato de tarea:**
```json
{
  "tenant_id": "tenant-a-uuid",
  "mail_id": "...",
  "sender": "...",
  "subject": "...",
  "body": "..."
}
```

### 3.4 Colas Redis con Namespace

Cada tenant tiene sus propias colas para aislamiento:

```
queue:tenant_a:raw_emails
queue:tenant_a:vision_tasks
queue:tenant_a:notifications
queue:tenant_b:raw_emails
queue:tenant_b:vision_tasks
queue:tenant_b:notifications
```

### 3.5 Notifier — Enrutamiento por Tenant

El notifier debe:
1. Recibir `tenant_id` en cada notificación
2. Buscar el `telegram_chat_id` del tenant en Postgres
3. Enviar al chat correcto

---

## 4. Flujo Multi-Tenant Completo

```
1. Tenant admin crea tenant via Web Dashboard
2. Admin registra un app registration en SU tenant de Azure AD con
   permisos de aplicación Mail.ReadWrite / Mail.Send / User.Read.All
   y aprueba el admin consent (Client Credentials, sin interacción posterior)
3. Credenciales del tenant guardadas cifradas en Postgres
   (graph_tenant_id, graph_client_id, graph_client_secret, mailbox_upn)
4. Vision Worker del tenant arranca y adquiere token desatendidamente
5. Cada correo procesado lleva tenant_id en la metadata
6. Brain Worker usa tenant_id para cargar config específica
7. Notifier envía al Telegram chat del tenant correcto
8. Grafana filtra por tenant_id label
```

---

## 5. Seguridad y Aislamiento

| Aspecto | Estrategia |
|---------|-----------|
| **Tokens** | Cifrados en Postgres o Redis, 1 por tenant |
| **Datos** | Colas Redis con namespace por tenant |
| **Workers** | 1 container Docker por tenant |
| **Brain** | Sin estado, tenant_id en cada request |
| **Grafana** | Label `tenant_id` en Loki para filtrar dashboards |
| **Web** | JWT con tenant_id en claims |

---

## 6. Migración desde Single-Tenant

### Fase 1: Preparación (1 día)
- [ ] Agregar `tenant_id` como campo opcional en `VisionWorker.__init__`
- [ ] Agregar `tenant_id` a todas las colas (compatibilidad hacia atrás)
- [ ] Actualizar `tracer.py` para emitir `tenant_id` en logs

### Fase 2: Tenant Registry (2 días)
- [ ] CRUD completo de tenants en Web Dashboard
- [ ] Página de autenticación M365 por tenant
- [ ] Almacenamiento cifrado de tokens

### Fase 3: Pool de Workers (2 días)
- [ ] Script `docker-compose.multi-tenant.yml` con workers por tenant
- [ ] Health check por tenant
- [ ] Dashboard de estado de tenants

### Fase 4: Brain Multi-Tenant (1 día)
- [ ] Carga de SKILL.md por tenant_id
- [ ] Carga de LEARNED_PROFILE.md por tenant_id
- [ ] Notificaciones enrutadas por tenant

---

## 7. Costos Estimados por Tenant

| Recurso | Por Tenant | 5 Tenants |
|---------|-----------|-----------|
| Vision Worker | 0.5 CPU / 512MB RAM | 2.5 CPU / 2.5GB RAM |
| Brain (compartido) | 0.5 CPU / 512MB RAM | 0.5 CPU / 512MB RAM |
| Redis (compartido) | — | 1 CPU / 1GB RAM |
| Loki (compartido) | — | 1 CPU / 1GB RAM |
| Postgres (compartido) | — | 0.5 CPU / 512MB RAM |
| **Total** | ~1.5 CPU / 1.5GB RAM | ~6 CPU / 6GB RAM |

---

## 8. Alternativas Consideradas

### Alternativa A: Single Worker con Rotación de Tokens ❌
- Un solo VisionWorker que rota tokens por tenant
- **Problema:** Complejidad de estado, cuellos de botella, riesgo de fuga de datos

### Alternativa B: Multi-Instancia con Docker Compose ✅ **(RECOMENDADA)**
- Un container por tenant con su propio entorno
- **Ventaja:** Aislamiento total, escalado independiente, fácil debugging
- **Desventaja:** Más consumo de recursos

### Alternativa C: Kubernetes con Namespaces
- Ideal para >10 tenants
- **Ventaja:** Orquestación automática, health checks, auto-scaling
- **Desventaja:** Overhead operativo para pocos tenants

---

## 9. Próximos Pasos

1. **Fase 1:** Agregar `tenant_id` a VisionWorker, BrainWorker, Tracer
2. **Fase 2:** Completar CRUD de tenants en Web Dashboard
3. **Fase 3:** Desplegar segundo tenant de prueba
4. **Fase 4:** Validar aislamiento de datos y notificaciones

---

*Documento de diseño. Pendiente de revisión y aprobación.*
