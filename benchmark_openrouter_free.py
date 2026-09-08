#!/usr/bin/env python3
"""
Benchmark de modelos gratuitos de OpenRouter.
Evalúa velocidad de respuesta y acertividad en clasificación de correos.
"""
import asyncio
import time
import json
import httpx
import os
import re
from openai import OpenAI

# ── 3 casos de prueba con diferentes clasificaciones esperadas ──
TEST_CASES = [
    {
        "name": "ACTION - Solicitud de aprobación",
        "expected": "Action",
        "data": {
            "action": "triage",
            "mail_id": "bench-action-1",
            "sender": "Maria Cristina Santofimio Trujillo",
            "subject": "Renovación certificado GAW",
            "dom_content": """De: Maria Cristina Santofimio Trujillo
Para: Darlyn Fernedy Gonzalez Arias
Asunto: Renovación certificado GAW

Cordial saludo,

Solicito su aprobación para la renovación del certificado GAW (ODP-22277) el cual se vence el próximo 30 de junio.

Quedo atenta a su respuesta.

Cordialmente,
Maria Cristina Santofimio Trujillo
Analista de Seguridad"""
        }
    },
    {
        "name": "NOISE - Notificación automática",
        "expected": "Noise",
        "data": {
            "action": "triage",
            "mail_id": "bench-noise-1",
            "sender": "Sistema de Notificaciones",
            "subject": "Notificación: Actualización completada",
            "dom_content": """De: Sistema de Notificaciones
Para: Todos los usuarios
Asunto: Notificación: Actualización completada

La actualización del sistema se ha completado exitosamente.

No requiere ninguna acción de su parte.

Gracias,
Equipo de Infraestructura"""
        }
    },
    {
        "name": "ACTION - Falla técnica en producción",
        "expected": "Action",
        "data": {
            "action": "triage",
            "mail_id": "bench-action-2",
            "sender": "Maria Cristina Santofimio Trujillo",
            "subject": "Falla en cifrado PGP en producción",
            "dom_content": """De: Maria Cristina Santofimio Trujillo
Para: Darlyn Fernedy Gonzalez Arias
CC: Telecomunicaciones
Asunto: Falla en cifrado PGP en producción

Buenos días,

Se reporta una falla técnica en el cifrado PGP en ambiente de producción debido a incompatibilidad de llaves ECC vs RSA.

Se recomienda el uso de llaves RSA para solucionar el problema.

Requiere acción del equipo de Telecomunicaciones para gestionar el cambio de llave.

Quedo atenta.

Cordialmente,
Maria Cristina Santofimio Trujillo
Analista de Telecomunicaciones"""
        }
    },
    {
        "name": "NOISE - Reporte automático semanal",
        "expected": "Noise",
        "data": {
            "action": "triage",
            "mail_id": "bench-noise-2",
            "sender": "Sistema de Monitoreo",
            "subject": "Reporte semanal de disponibilidad",
            "dom_content": """De: Sistema de Monitoreo
Para: Darlyn Fernedy Gonzalez Arias
Asunto: Reporte semanal de disponibilidad

Reporte de disponibilidad de la semana del 15 al 21 de junio:

Servidores: 99.97% disponibilidad
Bases de datos: 100% disponibilidad
Red: 99.99% disponibilidad

Este es un reporte automático generado por el sistema de monitoreo."""
        }
    },
    {
        "name": "ACTION - Solicitud de registro de equipo",
        "expected": "Action",
        "data": {
            "action": "triage",
            "mail_id": "bench-action-3",
            "sender": "Carlos Hernan Castellanos Munoz",
            "subject": "Registro Equipo para conexión remota Piso 25",
            "dom_content": """De: Carlos Hernan Castellanos Munoz
Para: Telecomunicaciones;Darlyn Fernedy Gonzalez Arias
Asunto: Registro Equipo para conexión remota Piso 25

Buenos días,

Solicito el registro de un equipo personal para conexión remota, según aprobación previa del Director.

El equipo es de Elsa Patricia Medina Herrera y requiere acceso a la red corporativa.

Quedo atento.

Cordialmente,
Carlos Hernan Castellanos Munoz"""
        }
    },
]


def get_first_key(secret_name: str) -> str:
    """Lee la primera API key de Docker Secrets o variables de entorno."""
    for path in [f"/run/secrets/{secret_name}", f"/run/secrets/{secret_name}.txt"]:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                keys = [k.strip() for k in f.read().split(",") if k.strip() and "YOUAPI" not in k]
                return keys[0] if keys else ""
    return os.getenv(secret_name.upper(), "")


