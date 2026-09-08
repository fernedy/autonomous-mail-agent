#!/bin/bash
# 🧹 JARVISMAIL V2.0: PURGA Y REESTRUCTURACIÓN DE MICROSERVICIOS

echo "🚀 Iniciando limpieza del monolito..."

# 1. Crear nueva estructura de directorios
mkdir -p src/brain src/vision src/notifier src/shared docker config

# 2. Preservar Conocimiento (Skills)
echo "🧠 Preservando Skills y Perfil..."
mv SKILL.md/ config/ 2>/dev/null || mv SKILL.md config/
mv LEARNED_PROFILE.md/ config/ 2>/dev/null || mv LEARNED_PROFILE.md config/

# 3. Mover Lógica a Microservicios
echo "📦 Distribuyendo lógica..."
mv src/llm_router.py src/brain/ 2>/dev/null
mv src/skill_manager.py src/shared/ 2>/dev/null
mv src/notifier.py src/notifier/ 2>/dev/null
mv src/browser/pwa_manager.py src/vision/ 2>/dev/null
mv src/browser/dom_scripts.js src/vision/ 2>/dev/null

# 4. Eliminar Basura y Logs (Clean Slate)
echo "🗑️ Eliminando logs y archivos obsoletos..."
rm -f main.py Dockerfile docker-compose.yml entrypoint.sh grafana.log grafana_ascii.log
rm -rf logs/ src/browser/ src/core/

echo "✅ Estructura v2.0 lista. Cada servicio tendrá su propio Dockerfile en /docker."