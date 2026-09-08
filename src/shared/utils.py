# [src/shared/utils.py]
import os
import re


def load_secret(secret_name: str, default: str = None) -> str:
    """Lee un secreto desde Docker Secrets o cae a Variables de Entorno."""
    # Ruta estándar de Docker Secrets
    secret_path = f"/run/secrets/{secret_name.lower()}"

    if os.path.exists(secret_path):
        with open(secret_path, "r") as f:
            return f.read().strip()

    # Fallback al .env si no estamos en modo Swarm/Secrets
    return os.getenv(secret_name.upper(), default)


def extract_clean_reason(reasoning: str) -> str:
    """Extrae la razón limpia del reasoning estructurado del LLM.

    El LLM produce campos estructurados:
      Sender: X Validacion: Y Asunto: Z Urgencia: W...
      Decision: Noise Confianza: Alta Explicacion: ...

    ESTRATEGIA (3 niveles):
    E1 - Prioridad 1: Extrae el valor del campo 'Explicacion:' si existe y tiene
         contenido significativo (>15 chars). Este campo se añadió explícitamente
         al prompt para forzar al LLM a escribir texto libre.
    E2 - Prioridad 2: Busca texto DESPUÉS del ÚLTIMO campo conocido y antes del
         divisor '--- INICIO/FIN DEL CORREO ---'. Si hay texto >20 chars, es la
         explicación libre del LLM. Limpia prefijos de decisión/confianza.
    E3 - Fallback: Si no hay texto explicativo en ninguna de las formas anteriores,
         COMPONE una razón a partir de los campos extraídos más informativos
         (Asunto, Acción, Decisión, Temporal, Urgencia). Esto es mejor que devolver
         un mensaje genérico porque provee contexto real al operador.

    Returns:
        Texto limpio <=250 chars, sin campos estructurados visibles.
    """
    if not reasoning:
        return ""

    flat = reasoning.replace('\n', ' ').replace('\r', ' ')
    flat = re.sub(r'\s+', ' ', flat).strip()

    field_names = r'(?:Sender|Validacion|Asunto|Urgencia|Temporal|Acci[óo]n|Jerarqu[íi]a|Hilo|Decision|Confianza|Explicacion)'
    fields_re = re.compile(rf'\b{field_names}\s*:', re.IGNORECASE)
    all_matches = list(fields_re.finditer(flat))

    if not all_matches:
        if len(flat) > 250:
            return flat[:250] + "..."
        return flat

    # Helper: limpiar prefijos de decisión/confianza
    def _strip_decision_prefix(text: str) -> str:
        if not text:
            return text
        t = text.strip()
        decision_words = [
            'noise', 'action', 'executive_decision',
            'alta', 'media', 'baja',
        ]
        for word in decision_words:
            m = re.match(rf'\b{re.escape(word)}\s*[\.:?!]?\s+', t, re.IGNORECASE)
            if m:
                t = t[m.end():].strip()
                break
        return t

    # Extraer valor de un campo por nombre (helper)
    def _get_field_value(field_lower: str) -> str:
        """Devuelve el texto entre el campo matching y el siguiente campo."""
        for i, m in enumerate(all_matches):
            cur_field = flat[m.start():m.end()].lower().rstrip(':').strip()
            if cur_field == field_lower:
                val_start = m.end()
                val_end = all_matches[i+1].start() if i + 1 < len(all_matches) else len(flat)
                val = flat[val_start:val_end].strip().rstrip(',').strip()
                # Quitar delimitadores
                val = re.sub(r'^-{3,}.*', '', val).strip()
                return val
        return ""

    # ─── E1: Intentar extraer del campo 'Explicacion:' (nuevo campo forzado en prompt) ───
    explicacion_val = _get_field_value('explicacion')
    if explicacion_val and len(explicacion_val) > 15:
        return explicacion_val[:250].strip()

    # ─── E2: Texto después del último campo conocido ───
    last_m = all_matches[-1]
    after = flat[last_m.end():].strip()
    div_match = re.search(r'-{3,}\s+(?:INICIO|FIN|INICIO\s+DEL|FIN\s+DEL)\s+CORREO\s*-{3,}', after, re.IGNORECASE)
    if div_match:
        after = after[:div_match.start()].strip()

    if len(after) > 20:
        return _strip_decision_prefix(after)[:250].strip()

    # ─── E3: Fallback — COMPONER desde campos extraídos ───
    # Extraer los campos más informativos y armar una razón compuesta.
    # Esto evita el genérico "Sin razonamiento explicativo disponible." y
    # provee contexto útil aunque el LLM no haya escrito texto libre.
    composed_parts = []
    priority_fields = ['asunto', 'accion', 'acción', 'decision', 'temporal', 'urgencia', 'sender']
    for field_name in priority_fields:
        val = _get_field_value(field_name)
        if val and len(val) > 3:
            # Limpiar el valor
            val_clean = re.sub(r'\s+', ' ', val)[:80].strip()
            if val_clean:
                composed_parts.append(f"{field_name.capitalize()}: {val_clean}")

    if composed_parts:
        result = " | ".join(composed_parts)
        if len(result) > 250:
            result = result[:247] + "..."
        return result

    # Fallback final (no debería llegar aquí si hay campos)
    return "Sin razonamiento explicativo disponible."
