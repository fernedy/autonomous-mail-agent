#!/usr/bin/env python3
"""
Benchmark multi-provider de modelos gratuitos.
Evalúa velocidad de respuesta y acertividad en clasificación de correos
para Gemini, Groq, OpenRouter y Ollama Cloud.
Genera ranking por provider y ranking global.
"""
import asyncio
import time
import json
import os
import re
import sys
from openai import OpenAI
from google import genai
from google.genai import types
from groq import Groq
from ollama import AsyncClient

# ═══════════════════════════════════════════════
#  CONFIGURACIÓN
# ═══════════════════════════════════════════════

# Dónde guardar resultados
OUTPUT_FILE = "benchmark_all_results.json"

# Límite de modelos a probar por provider (0 = sin límite)
MAX_MODELS_PER_PROVIDER = {
    "gemini": 12,      # Los principales modelos chat
    "groq": 10,        # Excluir especializados (prompt-guard, orpheus)
    "open_router": 23, # Todos los free
    "ollama_cloud": 10 # Los principales
}

# Tiempo máximo por llamada (segundos)
TIMEOUT_SECS = 90

# Pausa entre requests para evitar rate limiting
PAYLOAD_DELAY = 1.0

# ── Casos de prueba ──
TEST_CASES = [
    {
        "name": "ACTION - Solicitud de aprobación",
        "expected": "Action",
        "data": {
            "action": "triage", "mail_id": "bench-action-1",
            "sender": "Maria Cristina Santofimio Trujillo",
            "subject": "Renovación certificado GAW",
            "dom_content": (
                "De: Maria Cristina Santofimio Trujillo\n"
                "Para: Darlyn Fernedy Gonzalez Arias\n"
                "Asunto: Renovación certificado GAW\n\n"
                "Cordial saludo,\n\n"
                "Solicito su aprobación para la renovación del certificado "
                "GAW (ODP-22277) el cual se vence el próximo 30 de junio.\n\n"
                "Quedo atenta a su respuesta.\n\n"
                "Cordialmente,\nMaria Cristina Santofimio Trujillo\nAnalista de Seguridad"
            )
        }
    },
    {
        "name": "NOISE - Notificación automática",
        "expected": "Noise",
        "data": {
            "action": "triage", "mail_id": "bench-noise-1",
            "sender": "Sistema de Notificaciones",
            "subject": "Notificación: Actualización completada",
            "dom_content": (
                "De: Sistema de Notificaciones\n"
                "Para: Todos los usuarios\n"
                "Asunto: Notificación: Actualización completada\n\n"
                "La actualización del sistema se ha completado exitosamente.\n\n"
                "No requiere ninguna acción de su parte.\n\n"
                "Gracias,\nEquipo de Infraestructura"
            )
        }
    },
    {
        "name": "ACTION - Falla técnica en producción",
        "expected": "Action",
        "data": {
            "action": "triage", "mail_id": "bench-action-2",
            "sender": "Maria Cristina Santofimio Trujillo",
            "subject": "Falla en cifrado PGP en producción",
            "dom_content": (
                "De: Maria Cristina Santofimio Trujillo\n"
                "Para: Darlyn Fernedy Gonzalez Arias\n"
                "CC: Telecomunicaciones\n"
                "Asunto: Falla en cifrado PGP en producción\n\n"
                "Buenos días,\n\n"
                "Se reporta una falla técnica en el cifrado PGP en ambiente "
                "de producción debido a incompatibilidad de llaves ECC vs RSA.\n\n"
                "Se recomienda el uso de llaves RSA para solucionar el problema.\n\n"
                "Requiere acción del equipo de Telecomunicaciones.\n\n"
                "Cordialmente,\nMaria Cristina Santofimio Trujillo\n"
                "Analista de Telecomunicaciones"
            )
        }
    },
    {
        "name": "NOISE - Reporte automático semanal",
        "expected": "Noise",
        "data": {
            "action": "triage", "mail_id": "bench-noise-2",
            "sender": "Sistema de Monitoreo",
            "subject": "Reporte semanal de disponibilidad",
            "dom_content": (
                "De: Sistema de Monitoreo\n"
                "Para: Darlyn Fernedy Gonzalez Arias\n"
                "Asunto: Reporte semanal de disponibilidad\n\n"
                "Reporte de disponibilidad de la semana del 15 al 21 de junio:\n\n"
                "Servidores: 99.97% disponibilidad\n"
                "Bases de datos: 100% disponibilidad\n"
                "Red: 99.99% disponibilidad\n\n"
                "Este es un reporte automático generado por el sistema de monitoreo."
            )
        }
    },
    {
        "name": "ACTION - Solicitud de registro de equipo",
        "expected": "Action",
        "data": {
            "action": "triage", "mail_id": "bench-action-3",
            "sender": "Carlos Hernan Castellanos Munoz",
            "subject": "Registro Equipo para conexión remota Piso 25",
            "dom_content": (
                "De: Carlos Hernan Castellanos Munoz\n"
                "Para: Telecomunicaciones;Darlyn Fernedy Gonzalez Arias\n"
                "Asunto: Registro Equipo para conexión remota Piso 25\n\n"
                "Buenos días,\n\n"
                "Solicito el registro de un equipo personal para conexión "
                "remota, según aprobación previa del Director.\n\n"
                "El equipo es de Elsa Patricia Medina Herrera y requiere "
                "acceso a la red corporativa.\n\n"
                "Quedo atento.\n\n"
                "Cordialmente,\nCarlos Hernan Castellanos Munoz"
            )
        }
    },
]

