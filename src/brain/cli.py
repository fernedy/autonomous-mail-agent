import os
import json
import asyncio
import logging
from shared.redis_client import RedisTaskBroker

logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger("brain.cli")

# v6.0: CLI simplificado — el LLM Router Gateway es embebido y selecciona el
# modelo por intención vía variables de entorno (OPENAI_* / LLM_GATEWAY_*).
# Este CLI solo muestra estado y permite el override en caliente del modelo
# por defecto (hive:config:model).

# Intenciones del gateway y su variable de entorno asociada
INTENT_ENV_VARS = {
    "email_triage": "LLM_GATEWAY_MODEL_TRIAGE",
    "ui_action": "LLM_GATEWAY_MODEL_UI",
    "profile_analysis": "LLM_GATEWAY_MODEL_PROFILE",
    "general": "LLM_GATEWAY_MODEL",
}


def _env(name: str, default: str = "N/A") -> str:
    return os.getenv(name) or default


async def show_status(broker):
    """Muestra el estado actual del sistema y del gateway."""
    prov_val = await broker.client.get("hive:config:provider")
    model_val = await broker.client.get("hive:config:model")
    provider = prov_val.decode() if isinstance(prov_val, bytes) else (prov_val or "N/A")
    model_override = model_val.decode() if isinstance(model_val, bytes) else (model_val or "N/A")

    health_val = await broker.client.get("hive:status:router_health")
    health_raw = health_val.decode() if isinstance(health_val, bytes) else (health_val or "")
    try:
        health = json.loads(health_raw)
    except Exception:
        health = {}

    print(f"\n{'='*60}")
    print(f"🧠 JARVISMAIL — ESTADO ACTUAL")
    print(f"{'='*60}")
    print(f"\n📋 CONFIGURACIÓN:")
    print(f"   Provider:     {provider}")
    print(f"   Override:     {model_override}")
    print(f"   Gateway:      LLM Router Gateway (embebido)")
    print(f"   Base URL:     {_env('OPENAI_BASE_URL', 'https://api.openai.com/v1')}")
    print(f"   API Key:      {'✅ configurada' if _env('OPENAI_API_KEY', '') != 'N/A' else '❌ falta OPENAI_API_KEY'}")
    print(f"   Estado:       {health.get('status', 'N/A')}")

    print(f"\n🎯 MODELOS POR INTENCIÓN (detección automática):")
    for intent, env_var in INTENT_ENV_VARS.items():
        print(f"   {intent:<18} -> {_env(env_var)}  [{env_var}]")

    print(f"\n💡 RECOMENDACIÓN:")
    print(f"   El gateway detecta la intención y elige el modelo automáticamente.")
    print(f"   Para ajustar modelos, define LLM_GATEWAY_MODEL_* en el .env.")
    print(f"   Override manual en caliente: set <provider> <modelo>")
    print(f"\n   COMANDOS:")
    print(f"   status        — Mostrar esta pantalla")
    print(f"   set <p> <m>   — Override del modelo por defecto (hive:config:*)")
    print(f"   exit / 0      — Salir")
    print(f"{'='*60}\n")


async def interactive_setup():
    broker = RedisTaskBroker()
    await broker.connect()

    await show_status(broker)

    while True:
        try:
            cmd = input("> ").strip()
            if not cmd:
                continue

            parts = cmd.split()
            action = parts[0].lower()

            if action in ("exit", "0", "quit", "q", "salir"):
                print("👋 Hasta luego.")
                break

            elif action == "status":
                await show_status(broker)

            elif action == "set" and len(parts) >= 3:
                provider = parts[1]
                model = parts[2]
                await broker.client.set("hive:config:provider", provider)
                await broker.client.set("hive:config:model", model)
                print(f"\n✅ Override configurado: provider={provider}, model={model}")
                print(f"💡 El gateway aplica este override a triage/uso general; "
                      f"las intenciones especializadas conservan su modelo del .env")

            elif action == "set":
                print("⚠️  Uso: set <provider> <modelo>")
                print(f"   Ejemplo: set openai gpt-4o-mini")

            else:
                print("⚠️  Comando no reconocido. Usa: status, set, exit")

        except KeyboardInterrupt:
            print("\n👋 Hasta luego.")
            break
        except Exception as e:
            print(f"❌ Error: {e}")


if __name__ == "__main__":
    asyncio.run(interactive_setup())
