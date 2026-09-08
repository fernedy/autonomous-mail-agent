import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from src.shared.redis_client import RedisTaskBroker

@pytest.mark.asyncio
async def test_flight():
    broker = RedisTaskBroker()
    broker.client = MagicMock()
    broker.client.lpush = AsyncMock()
    broker.client.llen = AsyncMock(return_value=1)
    
    # Payload de prueba simulando un triage ejecutivo
    test_payload = {
        "type": "info",
        "event": "VUELO DE PRUEBA - JARVISMAIL V2.1",
        "subject": "Validación de Conectividad Telegram",
        "decision": "Action Required",
        "reasoning": "Prueba de integración técnica del microservicio Notifier bajo arquitectura de colas Redis."
    }
    
    print("🚀 Inyectando mensaje de prueba en queue:notifications...")
    await broker.push_task("queue:notifications", test_payload)
    print("✅ Inyección completada. Verifique su Telegram, Director.")

if __name__ == "__main__":
    asyncio.run(test_flight())