# ═══════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════

def get_first_key(secret_name: str) -> str:
    for path in [f"/run/secrets/{secret_name}", f"/run/secrets/{secret_name}.txt"]:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                keys = [k.strip() for k in f.read().split(",") if k.strip() and "YOUAPI" not in k]
                return keys[0] if keys else ""
    return os.getenv(secret_name.upper(), "")

def extract_json(text):
    if not text:
        return {}
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {}

TEST_PROMPT = """Analiza el siguiente correo y decide si es 'Noise', 'Action' o 'Executive_Decision'.

Debes responder SOLO con un JSON con estos campos:
- decision: 'Noise', 'Action' o 'Executive_Decision'
- reasoning: Explicación breve de por qué
- sender: Quién envía el correo
- to_recipients: Destinatarios

--- INICIO DEL CORREO ---
{content}
--- FIN DEL CORREO ---"""

# ═══════════════════════════════════════════════
#  TEST RUNNER GENÉRICO
# ═══════════════════════════════════════════════

async def run_single_test(provider: str, model: str, test_case: dict,
                          api_key: str) -> dict:
    """Prueba un modelo con un caso específico."""
    content = test_case["data"]["dom_content"]
    prompt = TEST_PROMPT.format(content=content[:20000])

    call_start = time.time()
    error_msg = None
    result_data = None
    p_tokens, c_tokens = 0, 0

    try:
        if provider == "gemini":
            client = genai.Client(api_key=api_key)
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1
            )
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(
                None, lambda: client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
            )
            p_tokens = resp.usage_metadata.prompt_token_count if resp.usage_metadata else 0
            c_tokens = resp.usage_metadata.candidates_token_count if resp.usage_metadata else 0
            result_data = extract_json(resp.text)

        elif provider == "groq":
            client = Groq(api_key=api_key)
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You output JSON only."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=TIMEOUT_SECS
            )
            p_tokens = resp.usage.prompt_tokens if resp.usage else 0
            c_tokens = resp.usage.completion_tokens if resp.usage else 0
            result_data = extract_json(resp.choices[0].message.content)

        elif provider == "open_router":
            client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You output JSON only."},
                    {"role": "user", "content": prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
                timeout=TIMEOUT_SECS,
                extra_headers={
                    "HTTP-Referer": "https://jarvis.mail",
                    "X-OpenRouter-Title": "JarvisMail Benchmark",
                }
            )
            p_tokens = resp.usage.prompt_tokens if resp.usage else 0
            c_tokens = resp.usage.completion_tokens if resp.usage else 0
            result_data = extract_json(resp.choices[0].message.content)

        elif provider == "ollama_cloud":
            headers = {'Authorization': f'Bearer {api_key}'} if api_key else {}
            client = AsyncClient(host="https://ollama.com", headers=headers)
            resp = await client.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                format="json",
                options={"temperature": 0.1}
            )
            p_tokens = resp.get('prompt_eval_count', 0)
            c_tokens = resp.get('eval_count', 0)
            result_data = extract_json(resp.get('message', {}).get('content', ''))

    except Exception as e:
        elapsed = time.time() - call_start
        error_msg = str(e)[:200]

    elapsed = time.time() - call_start
    decision = result_data.get("decision", "unknown") if result_data else "error"
    is_correct = decision.lower() == test_case["expected"].lower() if result_data else False

    return {
        "model": model, "provider": provider,
        "test": test_case["name"], "expected": test_case["expected"],
        "decision": decision, "correct": is_correct,
        "time_s": round(elapsed, 2),
        "prompt_tokens": p_tokens, "completion_tokens": c_tokens,
        "total_tokens": p_tokens + c_tokens,
        "error": error_msg,
        "status": "ERROR" if error_msg else "OK"
    }

