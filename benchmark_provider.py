#!/usr/bin/env python3
"""
Benchmark para un solo proveedor.
Uso: python benchmark_provider.py <provider>
Providers: gemini, groq, open_router, ollama_cloud
"""
import asyncio, time, json, os, re, sys, traceback
from openai import OpenAI
from google import genai
from google.genai import types
from groq import Groq
from ollama import AsyncClient

TEST_CASES = [
    {"name": "ACTION - Solicitud de aprobación", "expected": "Action",
     "dom_content": "De: Maria Cristina Santofimio Trujillo\nPara: Darlyn Fernedy Gonzalez Arias\nAsunto: Renovación certificado GAW\n\nCordial saludo,\n\nSolicito su aprobación para la renovación del certificado GAW (ODP-22277) el cual se vence el próximo 30 de junio.\n\nQuedo atenta a su respuesta.\n\nCordialmente,\nMaria Cristina Santofimio Trujillo\nAnalista de Seguridad"},
    {"name": "NOISE - Notificación automática", "expected": "Noise",
     "dom_content": "De: Sistema de Notificaciones\nPara: Todos los usuarios\nAsunto: Notificación: Actualización completada\n\nLa actualización del sistema se ha completado exitosamente.\n\nNo requiere ninguna acción de su parte.\n\nGracias,\nEquipo de Infraestructura"},
    {"name": "ACTION - Falla técnica en producción", "expected": "Action",
     "dom_content": "De: Maria Cristina Santofimio Trujillo\nPara: Darlyn Fernedy Gonzalez Arias\nCC: Telecomunicaciones\nAsunto: Falla en cifrado PGP en producción\n\nBuenos días,\n\nSe reporta una falla técnica en el cifrado PGP en ambiente de producción debido a incompatibilidad de llaves ECC vs RSA.\n\nSe recomienda el uso de llaves RSA para solucionar el problema.\n\nRequiere acción del equipo de Telecomunicaciones.\n\nCordialmente,\nMaria Cristina Santofimio Trujillo\nAnalista de Telecomunicaciones"},
    {"name": "NOISE - Reporte automático semanal", "expected": "Noise",
     "dom_content": "De: Sistema de Monitoreo\nPara: Darlyn Fernedy Gonzalez Arias\nAsunto: Reporte semanal de disponibilidad\n\nReporte de disponibilidad de la semana del 15 al 21 de junio:\n\nServidores: 99.97% disponibilidad\nBases de datos: 100% disponibilidad\nRed: 99.99% disponibilidad\n\nEste es un reporte automático generado por el sistema de monitoreo."},
    {"name": "ACTION - Solicitud de registro de equipo", "expected": "Action",
     "dom_content": "De: Carlos Hernan Castellanos Munoz\nPara: Telecomunicaciones;Darlyn Fernedy Gonzalez Arias\nAsunto: Registro Equipo para conexión remota Piso 25\n\nBuenos días,\n\nSolicito el registro de un equipo personal para conexión remota, según aprobación previa del Director.\n\nEl equipo es de Elsa Patricia Medina Herrera y requiere acceso a la red corporativa.\n\nQuedo atento.\n\nCordialmente,\nCarlos Hernan Castellanos Munoz"},
]

def get_first_key(secret_name):
    for path in [f"/run/secrets/{secret_name}", f"/run/secrets/{secret_name}.txt"]:
        if os.path.isfile(path):
            with open(path) as f:
                keys = [k.strip() for k in f.read().split(",") if k.strip() and "YOUAPI" not in k]
                return keys[0] if keys else ""
    return os.getenv(secret_name.upper(), "")

def extract_json(text):
    if not text: return {}
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    return {}

