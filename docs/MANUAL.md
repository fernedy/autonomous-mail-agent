# 📘 MANUAL OPENCODE + GENTLE-AI

> **Versión:** 2.0 | **OpenCode:** 1.14.32 | **Gentle-AI:** 1.36.8
> **Modelos gratuitos incorporados:** `opencode/deepseek-v4-flash-free` · `opencode/mimo-v2.5-free`
> **Aplica a:** CUALQUIER proyecto — sin API keys, sin configuración extra

---

## Índice

1. [Filosofía: Sin API keys](#1-filosofía-sin-api-keys)
2. [Qué tienes instalado](#2-qué-tienes-instalado)
3. [Cómo usar OpenCode al instante](#3-cómo-usar-opencode-al-instante)
4. [Gentle-AI: El ecosistema completo](#4-gentle-ai-el-ecosistema-completo)
5. [SDD: Spec-Driven Development](#5-sdd-spec-driven-development)
6. [Engram: Memoria persistente entre sesiones](#6-engram-memoria-persistente-entre-sesiones)
7. [GGA: Guardian Angel (Git Hooks)](#7-gga-guardian-angel-git-hooks)
8. [Skills: Cómo usarlas, crearlas y descubrirlas](#8-skills-cómo-usarlas-crearlas-y-descubrirlas)
9. [Custom Commands](#9-custom-commands)
10. [Atajos de teclado esenciales](#10-atajos-de-teclado-esenciales)
11. [Flujo de trabajo recomendado](#11-flujo-de-trabajo-recomendado)
12. [Solución de problemas](#12-solución-de-problemas)

---

## 1. Filosofía: Sin API keys

**OpenCode YA incluye modelos gratuitos.** No necesitas registrarte en nada, no necesitas API keys, no necesitas tarjeta de crédito.

### Modelos gratuitos disponibles (prefijo `opencode/`)

| Modelo ID | Uso recomendado |
|---|---|
| `opencode/deepseek-v4-flash-free` | 🧠 **Coder** — Tareas complejas, refactor, debugging |
| `opencode/mimo-v2.5-free` | ⚡ **Task/Title** — Tareas rápidas, titulares |
| `opencode/nemotron-3-ultra-free` | 🔬 Alternativa para razonamiento |

Estos modelos **ya vienen con OpenCode**. No hay que instalar nada, no hay que configurar nada.

### ¿Y si quiero usar mis propias API keys?

OpenCode también soporta tus propias keys (Groq, Gemini, OpenAI, Anthropic, etc.) mediante `opencode providers login`. Pero **no es necesario**. Con los modelos `opencode/*` tienes todo lo que necesitas.

---

## 2. Qué tienes instalado

| Componente | Propósito |
|---|---|
| **OpenCode** v1.14.32 | Agente de codificación con IA en terminal |
| **Config global** `~/.opencode.json` | Configuración que aplica a TODOS los proyectos |
| **Gentle-AI** v1.36.8 | Ecosistema: memoria, skills, flujos SDD, git hooks |
| **Engram** v1.16.1 | Memoria persistente entre sesiones |
| **GGA** | Guardian Angel — hooks de git automáticos |
| **Custom commands** | `user:prime-context` para cargar contexto rápido |

---

## 🥇 Regla de Oro: Skills + Contexto siempre

> **Siempre que entres a un proyecto, ejecuta `/init` o `user:prime-context` primero.**
> **Siempre consulta y usa las skills disponibles para cada tarea.**
> Esto carga las skills, el contexto y las reglas del proyecto.
> Sin contexto ni skills, el agente trabaja a ciegas. Con ambos, trabaja como un senior.

---

## 3. Cómo usar OpenCode al instante

### Paso 1: Abrir OpenCode

```bash
# Desde cualquier directorio
opencode

# O desde un proyecto específico
opencode -c /ruta/de/mi/proyecto
```

### Paso 2: Inicializar contexto del proyecto

Dentro de OpenCode, ejecuta:

```
/init
```

Esto analiza tu proyecto y crea un `AGENTS.md` con la estructura, dependencias y comandos del proyecto.

### Paso 3: Empieza a trabajar

Escribe `i` para enfocar el editor y escribe tu petición. Ejemplos:

```text
📝 "Explícame este proyecto en 2 minutos"
🔧 "Encuentra bugs en src/main.py"
📋 "Haz code review de los últimos cambios"
🧪 "Escribe tests unitarios"
```

### Cambiar de modelo sobre la marcha

Presiona **`Ctrl+O`** para abrir el selector de modelos. Elige entre los gratuitos (`opencode/*`) o los que hayas configurado.

---

## 4. Gentle-AI: El ecosistema completo

Gentle-AI transforma OpenCode de "un chat que escribe código" a un **ecosistema profesional** con memoria, flujos de trabajo y habilidades.

### Comandos principales de Gentle-AI

```bash
# Diagnóstico del ecosistema
gentle-ai doctor

# Sincronizar configuraciones con la versión actual
gentle-ai sync

# Refrescar registry de skills
gentle-ai skill-registry refresh

# Ver estado de fase SDD
gentle-ai sdd-status

# Actualizar Gentle-AI
gentle-ai update

# Abrir interfaz interactiva (TUI)
gentle-ai
```

### Inicializar en un proyecto nuevo

```bash
cd /ruta/de/mi/proyecto
```

Dentro de OpenCode, ejecuta:

```
/sdd-init
```

> **Nota:** `/sdd-init` funciona si tu agente lo soporta. Si no, no es obligatorio — puedes empezar a trabajar directamente.
> Gentle-AI se encarga del resto automáticamente.

Esto configura automáticamente:
- Detección del stack tecnológico
- Activación de modo TDD si hay tests
- Hooks de git via GGA

> Solo necesitas ejecutarlo UNA vez por proyecto, o cuando agregues/quites frameworks de testing.

---

## 5. SDD: Spec-Driven Development

SDD es un flujo de trabajo estructurado que divide el desarrollo en fases, cada una con un objetivo específico.

### Las fases de SDD

Dentro de OpenCode, presiona **`Tab`** para cambiar entre fases:

| Fase | Propósito | Qué hace el agente |
|---|---|---|
| `gentle-orchestrator` | 🎭 **Orquestador** | Decide qué fase ejecutar según la tarea |
| `sdd-design` | 📐 **Diseño** | Planifica arquitectura, analiza requisitos |
| `sdd-implement` | 💻 **Implementación** | Escribe código siguiendo el diseño |
| `sdd-review` | 👁️ **Revisión** | Code review del código implementado |
| `sdd-test` | 🧪 **Tests** | Escribe y ejecuta tests |

### Flujo de trabajo SDD recomendado

```
1. Pides una feature → gentile-orchestrator evalúa
2. Pasa a sdd-design → planifica la solución
3. Pasa a sdd-implement → escribe el código
4. Pasa a sdd-review → revisa lo escrito
5. Pasa a sdd-test → verifica con tests
```

Puedes saltar fases si no son necesarias (ej: un cambio trivial no necesita diseño).

### Ver estado actual de SDD

```bash
# Desde terminal
gentle-ai sdd-status
```

---

## 6. Engram: Memoria persistente entre sesiones

Engram permite que OpenCode **recuerde decisiones, bugs y contexto** entre sesiones, aunque cierres la terminal.

### Iniciar Engram

```bash
# En una terminal, inicia el servicio de memoria
engram serve &
```

### Comandos de Engram

```bash
# Ver todos los proyectos con memoria
engram projects list

# Buscar decisiones pasadas
engram search "por qué elegimos X"

# Consolidar nombres (evita duplicados como "app" vs "mi-app")
engram projects consolidate

# Visualizar memoria
engram tui
```

### ¿Qué guarda Engram automáticamente?

- Decisiones arquitectónicas
- Bugs encontrados y solucionados
- Configuraciones importantes
- El "por qué" de cada decisión

---

## 7. GGA: Guardian Angel (Git Hooks)

GGA protege tu código instalando hooks de git automáticos que:

- Ejecutan análisis de código antes de cada commit
- Previenen commits con errores
- Aseguran calidad mínima

### Inicializar GGA en un proyecto

```bash
# En la raíz de tu proyecto
gga init
gga install
```

### Qué hacen los hooks

| Hook | Cuándo se ejecuta | Qué verifica |
|---|---|---|
| `pre-commit` | Antes de cada commit | Sintaxis, archivos sin seguimiento |
| `pre-push` | Antes de hacer push | Tests, linting |

---

## 8. Skills: Cómo usarlas, crearlas y descubrirlas

Las Skills (habilidades) son flujos de trabajo reutilizables que le enseñan al agente a realizar tareas específicas. Gentle-AI mantiene un registro centralizado de todas las skills disponibles.

### Dónde se guardan las skills

| Ubicación | Propósito |
|---|---|
| `~/.config/opencode/` | Skills globales del usuario (aplican a TODOS los proyectos) |
| `.claude/skills/` en el proyecto | Skills específicas del proyecto (se versionan en git) |
| `~/.gentle-ai/` | Skills gestionadas por Gentle-AI (registry automático) |

### Refrescar el registry de skills

Cuando entres a un proyecto nuevo o instales nuevas skills:

```bash
# Desde terminal
gentle-ai skill-registry refresh
```

O dentro de OpenCode:

```
/skill-registry refresh
```

### Skills disponibles por defecto

Gentle-AI incluye estas skills preinstaladas:

| Skill | Propósito | Cuándo se activa |
|---|---|---|
| **Code Review** | Revisión de código automatizada | Al pedir "revisa este código" |
| **Testing** | Generación y ejecución de tests | Al pedir "escribe tests" |
| **Documentation** | Generación de documentación | Al pedir "documenta esto" |
| **Refactoring** | Refactorización segura | Al pedir "refactoriza" |
| **Debugging** | Diagnóstico de errores | Al pegar un error |

Las skills se activan **automáticamente** según el contexto de tu petición.

### Cómo crear tu propio skill

Los skills se definen en archivos `SKILL.md`. Ejemplo:

```markdown
# my-custom-skill

**Description:** Una skill para desplegar a producción
**Triggers:** deploy, producción, release

## Steps

1. Ejecuta los tests
2. Construye el proyecto
3. Despliega

## Rules

- Preguntar antes de hacer deploy a producción
- Verificar que los tests pasen
```

Guarda este archivo como `~/.config/opencode/skills/my-custom-skill/SKILL.md` y luego ejecuta `gentle-ai skill-registry refresh` para registrarlo.

### Cómo descubrir nuevas skills

- **Gentle-AI TUI:** Ejecuta `gentle-ai` y navega a la sección de skills
- **Comunidad:** Revisa el repositorio de Gentle-AI en GitHub para skills compartidas
- **Proyectos:** Clona proyectos y revisa sus `.claude/skills/` para aprender nuevas skills

---

## 9. Custom Commands

### `user:prime-context` — Cargar contexto del proyecto

Cuando entres a un proyecto, ejecuta esto dentro de OpenCode:

```
user:prime-context
```

Carga el `AGENTS.md` y la documentación del proyecto para que el agente entienda el contexto.

### Crear tus propios comandos

Crea archivos `.md` en `~/.opencode/commands/`:

```bash
# Ejemplo: comando para ver el estado de git
cat > ~/.opencode/commands/git-status.md << 'EOF'
# Estado de Git
RUN git status --short
RUN git diff --stat
EOF
```

Luego dentro de OpenCode escribe: `user:git-status`

### Comandos con argumentos

```bash
cat > ~/.opencode/commands/read-file.md << 'EOF'
# Leer archivo
RUN cat $FILE_PATH | head -$LINES
EOF
```

OpenCode te pedirá los valores de `$FILE_PATH` y `$LINES` al ejecutar el comando.

---

## 10. Atajos de teclado esenciales

### Globales

| Tecla | Acción |
|---|---|
| `Ctrl+C` | Salir de OpenCode |
| `?` o `Ctrl+?` | Ayuda rápida |
| `Ctrl+L` | Ver logs |
| `Ctrl+A` | Cambiar de sesión |
| `Ctrl+K` | Diálogo de comandos |
| `Ctrl+O` | Cambiar modelo / proveedor |
| `Esc` | Cerrar panel / volver |

### Chat y editor

| Tecla | Acción |
|---|---|
| `Ctrl+N` | Nueva sesión |
| `Ctrl+X` | Cancelar generación en curso |
| `i` | Enfocar editor (modo escritura) |
| `Ctrl+S` o `Enter` | Enviar mensaje |
| `Ctrl+E` | Abrir editor externo (VS Code, etc.) |
| `Tab` | Cambiar fase SDD (con Gentle-AI) |

### Gestión de sesiones

| Tecla | Acción |
|---|---|
| `↑` / `k` | Sesión anterior |
| `↓` / `j` | Siguiente sesión |
| `Enter` | Seleccionar sesión |

---

## 11. Flujo de trabajo recomendado

### Para un proyecto nuevo

```bash
# 1. Entrar al proyecto
cd /ruta/de/mi/proyecto

# 2. Iniciar Engram (memoria persistente)
engram serve &

# 3. Abrir OpenCode
opencode

# 4. Dentro de OpenCode:
#    - /init                  → Analiza el proyecto
#    - /sdd-init              → Inicializa SDD
#    - user:prime-context     → Carga contexto
#    - Tab → sdd-design      → Planifica
#    - Tab → sdd-implement   → Implementa
```

### Para un proyecto existente

```bash
# 1. Abrir OpenCode directamente
opencode

# 2. user:prime-context → Cargar contexto
# 3. ¡A trabajar!
```

### Día a día

```text
1. Abro OpenCode en mi proyecto
2. Pido lo que necesito:
   - "Agrega validación a este formulario"
   - "Explica este algoritmo"
   - "Refactoriza esta función"
3. OpenCode + Gentle-AI trabaja con memoria y skills
4. Si necesito cambiar de fase SDD, presiono Tab
5. Engram guarda automáticamente las decisiones importantes
```

---

## 12. Solución de problemas

### OpenCode no abre la TUI correctamente

```bash
# Modo debug para ver errores
opencode -d

# Revisar logs
cat ~/.opencode/logs/*.log 2>/dev/null | tail -50
```

### "command not found: opencode"

```bash
# Verificar que el PATH está bien
source ~/.bashrc
which opencode
# Debe mostrar: /home/fernedy/.opencode/bin/opencode
```

### Gentle-AI no reconoce el proyecto

```bash
# Ejecutar doctor para diagnóstico
gentle-ai doctor

# Si falta state.json, ejecutar install
gentle-ai install
```

### Engram no responde

```bash
# Iniciar el servicio
engram serve &

# Verificar
curl http://localhost:7437/health
```

### Los modelos `opencode/*` no aparecen

```bash
# Listar todos los modelos disponibles
opencode models | grep opencode/
```

---

## 🚀 Cheatsheet rápido

```bash
# ABRIR
opencode                          # Desde cualquier proyecto
opencode -c /ruta                 # Desde una ruta específica

# DENTRO DE OPENCODE
i              → Escribir mensaje
Ctrl+S/Enter   → Enviar
Ctrl+O         → Cambiar modelo
Ctrl+X         → Cancelar
Ctrl+N         → Nueva sesión
Tab            → Cambiar fase SDD
/?             → Ayuda
/init          → Inicializar proyecto
/sdd-init      → Inicializar SDD
user:prime-context → Cargar contexto

# GENTLE-AI (terminal)
gentle-ai doctor            → Diagnóstico
gentle-ai sync              → Sincronizar
gentle-ai skill-registry refresh  → Refrescar skills
engram serve &              → Iniciar memoria
engram search "texto"       → Buscar en memoria
```

---

> **¿Dudas?** Ejecuta `gentle-ai doctor` para diagnóstico completo.
> **Repo:** [github.com/Gentleman-Programming/gentle-ai](https://github.com/Gentleman-Programming/gentle-ai)