# ═══════════════════════════════════════════════
#  BENCHMARK POR PROVIDER
# ═══════════════════════════════════════════════

async def benchmark_provider(provider: str, models: list, api_key: str) -> list:
    """Ejecuta benchmark completo para un provider."""
    if not api_key:
        print(f"\n  ❌ No hay API key para {provider}, saltando...")
        return []

    print(f"\n{'='*70}")
    print(f"  🧪 BENCHMARK: {provider.upper()} ({len(models)} modelos)")
    print(f"{'='*70}")

    all_results = []

    for idx, model in enumerate(models, 1):
        print(f"\n  [{idx}/{len(models)}] 📡 {model}")
        model_results = []

        for tidx, test_case in enumerate(TEST_CASES, 1):
            print(f"    Test {tidx}/{len(TEST_CASES)}: {test_case['name'][:40]}...", end=" ", flush=True)
            result = await run_single_test(provider, model, test_case, api_key)
            model_results.append(result)

            status = "✅" if result["status"] == "OK" else "❌"
            correct = "✓" if result.get("correct") else "✗"
            print(f"{status} {result['time_s']:.1f}s | {result['decision']:<15} ({correct})", end="")
            if result.get("error"):
                print(f" | {result['error'][:60]}", end="")
            print()

            await asyncio.sleep(PAYLOAD_DELAY)

        # Estadísticas del modelo
        ok_tests = [r for r in model_results if r["status"] == "OK"]
        correct_tests = [r for r in ok_tests if r.get("correct")]
        avg_time = sum(r["time_s"] for r in ok_tests) / len(ok_tests) if ok_tests else 999
        accuracy = len(correct_tests) / len(TEST_CASES) * 100 if ok_tests else 0
        avg_tokens = sum(r.get("total_tokens", 0) for r in ok_tests) / len(ok_tests) if ok_tests else 0

        print(f"    📊 Precisión: {accuracy:.0f}% ({len(correct_tests)}/{len(TEST_CASES)}) | "
              f"Tiempo: {avg_time:.2f}s | Tokens: {avg_tokens:.0f}")

        all_results.append({
            "model": model, "provider": provider,
            "accuracy": accuracy, "avg_time_s": round(avg_time, 2),
            "avg_tokens": round(avg_tokens, 0),
            "correct_count": len(correct_tests), "total_tests": len(TEST_CASES),
            "tests": model_results
        })

        await asyncio.sleep(1)

    return all_results

def safe_sort_key(r):
    """Key de ordenamiento segura que maneja tipos mixtos."""
    acc = r.get("accuracy", 0)
    if not isinstance(acc, (int, float)):
        acc = 0
    tm = r.get("avg_time_s", 999)
    if not isinstance(tm, (int, float)):
        tm = 999
    return (-acc, tm)