async def test_model(provider, model, api_key):
    results = []
    for tc in TEST_CASES:
        prompt = f"Analiza el siguiente correo y decide si es 'Noise', 'Action' o 'Executive_Decision'.\n\nDebes responder SOLO con un JSON con estos campos:\n- decision: 'Noise', 'Action' o 'Executive_Decision'\n- reasoning: Explicación breve\n- sender: Quién envía\n- to_recipients: Destinatarios\n\n--- INICIO DEL CORREO ---\n{tc['dom_content'][:20000]}\n--- FIN DEL CORREO ---"
        start = time.time()
        try:
            if provider == "gemini":
                c = genai.Client(api_key=api_key)
                cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1)
                r = await asyncio.get_event_loop().run_in_executor(None, lambda: c.models.generate_content(model=model, contents=prompt, config=cfg))
                data = extract_json(r.text)
            elif provider == "groq":
                c = Groq(api_key=api_key)
                r = c.chat.completions.create(model=model, messages=[{"role":"system","content":"You output JSON only."},{"role":"user","content":prompt}], response_format={"type":"json_object"}, temperature=0.1, timeout=90)
                data = extract_json(r.choices[0].message.content)
            elif provider == "open_router":
                c = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
                r = c.chat.completions.create(model=model, messages=[{"role":"system","content":"You output JSON only."},{"role":"user","content":prompt}], response_format={"type":"json_object"}, temperature=0.1, timeout=90, extra_headers={"HTTP-Referer":"https://jarvis.mail","X-OpenRouter-Title":"JarvisMail"})
                data = extract_json(r.choices[0].message.content)
            elif provider == "ollama_cloud":
                hdrs = {'Authorization': f'Bearer {api_key}'} if api_key else {}
                c = AsyncClient(host="https://ollama.com", headers=hdrs)
                r = await c.chat(model=model, messages=[{"role":"user","content":prompt}], format="json", options={"temperature":0.1})
                data = extract_json(r.get('message',{}).get('content',''))
            else:
                data = {}
            decision = data.get("decision", "unknown") if data else "error"
            correct = decision.lower() == tc["expected"].lower() if data else False
            results.append({"test": tc["name"], "expected": tc["expected"], "decision": decision, "correct": correct, "time_s": round(time.time()-start, 2), "status": "OK"})
        except Exception as e:
            results.append({"test": tc["name"], "expected": tc["expected"], "decision": "error", "correct": False, "time_s": round(time.time()-start, 2), "status": "ERROR", "error": str(e)[:100]})
        await asyncio.sleep(0.5)
    
    ok = [r for r in results if r["status"] == "OK"]
    correct_count = len([r for r in ok if r["correct"]])
    accuracy = round(correct_count / len(TEST_CASES) * 100, 1)
    avg_time = round(sum(r["time_s"] for r in ok) / len(ok), 2) if ok else 999.0
    return {"model": model, "accuracy": accuracy, "avg_time_s": avg_time, "correct": correct_count, "total": len(TEST_CASES), "tests": results}

