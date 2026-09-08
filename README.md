# 📬 Autonomous Mail Agent — Multi-Provider LLM Orchestrator

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Docker Compose](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](#despliegue-con-docker)
[![Architecture](https://img.shields.io/badge/architecture-event--driven-orange)](#arquitectura-del-sistema)
[![LLM Routing](https://img.shields.io/badge/routing-ZenMux%20%7C%20OpenRouter-8A2BE2)](#llm-routing--gateway)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Sistema backend distribuido y autónomo diseñado para la extracción, clasificación, razonamiento semántico y respuesta automatizada de correo electrónico corporativo a gran escala.

Construido sobre una arquitectura desacoplada por eventos con colas en **Redis**, integración nativa con **Microsoft Graph API**, observabilidad integrada (**Prometheus + Grafana**) y enrutamiento inteligente de LLMs con failover automático.

---

## 🏛️ Arquitectura del Sistema

El procesamiento se desacopla en tres capas independientes sincronizadas por workers:

```
                  ┌──────────────────────┐
                  │  Microsoft Graph API │
                  └──────────┬───────────┘
                             │
                      [Vision Worker] (Ingesta & Extracción)
                             │
                       (Redis Queue)
                             │
                      [Brain Worker]  ◄──► [LLM Router Gateway]
                             │             (ZenMux / OpenRouter / Local)
                       (Redis Queue)
                             │
                     [Notifier Worker]
                             │
                  ┌──────────┴───────────┐
                  │  SMTP / Webhooks / MS │
                  └──────────────────────┘
```

1. **Ingestion & Extraction (`vision_worker`):** Monitorea bandejas vía Graph API, extrae remitentes, metadatos y normaliza el cuerpo de mensajes.
2. **Cognitive Routing (`brain_worker`):** Implementa el gateway de modelos de lenguaje (`llm_router_gateway.py`), seleccionando dinámicamente el proveedor óptimo evaluando coste, latencia y disponibilidad.
3. **Dispatch & Action (`notifier_worker`):** Ejecuta acciones autónomas: redactar respuestas contextuales, clasificar incidencias y emitir alertas operativas.
4. **Observabilidad:** Métricas operativas exportadas hacia Prometheus y paneles pre-aprovisionados en Grafana (`config/grafana/`).

---

## ⚡ Enrutamiento Inteligente de LLMs (Gateway)

El módulo cognitivo integra redundancia activa:
* **Failover transparente:** Transición automática entre ZenMux, OpenRouter y proveedores directos ante degradación de SLAs o cuotas excedidas.
* **Evaluación de modelos:** Suite de benchmarking integrada (`benchmark_provider.py`, `benchmark_all_providers.py`) para medir derivas de latencia y costo por token.
* **Memoria y Perfiles:** Módulo `auto_learning.py` con perfiles semánticos persistentes.

---

## 📂 Estructura del Repositorio

```text
.
├── config/              # Dashboards Grafana y perfiles semánticos
├── docker/              # Dockerfiles optimizados para workers
├── docs/                # Documentación extendida y diagramas técnicos
│   └── assets/          # Diagramas de arquitectura
├── src/
│   ├── brain/           # Workers cognitivos y LLM Router Gateway
│   ├── graph/           # Clientes de Microsoft Graph API y OAuth2
│   ├── notifier/        # Despachador de notificaciones y respuestas
│   ├── observability/   # Tracing y exportador de métricas
│   ├── shared/          # Clientes Redis y utilidades transversales
│   └── vision/          # Procesamiento e ingesta de correos
├── test/                # Suite de pruebas unitarias y de integración
├── docker-compose.yml   # Orquestación multicontenedor de la plataforma
└── requirements.txt     # Dependencias del servicio backend
```

---

## 🚀 Inicio Rápido

### Requisitos previos
* Python 3.11+
* Docker y Docker Compose
* Instancia de Redis 7+ (incluida en el compose)

### Configuración de variables de entorno

```bash
cp .env.example .env
```

Edita el archivo `.env` con tus credenciales de Microsoft Entra ID (Graph API), API Keys de los proveedores LLM y configuración de Redis.

### Despliegue con Docker

```bash
docker compose up -d --build
```

### Ejecución de Pruebas y Benchmarks

```bash
# Ejecutar suite de pruebas unitarias
pytest test/ -v

# Ejecutar benchmark de latencia/tokens contra los proveedores configurados
python benchmark_provider.py
```

---

## 📊 Métricas y Monitoreo

Una vez levantada la infraestructura:
* **Grafana:** `http://localhost:3000` (Dashboards automáticos cargados desde `config/grafana/provisioning/`)
* **Prometheus Metrics:** Puerto expuesto según configuración en `.env`