def print_provider_ranking(provider: str, results: list):
    """Imprime Top 5 de un provider."""
    try:
        ranked = sorted(results, key=safe_sort_key)
    except Exception:
        ranked = results
    print(f"\n{'='*70}")
    print(f"  🏆 TOP 5 — {provider.upper()}")
    print(f"{'='*70}")
    print(f"{'#'<5} {'Modelo':<48} {'Precisión':<10} {'Tiempo':<10} {'Tokens':<10}")
    print(f"{'-'*83}")
    for i, r in enumerate(ranked[:5], 1):
        acc_val = r.get('accuracy', 0)
        acc = f"{acc_val:.0f}%" if isinstance(acc_val, (int, float)) else f"{acc_val}%"
        tm_val = r.get('avg_time_s', 999)
        tm = f"{tm_val:.1f}s" if isinstance(tm_val, (int, float)) else f"{tm_val}s"
        tk_val = r.get('avg_tokens', 0)
        tk = f"{tk_val:.0f}" if isinstance(tk_val, (int, float)) else str(tk_val)
        m = r['model'][:46]
        print(f"{i:<5} {m:<48} {acc:<10} {tm:<10} {tk:<10}")

def print_full_list(provider: str, results: list):
    """Imprime lista completa de modelos con resultados."""
    try:
        ranked = sorted(results, key=safe_sort_key)
    except Exception:
        ranked = results
    print(f"\n{'='*70}")
    print(f"  📋 LISTA COMPLETA — {provider.upper()} ({len(ranked)} modelos)")
    print(f"{'='*70}")
    print(f"{'#'<5} {'Modelo':<48} {'Precisión':<10} {'Tiempo':<10} {'Tokens':<10} {'Estado':<10}")
    print(f"{'-'*93}")
    for i, r in enumerate(ranked, 1):
        acc = f"{r['accuracy']:.0f}%"
        tm = f"{r['avg_time_s']:.1f}s"
        tk = f"{r['avg_tokens']:.0f}"
        m = r['model'][:46]
        ok_count = len([t for t in r.get('tests', []) if t['status'] == 'OK'])
        status = "✅" if ok_count == r['total_tests'] else f"⚠️ {ok_count}/{r['total_tests']}"
        print(f"{i:<5} {m:<48} {acc:<10} {tm:<10} {tk:<10} {status:<10}")

def print_global_ranking(all_results: dict):
    """Imprime ranking global Top 10."""
    all_models = []
    for prov, results in all_results.items():
        all_models.extend(results)

    try:
        ranked = sorted(all_models, key=safe_sort_key)
    except Exception:
        ranked = all_models
    print(f"\n\n{'='*80}")
    print(f"  🌍 RANKING GLOBAL — TODOS LOS PROVIDERS")
    print(f"{'='*80}")
    print(f"{'#'<5} {'Modelo':<50} {'Provider':<15} {'Precisión':<10} {'Tiempo':<10}")
    print(f"{'-'*90}")
    for i, r in enumerate(ranked[:10], 1):
        acc = f"{r['accuracy']:.0f}%"
        tm = f"{r['avg_time_s']:.1f}s"
        m = r['model'][:48]
        p = r['provider'][:13]
        print(f"{i:<5} {m:<50} {p:<15} {acc:<10} {tm:<10}")

# ═══════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════