async def main():
    provider = sys.argv[1] if len(sys.argv) > 1 else "gemini"
    
    MODELS = {
        "gemini": ["gemini-2.5-flash","gemini-2.5-pro","gemini-2.0-flash","gemini-2.0-flash-lite","gemini-2.5-flash-lite","gemini-3-flash-preview","gemini-3-pro-preview","gemini-3.1-pro-preview","gemini-3.1-flash-lite","gemma-4-31b-it","gemma-4-26b-a4b-it","gemini-3.5-flash"],
        "groq": ["llama-3.3-70b-versatile","llama-3.1-8b-instant","qwen/qwen3.6-27b","qwen/qwen3-32b","meta-llama/llama-4-scout-17b-16e-instruct","openai/gpt-oss-120b","openai/gpt-oss-20b","allam-2-7b","groq/compound","groq/compound-mini"],
        "open_router": ["cohere/north-mini-code:free","nex-agi/nex-n2-pro:free","nvidia/nemotron-3.5-content-safety:free","nvidia/nemotron-3-ultra-550b-a55b:free","nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free","poolside/laguna-xs.2:free","poolside/laguna-m.1:free","google/gemma-4-26b-a4b-it:free","google/gemma-4-31b-it:free","nvidia/nemotron-3-super-120b-a12b:free","liquid/lfm-2.5-1.2b-thinking:free","liquid/lfm-2.5-1.2b-instruct:free","nvidia/nemotron-3-nano-30b-a3b:free","nvidia/nemotron-nano-12b-v2-vl:free","qwen/qwen3-next-80b-a3b-instruct:free","nvidia/nemotron-nano-9b-v2:free","openai/gpt-oss-120b:free","openai/gpt-oss-20b:free","qwen/qwen3-coder:free","cognitivecomputations/dolphin-mistral-24b-venice-edition:free","meta-llama/llama-3.3-70b-instruct:free","meta-llama/llama-3.2-3b-instruct:free","nousresearch/hermes-3-llama-3.1-405b:free"],
        "ollama_cloud": ["gemma3:4b","gemma3:12b","gemma3:27b","gemma4:31b","deepseek-v3.2","deepseek-v3.1:671b","deepseek-v4-flash","kimi-k2.5","ministral-3:3b","ministral-3:8b"]
    }
    
    key_map = {"gemini":"gemini_api_key","groq":"groq_api_key","open_router":"openrouter_api_key","ollama_cloud":"ollama_cloud_key"}
    api_key = get_first_key(key_map[provider])
    
    if not api_key:
        print(f"ERROR: No API key for {provider}")
        sys.exit(1)
    
    all_results = []
    for model in MODELS.get(provider, []):
        print(f"\nTESTING: {model}")
        try:
            result = await test_model(provider, model, api_key)
            acc = result["accuracy"]
            tm = result["avg_time_s"]
            corr = result["correct"]
            total = result["total"]
            print(f"  RESULT: {acc}% accuracy ({corr}/{total}), avg time: {tm}s")
            all_results.append(result)
        except Exception as e:
            print(f"  ERROR: {e}")
            traceback.print_exc()
    
    # Sort safely
    try:
        all_results.sort(key=lambda r: (-(r["accuracy"] if isinstance(r.get("accuracy"), (int,float)) else 0), r.get("avg_time_s", 999) if isinstance(r.get("avg_time_s"), (int,float)) else 999))
    except:
        pass
    
    print(f"\n{'='*70}")
    print(f"  📋 LISTA COMPLETA — {provider.upper()}")
    print(f"{'='*70}")
    print(f"{'#'<5} {'Modelo':<50} {'Precisión':<10} {'Tiempo':<10}")
    print(f"{'-'*75}")
    for i, r in enumerate(all_results, 1):
        acc_s = f"{r['accuracy']:.1f}%" if isinstance(r.get('accuracy'), (int,float)) else f"{r.get('accuracy','?')}"
        tm_s = f"{r['avg_time_s']:.2f}s" if isinstance(r.get('avg_time_s'), (int,float)) else f"{r.get('avg_time_s','?')}"
        print(f"  {i:<3} {r['model'][:48]:<50} {acc_s:<10} {tm_s:<10}")
    
    print(f"\n{'='*70}")
    print(f"  🏆 TOP 5 — {provider.upper()}")
    print(f"{'='*70}")
    for i, r in enumerate(all_results[:5], 1):
        acc_s = f"{r['accuracy']:.1f}%" if isinstance(r.get('accuracy'), (int,float)) else f"{r.get('accuracy','?')}"
        tm_s = f"{r['avg_time_s']:.2f}s" if isinstance(r.get('avg_time_s'), (int,float)) else f"{r.get('avg_time_s','?')}"
        print(f"  {i}. {r['model'][:48]:<50} {acc_s:<10} {tm_s:<10}")
    
    # Save results
    out = f"/app/benchmark_{provider}.json"
    with open(out, "w") as f:
        json.dump({"provider": provider, "timestamp": time.time(), "results": all_results}, f, indent=2)
    print(f"\n📄 Resultados guardados en: {out}")

if __name__ == "__main__":
    asyncio.run(main())
