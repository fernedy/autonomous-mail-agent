# [src/auto_learning.py]
"""
Módulo de auto-aprendizaje para JARVISMAIL.

Analiza correos enviados desde la Bandeja de Salida de Outlook via Microsoft Graph API,
extrae patrones de estilo y formato, y los incorpora al perfil de usuario
en LEARNED_PROFILE.md.

ARQUITECTURA:
  Usa GraphClient (Microsoft Graph API v1.0 oficial) para obtener correos
  enviados — SIN navegador, SIN Playwright, SIN DOM scraping.
  Todo via HTTP directo con tokens Bearer.

  Antes: OWAClient (OWA REST API v2.0 — deprecada 31 Mar 2024)
  Ahora: GraphClient (Microsoft Graph API v1.0 — soportada)
"""

import os
import json
import re
import time
import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger("auto_learning")


# ──────────────────────────────────────────────
#  Modelo de datos para los patrones extraídos
# ──────────────────────────────────────────────

@dataclass
class StylePatterns:
    greeting_patterns: list = field(default_factory=list)
    signoff_patterns: list = field(default_factory=list)
    common_phrases: list = field(default_factory=list)
    dominant_tone: str = ""
    avg_length_chars: float = 0.0
    uses_greeting: bool = True
    uses_signoff: bool = True
    sample_count: int = 0

    def to_markdown_block(self) -> str:
        lines = []
        lines.append(f"**Sample count:** {self.sample_count}")
        lines.append("")
        if self.greeting_patterns:
            lines.append("**Greeting patterns detected:**")
            for g in self.greeting_patterns:
                lines.append(f"- `{g}`")
            lines.append("")
        if self.signoff_patterns:
            lines.append("**Sign-off patterns detected:**")
            for s in self.signoff_patterns:
                lines.append(f"- `{s}`")
            lines.append("")
        if self.common_phrases:
            lines.append("**Common / repeated phrases:**")
            for p in self.common_phrases:
                lines.append(f"- \"{p}\"")
            lines.append("")
        lines.append(f"**Dominant tone:** {self.dominant_tone}")
        lines.append(f"**Average body length:** {self.avg_length_chars:,.0f} chars")
        return "\n".join(lines)


@dataclass
class BehavioralPatterns:
    """Patrones de comportamiento aprendidos del análisis de correos enviados.

    Estos patrones complementan SKILL.md con reglas de comportamiento reales
    observadas del usuario. Se almacenan en LEARNED_PROFILE.md bajo # BEHAVIORAL RULES.
    """
    decision_patterns: list = field(default_factory=list)
    approval_keywords: list = field(default_factory=list)
    delegation_patterns: list = field(default_factory=list)
    response_speed: str = ""  # inmediato / rápido / moderado / lento
    preferred_channels: list = field(default_factory=list)
    escalation_triggers: list = field(default_factory=list)

    def to_markdown_rules(self) -> str:
        """Genera reglas de comportamiento en formato Markdown para LEARNED_PROFILE.md.

        Sale como una sección # BEHAVIORAL RULES con subsecciones.
        """
        lines = []
        lines.append("# BEHAVIORAL RULES")
        lines.append("")
        lines.append("Reglas de comportamiento aprendidas del analisis de correos enviados.")
        lines.append("Complementan al perfil del usuario para mejorar las decisiones de triage.")
        lines.append("")

        if self.decision_patterns:
            lines.append("## Decision Patterns")
            for d in self.decision_patterns:
                lines.append(f"- {d}")
            lines.append("")

        if self.approval_keywords:
            lines.append("## Approval Keywords")
            lines.append("Palabras clave que indican aprobacion rapida:")
            for kw in self.approval_keywords:
                lines.append(f"- \"{kw}\"")
            lines.append("")

        if self.delegation_patterns:
            lines.append("## Delegation Patterns")
            for dp in self.delegation_patterns:
                lines.append(f"- {dp}")
            lines.append("")

        if self.response_speed:
            lines.append(f"**Estimated response speed:** {self.response_speed}")
            lines.append("")

        if self.preferred_channels:
            lines.append("**Preferred follow-up channels:**")
            for ch in self.preferred_channels:
                lines.append(f"- {ch}")
            lines.append("")

        if self.escalation_triggers:
            lines.append("## Escalation Triggers")
            lines.append("Situaciones que probablemente requieren atencion inmediata:")
            for et in self.escalation_triggers:
                lines.append(f"- {et}")
            lines.append("")

        return "\n".join(lines)