async def main():
    print(f"{'='*80}")
    print(f"  🧪 BENCHMARK MULTI-PROVIDER DE MODELOS GRATUITOS")
    print(f"  Prueba de velocidad y acertividad en clasificación de correos")
    print(f"{'='*80}")
    print(f"\n📧 {len(TEST_CASES)} casos de prueba:")
    for tc in TEST_CASES:
        print(f"  • {tc['name']} → se espera '{tc['expected']}'")

    # ── Obtener API keys ──
    keys = {
        "gemini": get_first_key("gemini_api_key"),
        "groq": get_first_key("groq_api_key"),
        "open_router": get_first_key("openrouter_api_key"),
        "ollama_cloud": get_first_key("ollama_cloud_key"),
    }

    for prov, k in keys.items():
        if k:
            print(f"  ✅ {prov}: API key encontrada")
        else:
            print(f"  ❌ {prov}: NO HAY API KEY")

    # ── Modelos por provider ──
    gemini_chat_models = [
        "gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash",
        "gemini-2.0-flash-lite", "gemini-2.5-flash-lite",
        "gemini-3-flash-preview", "gemini-3-pro-preview",
        "gemini-3.1-pro-preview", "gemini-3.1-flash-lite",
        "gemma-4-31b-it", "gemma-4-26b-a4b-it", "gemini-3.5-flash",
    ]

    groq_chat_models = [
        "llama-3.3-70b-versatile", "llama-3.1-8b-instant",
        "qwen/qwen3.6-27b", "qwen/qwen3-32b",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "openai/gpt-oss-120b", "openai/gpt-oss-20b",
        "allam-2-7b", "groq/compound", "groq/compound-mini",
    ]

    openrouter_free_models = [
        "cohere/north-mini-code:free", "nex-agi/nex-n2-pro:free",
        "nvidia/nemotron-3.5-content-safety:free",
        "nvidia/nemotron-3-ultra-550b-a55b:free",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "poolside/laguna-xs.2:free", "poolside/laguna-m.1:free",
        "google/gemma-4-26b-a4b-it:free", "google/gemma-4-31b-it:free",
        "nvidia/nemotron-3-super-120b-a12b:free",
        "liquid/lfm-2.5-1.2b-thinking:free",
        "liquid/lfm-2.5-1.2b-instruct:free",
        "nvidia/nemotron-3-nano-30b-a3b:free",
        "nvidia/nemotron-nano-12b-v2-vl:free",
        "qwen/qwen3-next-80b-a3b-instruct:free",
        "nvidia/nemotron-nano-9b-v2:free",
        "openai/gpt-oss-120b:free", "openai/gpt-oss-20b:free",
        "qwen/qwen3-coder:free",
        "cognitivecomputations/dolphin-mistral-24b-venice-edition:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "meta-llama/llama-3.2-3b-instruct:free",
        "nousresearch/hermes-3-llama-3.1-405b:free",
    ]

    ollama_cloud_chat_models = [
        "gemma3:4b", "gemma3:12b", "gemma3:27b", "gemma4:31b",
        "deepseek-v3.2", "deepseek-v3.1:671b", "deepseek-v4-flash",
        "kimi-k2.5", "ministral-3:3b", "ministral-3:8b",
    ]

    providers_config = [
        ("gemini", gemini_chat_models, keys["gemini"]),
        ("groq", groq_chat_models, keys["groq"]),
        ("open_router", openrouter_free_models, keys["open_router"]),
        ("ollama_cloud", ollama_cloud_chat_models, keys["ollama_cloud"]),
    ]

    all_results = {}

    for provider, models, api_key in providers_config:
        if not api_key:
            print(f"\n⚠️  No hay API key para {provider}, saltando...")
            all_results[provider] = []
            continue

        results = await benchmark_provider(provider, models, api_key)
        all_results[provider] = results

        if results:
            print_provider_ranking(provider, results)
            print_full_list(provider, results)

    # ── Ranking global ──
    print_global_ranking(all_results)

    # ── Guardar resultados ──
    output = {
        "timestamp": time.time(),
        "test_cases": [tc["name"] for tc in TEST_CASES],
        "providers": {}
    }
    for prov, results in all_results.items():
        ranked = sorted(results, key=safe_sort_key)
        output["providers"][prov] = {
            "total_models_tested": len(results),
            "ranking": [
                {"rank": i+1, "model": r["model"],
                 "accuracy_pct": r["accuracy"], "avg_time_s": r["avg_time_s"],
                 "correct_count": r["correct_count"], "total_tests": r["total_tests"]}
                for i, r in enumerate(ranked)
            ],
            "full_results": results
        }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n\n📄 Resultados completos guardados en: {OUTPUT_FILE}")

    # ── Recomendación final por provider ──
    print(f"\n\n{'='*80}")
    print(f"  💡 RECOMENDACIONES POR PROVIDER")
    print(f"{'='*80}")
    for prov, results in all_results.items():
        if results:
            ranked = sorted(results, key=safe_sort_key)
            best = ranked[0]
            print(f"\n  🏅 {prov.upper()}: {best['model']}")
            print(f"     Precisión: {best['accuracy']:.0f}% | Tiempo: {best['avg_time_s']:.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
