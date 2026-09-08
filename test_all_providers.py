#!/usr/bin/env python3
"""Test all providers with their free models for triage performance."""
import asyncio
import time
import json
import sys

CONTENIDO = '''De: Sistema de Seguridad
Para: Administrador IT
Asunto: ALERTA: Intento de acceso no autorizado

Se ha detectado un intento de acceso no autorizado al panel de administracion.
IP: 203.0.113.45 | Usuario: admin | Hora: 2026-06-19 14:23:05 UTC
Estado: Bloqueado por firewall.'''

TEST_DATA = {
    'action': 'triage',
    'mail_id': 'test-exhaustivo',
    'sender': 'Sistema de Seguridad',
    'subject': 'ALERTA: Intento de acceso no autorizado',
    'dom_content': CONTENIDO
}

PROVIDERS = [
    ("1. Gemini", "gemini", "gemma-4-31b-it"),
    ("2. Groq", "groq", "llama-3.3-70b-versatile"),
    ("3. Ollama Cloud", "ollama_cloud", "nemotron-3-ultra"),
    ("4. OpenRouter", "openrouter", "openrouter/free"),
]

async def test_provider(label, provider, model, router, broker):
    print(f"\n{'='*50}")
    print(f"  {label}")
    print(f"  Provider: {provider} | Modelo: {model}")
    print(f"{'='*50}")
    
    await broker.client.set('hive:config:provider', provider)
    await broker.client.set('hive:config:model', model)
    await asyncio.sleep(0.3)
    
    prov = await broker.client.get('hive:config:provider')
    mod = await broker.client.get('hive:config:model')
    print(f"  Redis OK: {prov}/{mod}")
    
    start = time.time()
    try:
        result = await router.triage(TEST_DATA)
        elapsed = time.time() - start
        decision = result.get('decision', 'unknown')
        confidence = result.get('confidence', 'unknown')
        print(f"  ⏱  {elapsed:.2f}s | Decisión: {decision} | Confianza: {confidence}")
        return {"provider": provider, "model": model, "time": elapsed, "decision": decision, "status": "OK", "result": result}
    except Exception as e:
        elapsed = time.time() - start
        err_str = str(e)[:200]
        print(f"  ❌ ERROR tras {elapsed:.2f}s: {err_str}")
        return {"provider": provider, "model": model, "time": elapsed, "error": err_str, "status": "ERROR"}

async def main():
    from brain.llm_router import LLMRouter
    from shared.redis_client import RedisTaskBroker
    
    broker = RedisTaskBroker()
    await broker.connect()
    
    router = LLMRouter(broker)
    
    results = []
    for label, provider, model in PROVIDERS:
        r = await test_provider(label, provider, model, router, broker)
        results.append(r)
        await asyncio.sleep(1)
    
    print(f"\n{'='*60}")
    print(f"  RESUMEN DE RESULTADOS")
    print(f"{'='*60}")
    print(f"{'Provider':<20} {'Modelo':<30} {'Tiempo':<10} {'Status'}")
    print(f"{'-'*70}")
    for r in results:
        status = "✅ OK" if r["status"] == "OK" else "❌ ERROR"
        tiempo = f"{r['time']:.2f}s" if r.get('time') else "N/A"
        modelo = r.get('model', '')[:28]
        print(f"{r['provider']:<20} {modelo:<30} {tiempo:<10} {status}")
    
    await broker.client.set('hive:config:provider', 'gemini')
    await broker.client.set('hive:config:model', 'gemma-4-31b-it')
    print(f"\n✅ Config restaurada: gemini/gemma-4-31b-it")
    
    return results

if __name__ == '__main__':
    results = asyncio.run(main())
    print(f"\nResultados JSON:")
    print(json.dumps(results, indent=2, default=str))