META_ANALYSIS_PROMPT = """Eres un analista de estilo de comunicación. Revisa los siguientes correos enviados por el usuario y extrae patrones de estilo.

## Correos Enviados (últimos {count})
{emails_text}

## Instrucciones
Identifica y lista:
1. **Greeting patterns** — Frases exactas de saludo (ej. "Buenos días", "Buenas tardes", "Cordial saludo")
2. **Sign-off patterns** — Frases exactas de despedida (ej. "Cordialmente y atento a sus comentarios", "Saludos cordiales")
3. **Common phrases** — Frases o estructuras que se repiten
4. **Dominant tone** — ¿Corporativo, urgente, técnico, informal? Una o dos palabras.
5. **Average body length** — Longitud aproximada en caracteres

Devuelve SOLO un JSON válido con esta estructura exacta, sin markdown fences ni texto adicional:
{{
  "greeting_patterns": ["..."],
  "signoff_patterns": ["..."],
  "common_phrases": ["..."],
  "dominant_tone": "...",
  "avg_length_chars": 0.0
}}"""


BEHAVIORAL_PROMPT = """Eres un analista de comportamiento de comunicacion. Revisa los siguientes correos enviados por el usuario y extrae patrones de comportamiento.

## Correos Enviados (ultimos {count})
{emails_text}

## Instrucciones
Analiza el comportamiento del usuario en estos correos y extrae:

1. **Decision patterns** — Como toma decisiones? Aprueba rapido? Pide revision? Delega? (lista de frases descriptivas)
2. **Approval keywords** — Palabras o frases exactas que indican aprobacion (ej. "Aprobado", "Visto bueno", "Proceda")
3. **Delegation patterns** — Como delega tareas? A quien? Con que instrucciones?
4. **Response speed** — Estimado: inmediato / rapido / moderado / lento
5. **Preferred channels** — Usa Teams, correo directo, o reenvia a otros? (lista)
6. **Escalation triggers** — Que situaciones hacen que el usuario escale o ponga urgencia?

Devuelve SOLO un JSON valido con esta estructura exacta, sin markdown fences ni texto adicional:
{{
  "decision_patterns": ["..."],
  "approval_keywords": ["..."],
  "delegation_patterns": ["..."],
  "response_speed": "...",
  "preferred_channels": ["..."],
  "escalation_triggers": ["..."]
}}"""


