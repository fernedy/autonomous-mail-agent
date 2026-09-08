"""
SenderExtractor — Validación de nombres de remitente.

Usado por brain_worker.py guardrails N2 y N8 para determinar si un
sender es un nombre de persona REAL o un label de sistema/ticket.
"""

import re
from typing import Optional

# Palabras genéricas en español que NUNCA deben tratarse como nombre de persona
# aunque empiecen con mayúscula
_SPANISH_FALSE_NAMES: set[str] = {
    "validación", "validacion", "reporte", "informe",
    "solicitud", "notificación", "notificacion",
    "sistema", "proyecto", "servicio", "gestión", "gestion",
    "aplicación", "aplicacion", "actualización", "actualizacion",
    "configuración", "configuracion", "implementación", "implementacion",
    "migración", "migracion", "integración", "integracion",
    "despliegue", "mantenimiento", "revisión", "revision",
    "monitoreo", "avance", "estado", "respuesta",
    "soporte", "incidente", "problema", "caso",
    "seguimiento", "proyectos",
}


class SenderExtractor:
    """Valida y extrae nombres de remitente."""

    @staticmethod
    def is_valid(sender: Optional[str], subject: Optional[str] = None) -> bool:
        """Determina si un sender es un nombre de persona REAL o un label de sistema.

        Estrategia:
        1. Si sender está vacío → inválido
        2. Si sender contiene '@' → es email, no nombre → inválido
        3. Si sender contiene números al inicio (ej: "IM26-390076") → ticket → inválido
        4. Si sender contiene palabras de _SPANISH_FALSE_NAMES → label de sistema → inválido
        5. Si sender es una sola palabra corta (<5 chars) → probablemente no es nombre completo
        6. Si sender contiene fechas → inválido
        7. Si sender tiene >100 chars → es firma/signature → inválido
        8. Si tiene palabras de disclaimer legal → inválido
        9. Si contiene punto y coma con múltiples nombres >30 chars → inválido
        10. Si todo lo anterior pasa → probablemente es nombre real → válido

        Args:
            sender: Nombre del remitente a validar.
            subject: Asunto del correo (opcional, para cross-validation).

        Returns:
            True si parece un nombre de persona real, False si es label de sistema.
        """
        if not sender:
            return False

        cleaned = sender.strip()
        if not cleaned:
            return False

        sender_lower = cleaned.lower()

        # Check 1: Email address
        if '@' in cleaned:
            return False

        # Check 2: System ticket pattern (números, guiones al inicio)
        # Ej: "IM26-390076", "Caso 12345", "Ticket #123"
        if re.match(r'^[A-Z]{2,}\d+[-/]\d+', cleaned):
            return False

        # Check 3: False names (system labels que parecen nombres)
        first_word = cleaned.split()[0].lower().strip('.,;:()[]{}')
        if first_word in _SPANISH_FALSE_NAMES:
            return False

        # Check 4: Single short word (<5 chars) is not a full name
        words = cleaned.split()
        if len(words) == 1 and len(cleaned) < 5:
            return False

        # Check 5: Contains dates (digits + separators)
        if re.search(r'\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b', cleaned):
            return False

        # Check 6: Too long (>100 chars = signature/disclaimer)
        if len(cleaned) > 100:
            return False

        # Check 7: Disclaimer patterns
        disclaimer_patterns = [
            "si usted sospecha", "aviso de confidencial", "confidencialidad",
            "este mensaje es", "este correo es", "procedencia y/o contenido",
            "la informacion contenida", "propiedad del remitente",
        ]
        for pattern in disclaimer_patterns:
            if pattern in sender_lower:
                return False

        # Check 8: Multiple names separated by semicolon (>30 chars = list)
        if ';' in cleaned and len(cleaned) > 30:
            return False

        # Check 9: Greeting/signature patterns
        greeting_patterns = [
            r'^buenos\s+d[ií]as', r'^buenas\s+tardes', r'^buenas\s+noches',
            r'^cordial\s+(saludo|mente)', r'^atentamente', r'^saludos?\s+cordiales?',
            r'^hola\s+', r'^estimados?\s+', r'^gracias\s+por',
            r'^quedo\s+atento', r'^quedamos\s+atentos',
        ]
        for pattern in greeting_patterns:
            if re.match(pattern, sender_lower):
                return False

        # Cross-validation with subject: if sender equals subject, it's probably not a person
        if subject and cleaned.lower() == subject.strip().lower():
            return False

        # Passed all checks → looks like a real person name
        return True