async def fetch_free_models() -> list:
    """Obtiene la lista de modelos gratuitos desde OpenRouter."""
    key = get_first_key("openrouter_api_key")
    if not key:
        print("❌ No se encontró clave de OpenRouter en secrets/env")
        return []

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10
            )
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                # Filtrar solo modelos gratuitos (contienen ":free" en el ID)
                free_models = [m["id"] for m in models if ":free" in m.get("id", "").lower()]
                # También incluir "openrouter/free" si no está ya
                if "openrouter/free" not in free_models:
                    free_models.append("openrouter/free")
                return free_models
            else:
                print(f"❌ Error HTTP {resp.status_code} al consultar modelos")
                return []
        except Exception as e:
            print(f"❌ Error consultando modelos: {e}")
            return []


async def test_single_model(model_id: str, api_key: str, test_idx: int, test_case: dict) -> dict:
    """Prueba un modelo con un caso específico y mide rendimiento."""
    client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")

    data = test_case["data"]
    content = data.get("dom_content", "")
    sender_hint = data.get("sender", "")

    prompt = (
        "Analiza el siguiente correo y decide si es 'Noise', 'Action' o 'Executive_Decision'.\n\n"
        "Debes responder SOLO con un JSON con estos campos:\n"
        "- decision: 'Noise', 'Action' o 'Executive_Decision'\n"
        "- reasoning: Explicación breve de por qué\n"
        "- sender: Quién envía el correo\n"
        "- to_recipients: Destinatarios\n\n"
        f"--- INICIO DEL CORREO ---\n{content[:20000]}\n--- FIN DEL CORREO ---"
    )

    call_start = time.time()
    error_msg = None
    result_data = None
    p_tokens = 0
    c_tokens = 0

    try:
        resp = client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": "You output JSON only."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            extra_headers={
                "HTTP-Referer": "https://jarvis.mail",
                "X-OpenRouter-Title": "JarvisMail Benchmark",
            },
            timeout=60
        )

        elapsed = time.time() - call_start
        p_tokens = resp.usage.prompt_tokens if resp.usage else 0
        c_tokens = resp.usage.completion_tokens if resp.usage else 0

        resp_text = resp.choices[0].message.content
        if resp_text:
            # Extraer JSON
            json_match = re.search(r'\{.*\}', resp_text, re.DOTALL)
            if json_match:
                result_data = json.loads(json_match.group(0))
            else:
                error_msg = f"No se encontró JSON en respuesta: {resp_text[:100]}"
        else:
            error_msg = "Respuesta vacía"

    except Exception as e:
        elapsed = time.time() - call_start
        error_msg = str(e)[:200]

    decision = result_data.get("decision", "unknown") if result_data else "error"
    is_correct = (decision.lower() == test_case["expected"].lower()) if result_data else False

    return {
        "model": model_id,
        "test": test_case["name"],
        "expected": test_case["expected"],
        "decision": decision,
        "correct": is_correct,
        "time_s": round(elapsed, 2),
        "prompt_tokens": p_tokens,
        "completion_tokens": c_tokens,
        "total_tokens": p_tokens + c_tokens,
        "error": error_msg,
        "status": "ERROR" if error_msg else "OK"
    }