class AutoLearner:
    """Analiza correos enviados y extrae patrones de estilo."""

    def __init__(
        self,
        vision=None,
        router=None,
        profile_path: str = "/app/config/LEARNED_PROFILE.md",
        graph_client=None,
        owa_client=None,  # Mantenido por compatibilidad legacy
    ):
        """
        Args:
            vision: PWAManager (opcional, no usado desde migración a Graph API).
            router: LLMRouterGateway para análisis LLM (detección de intención + selección de modelo).
            profile_path: Ruta a LEARNED_PROFILE.md.
            graph_client: GraphClient para obtener correos via Microsoft Graph API.
            owa_client: DEPRECATED, mantener solo graph_client.
        """
        self.vision = vision
        self.router = router
        self.profile_path = profile_path
        self.graph = graph_client or owa_client  # Preferir graph_client

    # ──────────────────────────────────────────
    #  1. Obtener correos enviados vía OWA API
    # ──────────────────────────────────────────

    async def _extract_sent_via_graph(self, limit: int = 5) -> list:
        """Obtiene correos enviados usando Microsoft Graph API.

        Returns:
            Lista de dicts con {conv_id, to, subject, body, timestamp}.
        """
        if not self.graph:
            _json_log("action", "No Graph client available", {}, "WARNING")
            return []

        try:
            # Obtener metadatos de últimos correos enviados
            sent = await self.graph.get_sent_messages(limit=limit)
            if not sent:
                _json_log("action", "No sent emails via Graph API", {}, "WARNING")
                return []

            _json_log("action", f"Got {len(sent)} sent emails via Graph API", {
                "subjects": [e.get("subject", "")[:40] for e in sent]
            })

            emails = []
            for msg in sent:
                # Obtener cuerpo completo del mensaje
                body_content = await self.graph.get_message_content(msg["id"])
                body = ""
                if body_content:
                    body = (body_content.get("body_text", "")
                            or body_content.get("body_html", "")
                            or body_content.get("preview", ""))
                    # Strip HTML antes de analizar — evita CSS/etiquetas como "common phrases"
                    if body and ("<" in body and ">" in body):
                        body = re.sub(r'<[^>]+>', ' ', body)
                        body = re.sub(r'&[a-zA-Z]+;', ' ', body)
                        body = re.sub(r'\s+', ' ', body).strip()

                emails.append({
                    "conv_id": msg.get("id", ""),
                    "to": msg.get("to_recipients", ""),
                    "subject": msg.get("subject", "Sin asunto"),
                    "body": body[:5000],
                    "timestamp": msg.get("sent_at", ""),
                })

            bodies_ok = sum(1 for e in emails if e.get("body") and len(e["body"]) > 50)
            _json_log("action", f"Extracted {len(emails)} sent emails via Graph API", {
                "total": len(emails),
                "with_body": bodies_ok,
                "subjects": [e.get("subject", "")[:40] for e in emails]
            })
            return emails

        except Exception as e:
            _json_log("action", f"Graph API sent extraction failed: {e}", {
                "error": str(e)[:100]
            }, "ERROR")
            return []

    # ──────────────────────────────────────────
    #  2. Análisis de estilo
    # ──────────────────────────────────────────

    async def _extract_style(self, emails: list) -> StylePatterns:
        if not emails:
            return StylePatterns(sample_count=0)

        if self.router is not None:
            try:
                return await self._llm_style_analysis(emails)
            except Exception as e:
                logger.warning(f"LLM analysis failed: {e}")

        return self._heuristic_style_analysis(emails)

    async def _llm_style_analysis(self, emails: list) -> StylePatterns:
        emails_text = self._format_emails_for_prompt(emails)
        prompt = META_ANALYSIS_PROMPT.format(
            count=len(emails),
            emails_text=emails_text
        )
        raw = await self.router.analyze_profile(prompt)

        if not raw:
            raise ValueError("LLM returned empty response")

        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON found in LLM response")

        data = json.loads(json_match.group(0))
        return StylePatterns(
            greeting_patterns=data.get("greeting_patterns", []),
            signoff_patterns=data.get("signoff_patterns", []),
            common_phrases=data.get("common_phrases", []),
            dominant_tone=data.get("dominant_tone", "no determinado"),
            avg_length_chars=float(data.get("avg_length_chars", 0)),
            sample_count=len(emails),
        )

    def _heuristic_style_analysis(self, emails: list) -> StylePatterns:
        greeting_patterns = set()
        signoff_patterns = set()
        common_phrases = []
        all_text = []
        total_length = 0

        for email in emails:
            body = email.get("body", "")
            subject = email.get("subject", "")

            if not body:
                body = subject if subject else ""

            total_length += len(body)

            greeting_match = re.search(
                r'^(Buen(?:os)?\s+d[ií]as|Buenas\s+tardes|Cordial\s+saludo|Hola|Estimad[ao])',
                body.lstrip(), re.IGNORECASE
            )
            if greeting_match:
                greeting_patterns.add(greeting_match.group(1))

            signoff_match = re.search(
                r'(Cordialmente\s+y\s+atento\s+a\s+sus\s+comentarios|Saludos\s+cordiales|Atentamente|Quedo\s+atento|Quedamos\s+atentos)',
                body, re.IGNORECASE
            )
            if signoff_match:
                signoff_patterns.add(signoff_match.group(1))

            all_text.append(body)

        avg_length = total_length / len(emails) if emails else 0
        combined = " ".join(all_text).lower()

        words = combined.split()
        bigrams = Counter(zip(words, words[1:]))
        min_bigram_occurrences = max(2, len(emails) // 2)
        common_bigrams = [
            f"{' '.join(bg)}" for bg, count in bigrams.most_common(5)
            if count >= min_bigram_occurrences and len(bg[0]) > 3
        ]

        tone = "corporativo"
        if any(w in combined for w in ["urgente", "inmediato", "prioridad"]):
            tone = "urgente"
        elif any(w in combined for w in ["error", "falla", "incidente"]):
            tone = "técnico"
        elif any(w in combined for w in ["gracias", "por favor", "cordialmente"]):
            tone = "cortés"

        return StylePatterns(
            greeting_patterns=sorted(greeting_patterns),
            signoff_patterns=sorted(signoff_patterns),
            common_phrases=common_bigrams,
            dominant_tone=tone,
            avg_length_chars=round(avg_length, 1),
            sample_count=len(emails),
        )

    def _format_emails_for_prompt(self, emails: list) -> str:
        blocks = []
        for i, email in enumerate(emails, 1):
            body = email.get("body", "")
            subject = email.get("subject", "?")
            if not body:
                body = f"(cuerpo no disponible, asunto: {subject})"
            blocks.append(
                f"--- Correo {i} ---\n"
                f"To: {email.get('to', '?')}\n"
                f"Subject: {subject}\n"
                f"Body:\n{body[:2000]}"
            )
        return "\n\n".join(blocks)

    # ──────────────────────────────────────────
    #  3. Persistencia en LEARNED_PROFILE.md
    # ──────────────────────────────────────────

    async def _append_to_profile(self, patterns: StylePatterns) -> bool:
        if patterns.sample_count == 0:
            _json_log("action", "No patterns to write", {"reason": "zero_samples"})
            return False

        try:
            session_tag = f"## Session {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            markdown_block = patterns.to_markdown_block()
            auto_learning_section = (
                "\n\n"
                "# AUTO LEARNING\n"
                f"{session_tag}\n\n"
                f"{markdown_block}\n"
            )

            current = ""
            if os.path.exists(self.profile_path):
                with open(self.profile_path, "r", encoding="utf-8") as f:
                    current = f.read()

            if "# AUTO LEARNING" in current:
                header_end = current.index("# AUTO LEARNING") + len("# AUTO LEARNING")
                rest = current[header_end:]
                first_nl = rest.index("\n") if "\n" in rest else len(rest)
                insert_point = header_end + first_nl + 1
                updated = (
                    current[:insert_point]
                    + "\n" + session_tag + "\n\n" + markdown_block + "\n"
                    + current[insert_point:]
                )
            else:
                updated = current.rstrip() + auto_learning_section

            tmp_path = self.profile_path + ".tmp"
            dir_path = os.path.dirname(self.profile_path)
            os.makedirs(dir_path, exist_ok=True)

            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(updated)
                f.flush()
                os.fsync(f.fileno())

            os.replace(tmp_path, self.profile_path)

            if os.path.exists(self.profile_path):
                with open(self.profile_path, "r", encoding="utf-8") as f:
                    verify = f.read()
                if "# AUTO LEARNING" in verify and session_tag in verify:
                    _json_log("action", "Profile updated successfully", {
                        "session": session_tag,
                        "patterns": len(patterns.greeting_patterns) + len(patterns.signoff_patterns)
                    })

            return True

        except Exception as e:
            logger.error(f"Error escribiendo al perfil: {e}")
            return False

    # ──────────────────────────────────────────
    #  4. Análisis de comportamiento (behavioral rules)
    # ──────────────────────────────────────────

    async def _extract_behavior(self, emails: list) -> BehavioralPatterns:
        """Extrae patrones de comportamiento de los correos enviados.

        Usa LLM si router disponible, sino heurística regex.
        """
        if not emails:
            return BehavioralPatterns()

        if self.router is not None:
            try:
                return await self._llm_behavior_analysis(emails)
            except Exception as e:
                logger.warning(f"LLM behavioral analysis failed: {e}")

        return self._heuristic_behavior_analysis(emails)

    async def _llm_behavior_analysis(self, emails: list) -> BehavioralPatterns:
        emails_text = self._format_emails_for_prompt(emails)
        prompt = BEHAVIORAL_PROMPT.format(
            count=len(emails),
            emails_text=emails_text
        )
        raw = await self.router.analyze_profile(prompt)

        if not raw:
            raise ValueError("LLM returned empty response")

        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            raise ValueError("No JSON found in LLM behavioral response")

        data = json.loads(json_match.group(0))
        return BehavioralPatterns(
            decision_patterns=data.get("decision_patterns", []),
            approval_keywords=data.get("approval_keywords", []),
            delegation_patterns=data.get("delegation_patterns", []),
            response_speed=data.get("response_speed", "moderado"),
            preferred_channels=data.get("preferred_channels", []),
            escalation_triggers=data.get("escalation_triggers", []),
        )

    def _heuristic_behavior_analysis(self, emails: list) -> BehavioralPatterns:
        """Análisis heurístico de comportamiento sin LLM."""
        combined_body = " ".join([
            e.get("body", "").lower()
            for e in emails if e.get("body")
        ])
        combined_subject = " ".join([
            e.get("subject", "").lower()
            for e in emails if e.get("subject")
        ])
        combined = combined_body + " " + combined_subject

        # Approval keywords
        approval_kws = []
        for kw in ["aprobado", "aprobada", "visto bueno", "proceda", "ok",
                    "autorizado", "confirmado", "validado"]:
            if kw in combined:
                approval_kws.append(kw)

        # Decision patterns
        decision_patterns = []
        if any(w in combined for w in ["aprob", "ok", "proceda"]):
            decision_patterns.append("Aprueba rapidamente cuando hay certeza")
        if any(w in combined for w in ["coordinar", "sincronizar", "revisemos"]):
            decision_patterns.append("Coordina con otros antes de decidir")
        if any(w in combined for w in ["por favor", "favor", "solicito"]):
            decision_patterns.append("Delega tareas especificas con instrucciones claras")

        # Delegation patterns
        delegation_patterns = []
        if any(w in combined for w in ["favor", "por favor", "coordinar"]):
            delegation_patterns.append("Asigna responsables directos para cada tarea")
        if "validar" in combined:
            delegation_patterns.append("Pide validacion antes de ejecutar")

        # Response speed
        has_urgency = any(w in combined for w in ["urgente", "inmediato", "prioridad", "hoy"])
        has_formal = any(w in combined for w in ["cordialmente", "atento", "saludos"])
        if has_urgency:
            response_speed = "rapido"
        elif has_formal:
            response_speed = "moderado"
        else:
            response_speed = "rapido"

        # Preferred channels
        channels = []
        if "teams" in combined:
            channels.append("Microsoft Teams")
        if "reunion" in combined:
            channels.append("Reuniones programadas")
        channels.append("Correo electronico")

        # Escalation triggers
        escalation = []
        if "error" in combined:
            escalation.append("Errores o fallas tecnicas")
        if "urgente" in combined:
            escalation.append("Situaciones marcadas como urgentes")
        if "incidente" in combined:
            escalation.append("Incidentes reportados")

        return BehavioralPatterns(
            decision_patterns=decision_patterns,
            approval_keywords=approval_kws,
            delegation_patterns=delegation_patterns,
            response_speed=response_speed,
            preferred_channels=channels,
            escalation_triggers=escalation,
        )

    # ──────────────────────────────────────────
    #  5. Persistencia de behavioral rules
    # ──────────────────────────────────────────

    async def _append_behavioral_rules(self, patterns: BehavioralPatterns) -> bool:
        """Guarda las behavioral rules en LEARNED_PROFILE.md bajo # BEHAVIORAL RULES.

        Reemplaza la seccion completa cada vez (no acumula como AUTO LEARNING).
        """
        if not patterns.decision_patterns and not patterns.approval_keywords:
            _json_log("action", "No behavioral patterns to write", {"reason": "no_patterns"})
            return False

        try:
            rules_block = patterns.to_markdown_rules()

            current = ""
            if os.path.exists(self.profile_path):
                with open(self.profile_path, "r", encoding="utf-8") as f:
                    current = f.read()

            if "# BEHAVIORAL RULES" in current:
                # Reemplazar seccion existente entre # BEHAVIORAL RULES y el siguiente # o EOF
                before = current[:current.index("# BEHAVIORAL RULES")]
                after_marker = current[current.index("# BEHAVIORAL RULES"):]
                next_header = re.search(r'\n# ', after_marker[1:])
                if next_header:
                    after = after_marker[next_header.start() + 1:]
                else:
                    after = ""
                updated = before.rstrip() + "\n\n" + rules_block + "\n\n" + after.lstrip()
            else:
                # Insertar despues del perfil de usuario, antes de # AUTO LEARNING
                if "# AUTO LEARNING" in current:
                    insert_point = current.index("# AUTO LEARNING")
                    updated = current[:insert_point].rstrip() + "\n\n" + rules_block + "\n\n" + current[insert_point:]
                else:
                    updated = current.rstrip() + "\n\n" + rules_block + "\n"

            tmp_path = self.profile_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(updated)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.profile_path)

            _json_log("action", "Behavioral rules updated", {
                "decision_patterns": len(patterns.decision_patterns),
                "approval_keywords": len(patterns.approval_keywords),
                "escalation_triggers": len(patterns.escalation_triggers),
            })
            return True

        except Exception as e:
            logger.error(f"Error escribiendo behavioral rules: {e}")
            return False

    # ──────────────────────────────────────────
    #  6. Orquestador principal
    # ──────────────────────────────────────────

    async def run(self, limit: int = 5) -> tuple:
        """Ciclo completo de auto-aprendizaje vía Microsoft Graph API.

        SIN navegación. SIN DOM scraping. SIN Playwright.
        Todo via GraphClient (Microsoft Graph API v1.0 oficial).

        Args:
            limit: Cantidad de correos a analizar.

        Returns:
            (success, patterns, behavioral_counts, analysis_mode) donde:
            - patterns: StylePatterns con los patrones extraídos
            - behavioral_counts: dict con cantidades de behavioral rules
            - analysis_mode: "LLM" si usó router, "heurística" si fallback
        """
        _json_log("action", "Auto-learning started", {"limit": limit, "has_graph": self.graph is not None})

        # E1: Obtener correos vía Graph API
        emails = await self._extract_sent_via_graph(limit=limit)

        if not emails:
            _json_log("action", "No sent emails found", {}, "WARNING")
            return (False, None, {}, "")

        # E2: Análisis de estilo — detectar modo
        used_llm = False
        try:
            if self.router is not None:
                patterns = await self._llm_style_analysis(emails)
                used_llm = True
            else:
                patterns = self._heuristic_style_analysis(emails)
        except Exception:
            patterns = self._heuristic_style_analysis(emails)

        analysis_mode = "🤖 IA" if used_llm else "📊 Heurística"
        _json_log("action", "Style analysis completed", {
            "greetings": len(patterns.greeting_patterns),
            "signoffs": len(patterns.signoff_patterns),
            "tone": patterns.dominant_tone,
            "mode": analysis_mode,
        })

        # E3: Análisis de comportamiento (behavioral rules)
        behavioral = await self._extract_behavior(emails)
        _json_log("action", "Behavioral analysis completed", {
            "decision_patterns": len(behavioral.decision_patterns),
            "approval_keywords": len(behavioral.approval_keywords),
        })

        # Persistir estilo
        written = await self._append_to_profile(patterns)
        if not written:
            _json_log("action", "Failed to write style patterns", {}, "ERROR")

        # Persistir behavioral rules
        rules_written = await self._append_behavioral_rules(behavioral)

        # Construir resumen de behavioral rules para el overview
        behavioral_counts = {
            "decision_patterns": len(behavioral.decision_patterns),
            "approval_keywords": len(behavioral.approval_keywords),
            "delegation_patterns": len(behavioral.delegation_patterns),
            "escalation_triggers": len(behavioral.escalation_triggers),
            "response_speed": behavioral.response_speed,
        }

        _json_log("action", "Auto-learning completed", {
            "emails": len(emails),
            "greetings": len(patterns.greeting_patterns),
            "signoffs": len(patterns.signoff_patterns),
            "tone": patterns.dominant_tone,
            "behavioral_rules": rules_written,
            "approval_keywords": len(behavioral.approval_keywords),
            "mode": analysis_mode,
        })
        return (True, patterns, behavioral_counts, analysis_mode)


def _json_log(event_type, message, data=None, level="INFO"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": "auto_learning",
        "event_type": event_type,
        "message": message,
        "data": data or {},
    }, ensure_ascii=False))


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    from vision.pwa_manager import PWAManager
    from vision.owa_client import OWAClient

    vision = PWAManager()
    await vision.start_session()
    await vision.ensure_logged_in()

    owa = OWAClient(vision)
    learner = AutoLearner(vision=vision, owa_client=owa)
    success, patterns = await learner.run(limit=5)

    if success:
        print("✅ Auto-learning completado exitosamente.")
        if patterns:
            print(f"   - Saludos: {patterns.greeting_patterns}")
            print(f"   - Despedidas: {patterns.signoff_patterns}")
            print(f"   - Tono: {patterns.dominant_tone}")
    else:
        print("❌ Auto-learning falló.")


if __name__ == "__main__":
    asyncio.run(main())
