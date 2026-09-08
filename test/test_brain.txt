import os
from google import genai

def get_api_key():
    """Extrae la llave priorizando los secrets."""
    for path in ["/run/secrets/gemini_api_key", "/run/secrets/gemini_api_key.txt"]:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                keys = [k.strip() for k in f.read().split(",") if k.strip() and "YOUAPI" not in k]
                if keys: 
                    return keys[0]
    return os.getenv("GEMINI_API_KEY")

def run_diagnostics():
    print("="*50)
    print("🧠 DIAGNÓSTICO MANUAL DEL CEREBRO (GEMMA 4)")
    print("="*50)

    api_key = get_api_key()
    if not api_key:
        print("❌ ERROR FATAL: No se encontró la API Key en los secrets.")
        return

    print("✅ API Key extraída con éxito (Oculta por seguridad).")
    
    # 1. Conexión usando el SDK moderno google-genai
    try:
        client = genai.Client(api_key=api_key)
        print("✅ Cliente genai instanciado correctamente.")
    except Exception as e:
        print(f"❌ Error instanciando el cliente: {e}")
        return

    # 2. Prueba de Generación Directa con Gemma 4
    # Basado en tu documentación oficial: "gemma-4-31b-it"
    model_name = "gemma-4-31b-it"
    print(f"\n🚀 Enviando ping de prueba a: {model_name}...")
    
    try:
        response = client.models.generate_content(
            model=model_name,
            contents="Hola Gemma. Responde estrictamente con la palabra 'CONECTADO' y nada más."
        )
        print("\n🟢 RESPUESTA DEL MODELO:")
        print(f">>> {response.text}")
        print("\n✅ EL CEREBRO ESTÁ EN LÍNEA Y RESPONDIENDO.")
    except Exception as e:
        print(f"\n❌ ERROR DE API (404/400/etc):")
        print(e)
        print("\n⚠️ Esto significa que la API Key no tiene permisos para este modelo o el string del modelo es incorrecto.")

if __name__ == "__main__":
    run_diagnostics()