async def benchmark() -> list:
    """Ejecuta el benchmark completo."""
    api_key = get_first_key("openrouter_api_key")
    if not api_key:
        print("❌ No se encontró API key de OpenRouter")
        return []

    print("🔍 Obteniendo modelos gratuitos de OpenRouter...")
    free_models = await fetch_free_models()

    if not free_models:
        print("❌ No se encontraron modelos gratuitos")
        return []

    print(f"\n📋 Se encontraron {len(free_models)} modelos gratuitos:")
    for i, m in enumerate(free_models, 1):
        print(f"  {i}. {m}")

    # Limitar a máx 10 modelos para no gastar demasiados tokens
    if len(free_models) > 10:
        print(f"\n⚠️  Limitando a los primeros 10 modelos (de {len(free_models)})")
        free_models = free_models[:10]

    all_results = []

    for model_id in free_models:
        print(f"\n{'='*60}")
        print(f"  🧪 Probando: {model_id}")
        print(f"{'='*60}")

        model_results = []
        for idx, test_case in enumerate(TEST_CASES):
            print(f"  [{idx + 1}/{len(TEST_CASES)}] {test_case['name']}...", end=" ", flush=True)
            result = await test_single_model(model_id, api_key, idx, test_case)
            model_results.append(result)

            status = "✅" if result["status"] == "OK" else "❌"
            correct = "✓" if result.get("correct") else "✗"
            print(f"{status} {result['time_s']:.1f}s | {result['decision']:<15} ({correct}) | {result.get('total_tokens', 0)} tokens")

            # Pequeña pausa entre requests para evitar rate limiting
            await asyncio.sleep(0.5)

        # Calcular estadísticas del modelo
        ok_tests = [r for r in model_results if r["status"] == "OK"]
        correct_tests = [r for r in ok_tests if r.get("correct")]
        avg_time = sum(r["time_s"] for r in ok_tests) / len(ok_tests) if ok_tests else 999
        accuracy = len(correct_tests) / len(TEST_CASES) * 100
        avg_tokens = sum(r.get("total_tokens", 0) for r in ok_tests) / len(ok_tests) if ok_tests else 0

        print(f"\n  📊 {model_id[:50]}:")
        print(f"     Precisión: {accuracy:.0f}% ({len(correct_tests)}/{len(TEST_CASES)})")
        print(f"     Tiempo promedio: {avg_time:.2f}s")
        print(f"     Tokens promedio: {avg_tokens:.0f}")

        all_results.append({
            "model": model_id,
            "accuracy": accuracy,
            "avg_time_s": round(avg_time, 2),
            "avg_tokens": round(avg_tokens, 0),
            "correct_count": len(correct_tests),
            "total_tests": len(TEST_CASES),
            "tests": model_results
        })

        # Pausa entre modelos
        await asyncio.sleep(1)

    return all_results


def print_ranking(results: list):
    """Imprime el ranking ordenado por performance."""
    print(f"\n\n{'='*70}")
    print(f"  🏆 RANKING DE MODELOS GRATUITOS OPENROUTER")
    print(f"{'='*70}")

    if not results:
        print("  No hay resultados para mostrar.")
        return

    # Ordenar: primero por precisión (desc), luego por velocidad (asc)
    ranked = sorted(results, key=lambda r: (-r["accuracy"], r["avg_time_s"]))

    print(f"\n{'#' <4} {'Modelo':<50} {'Precisión':<10} {'Tiempo':<10} {'Tokens':<10}")
    print(f"{'-'*84}")
    for i, r in enumerate(ranked, 1):
        acc = f"{r['accuracy']:.0f}%"
        tm = f"{r['avg_time_s']:.1f}s"
        tk = f"{r['avg_tokens']:.0f}"
        model_short = r['model'][:48]
        print(f"{i:<4} {model_short:<50} {acc:<10} {tm:<10} {tk:<10}")

    print(f"\n{'='*70}")
    best = ranked[0] if ranked else None
    if best:
        print(f"  🥇 RECOMENDADO: {best['model']}")
        print(f"     Precisión: {best['accuracy']:.0f}% | Tiempo: {best['avg_time_s']:.1f}s promedio")
    print(f"{'='*70}")


async def main():
    print(f"{'='*70}")
    print(f"  🧪 BENCHMARK DE MODELOS GRATUITOS OPENROUTER")
    print(f"  Prueba de velocidad y acertividad en clasificación de correos")
    print(f"{'='*70}")
    print(f"\n📧 {len(TEST_CASES)} casos de prueba:")
    for tc in TEST_CASES:
        print(f"  • {tc['name']} → se espera '{tc['expected']}'")

    results = await benchmark()

    print_ranking(results)

    # Guardar resultados a JSON
    output = {
        "timestamp": time.time(),
        "total_models_tested": len(results),
        "test_cases": [tc["name"] for tc in TEST_CASES],
        "ranking": [
            {
                "rank": i + 1,
                "model": r["model"],
                "accuracy_pct": r["accuracy"],
                "avg_time_s": r["avg_time_s"],
                "avg_tokens": r["avg_tokens"],
                "correct_count": r["correct_count"],
                "total_tests": r["total_tests"]
            }
            for i, r in enumerate(sorted(results, key=lambda r: (-r["accuracy"], r["avg_time_s"])))
        ],
        "full_results": results
    }

    with open("benchmark_results.json", "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n📄 Resultados guardados en: benchmark_results.json")

    # Recomendación final
    if results:
        best = sorted(results, key=lambda r: (-r["accuracy"], r["avg_time_s"]))[0]
        print(f"\n💡 RECOMENDACIÓN: {best['model']}")
        print(f"   ({best['accuracy']:.0f}% precisión, {best['avg_time_s']:.1f}s tiempo promedio)")


if __name__ == "__main__":
    asyncio.run(main())
