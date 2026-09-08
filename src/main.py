# [src/vision/vision_worker.py]
import os
import asyncio
from vision.pwa_manager import PWAManager  # ✅ Ahora Python lo encontrará en /app/src/vision/
from shared.redis_client import RedisTaskBroker # ✅ Y este en /app/src/shared/
from brain.llm_router_gateway import LLMRouterGateway

async def jarvis_mail_orchestrator():
    print("🛡️🤖 JARVISMAIL ONLINE | SISTEMA DE GESTIÓN EJECUTIVA")
    brain = LLMRouterGateway()
    await brain.initialize_engine() 
    vision = PWAManager()
    telegram = TelegramNotifier()
    
    processed_count = 0

    try:
        await vision.start_session()
        await vision.navigate_to_outlook()
        
        while True:
            await vision.apply_unread_filter()
            raw_emails = await vision.extract_recent_emails(limit=10)
            
            for mail in raw_emails:
                # 1. Triage con Gemma 4 y razonamiento para Loki
                analysis = await brain.triage(mail['subject_preview'])
                conv_id = mail['conv_id']
                
                if analysis.get("category") == "Noise":
                    # Punto 1: Marcar como leído en silencio usando shortcut 'q'
                    await vision.mark_as_read(conv_id)
                else:
                    # 2. HITL - Preparar borrador inyectando en el DOM
                    if analysis.get("draft_reply"):
                        await vision.prepare_draft(conv_id, analysis['draft_reply'])
                    
                    # 3. Notificar y BLOQUEAR hasta recibir tu orden
                    await telegram.send_hitl_notification(mail['subject_preview'], analysis, conv_id)
                    
                    # El agente se "entera" de tu respuesta aquí
                    user_decision = await telegram.wait_for_decision(conv_id)
                    
                    # 4. Ejecutar la decisión humana en Outlook
                    await vision.handle_user_decision(user_decision, conv_id)
                    print(f"✅ Acción ejecutada satisfactoriamente.")

                processed_count += 1

                # Punto 4: Auto-aprendizaje cada 10 correos
                if processed_count % 10 == 0:
                    print("🧠 Evolucionando: Sincronizando estilo del Director...")
                    sent_data = await vision.get_sent_emails(limit=10)
                    await brain.evolve_skills(sent_data) 
                    await vision.navigate_to_outlook()

            await asyncio.sleep(60) 
    finally:
        await vision.close()

if __name__ == "__main__":
    asyncio.run(jarvis_mail_orchestrator())