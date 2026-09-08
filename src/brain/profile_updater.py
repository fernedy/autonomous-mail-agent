import os
import re
import json
import time
import asyncio
import logging
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from observability.metrics import OperationalMetrics

logger = logging.getLogger("brain.profile")
_metrics = OperationalMetrics()

def _json_log(event_type, message, data=None, level="INFO"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": "brain",
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }, ensure_ascii=False))

# ── Prompt simplificado: extrae patrones NUEVOS en vez de reescribir todo ──
AUTO_LEARNING_PROMPT = """Eres un analista de estilo de comunicacion. Revisa los siguientes correos enviados por el usuario y extrae SOLO los patrones NUEVOS o CAMBIOS que detectes.

## Correos Recientes Enviados (Ultimos {count})
{sent_emails}

## Instrucciones
Identifica y lista:
1. **Greeting patterns** — Frases exactas de saludo (ej. "Buenos días", "Buenas tardes", "Cordial saludo")
2. **Sign-off patterns** — Frases exactas de despedida (ej. "Cordialmente y atento a sus comentarios", "Saludos cordiales")
3. **Common phrases** — Frases o estructuras que se repiten
4. **Dominant tone** — ¿Corporativo, urgente, técnico, informal? Una o dos palabras.
5. **Average body length** — Longitud aproximada en caracteres

Devuelve SOLO un JSON valido con esta estructura exacta, sin markdown fences ni texto adicional:
{{
  "greeting_patterns": ["..."],
  "signoff_patterns": ["..."],
  "common_phrases": ["..."],
  "dominant_tone": "...",
  "avg_length_chars": 0.0
}}"""

PROFILE_PATH = os.getenv("LEARNED_PROFILE_PATH", "/app/config/LEARNED_PROFILE.md")
TMP_PROFILE_PATH = PROFILE_PATH + ".brain.tmp"


@dataclass
class StylePatterns:
    """Patrones de estilo detectados en un lote de correos enviados."""
    greeting_patterns: list = field(default_factory=list)
    signoff_patterns: list = field(default_factory=list)
    common_phrases: list = field(default_factory=list)
    dominant_tone: str = ""
    avg_length_chars: float = 0.0
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


class ProfileUpdater:
    def __init__(self, broker, router, learning_paused: asyncio.Event = None):
        self.broker = broker
        self.router = router
        self.collected_emails: list = []
        self.batch_size = 5
        self.learning_paused = learning_paused or asyncio.Event()
        self._ensure_profile()
        _json_log("tactical", "ProfileUpdater initialized", {
            "action": "profile_updater_init",
            "batch_size": self.batch_size,
            "profile_path": PROFILE_PATH
        })

    def _ensure_profile(self) -> None:
        if not os.path.exists(PROFILE_PATH):
            dir_path = os.path.dirname(PROFILE_PATH)
            os.makedirs(dir_path, exist_ok=True)
            with open(PROFILE_PATH, "w", encoding="utf-8") as f:
                f.write("# User Behavior Profile\n\n")
            _json_log("tactical", "Profile file created from scratch", {
                "action": "profile_created",
                "profile_path": PROFILE_PATH
            })
            _json_log("clevel", "Profile created", {
                "metric": "profile_created",
                "value": 1,
                "details": {"profile_path": PROFILE_PATH}
            })

    async def _collect_one(self) -> bool:
        try:
            raw = await self.broker.client.rpop("queue:sent_emails")
            if raw is None:
                return False
            email = json.loads(raw)
            self.collected_emails.append(email)
            _json_log("tactical", "Sent email collected from Redis (RPOP)", {
                "action": "email_collected",
                "buffer_length": len(self.collected_emails),
                "batch_size": self.batch_size,
                "subject": email.get("subject", ""),
                "to": email.get("to", "")
            })
            return True
        except Exception as e:
            _json_log("tactical", "Failed to collect sent email", {
                "action": "collect_error",
                "error": str(e)
            }, "ERROR")
            return False

    async def _collect_loop(self, interval: float = 3.0) -> None:
        _json_log("tactical", "Collect loop started", {
            "action": "collect_loop_start",
            "interval_seconds": interval
        })
        while True:
            try:
                await self._collect_one()
            except Exception as e:
                _json_log("tactical", "Unhandled exception in collect loop", {
                    "action": "collect_loop_crash",
                    "traceback": traceback.format_exc()
                }, "ERROR")
            await asyncio.sleep(interval)

    async def _analyze_loop(self, interval: float = 120.0) -> None:
        _json_log("tactical", "Analyze loop started", {
            "action": "analyze_loop_start",
            "interval_seconds": interval
        })
        while True:
            try:
                await asyncio.sleep(interval)
                if self.collected_emails:
                    await self._run_analysis()
            except Exception as e:
                _json_log("tactical", "Unhandled exception in analyze loop", {
                    "action": "analyze_loop_crash",
                    "traceback": traceback.format_exc()
                }, "ERROR")

    async def _run_analysis(self) -> bool:
        if len(self.collected_emails) < self.batch_size:
            _json_log("tactical", "Insufficient emails for analysis", {
                "action": "analysis_skip",
                "buffer_length": len(self.collected_emails),
                "required": self.batch_size
            })
            return False

        batch = self.collected_emails[:self.batch_size]

        try:
            # ── PAUSAR TRIAGE (dentro del try para garantizar finally) ──
            self.learning_paused.set()
            _json_log("tactical", "Triage paused for profile analysis", {
                "action": "profile_analysis_pause_triage"
            })

            # Usar el mismo formato que AutoLearner (extraer patrones simples)
            emails_text = self._format_emails_for_llm(batch)
            prompt = AUTO_LEARNING_PROMPT.format(
                count=len(batch),
                sent_emails=emails_text
            )

            _json_log("tactical", "Starting profile pattern extraction", {
                "action": "analysis_start",
                "batch_size": len(batch)
            })

            raw = await self.router.analyze_profile(prompt)

            if not raw or raw.startswith("ERROR") or len(raw) < 50:
                _json_log("tactical", "LLM returned invalid output for pattern extraction", {
                    "action": "analysis_invalid_output",
                    "output_length": len(raw or ""),
                    "output_preview": (raw or "")[:200]
                }, "ERROR")
                return False

            # Parsear JSON del LLM
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not json_match:
                _json_log("tactical", "No JSON found in LLM response for pattern extraction", {
                    "raw_preview": raw[:500]
                }, "ERROR")
                return False

            data = json.loads(json_match.group(0))
            patterns = StylePatterns(
                greeting_patterns=data.get("greeting_patterns", []),
                signoff_patterns=data.get("signoff_patterns", []),
                common_phrases=data.get("common_phrases", []),
                dominant_tone=data.get("dominant_tone", "no determinado"),
                avg_length_chars=float(data.get("avg_length_chars", 0)),
                sample_count=len(batch),
            )

            # Append con #AUTO LEARNING tag
            written = self._append_to_profile(patterns)
            if not written:
                _json_log("tactical", "Failed to append patterns to profile", {
                    "action": "analysis_write_failure"
                }, "ERROR")
                return False

            self.collected_emails = self.collected_emails[self.batch_size:]

            _json_log("tactical", "Profile pattern extraction and append completed", {
                "action": "analysis_complete",
                "remaining_buffer": len(self.collected_emails),
                "greetings_found": len(patterns.greeting_patterns),
                "signoffs_found": len(patterns.signoff_patterns),
                "tone": patterns.dominant_tone
            })

            _metrics.profile_update_success(
                rules_added=len(patterns.greeting_patterns) + len(patterns.signoff_patterns),
                tone_shifts=[]
            )
            return True

        finally:
            # ── REANUDAR TRIAGE (SIEMPRE, incluso si falló) ──
            self.learning_paused.clear()
            _json_log("tactical", "Triage resumed after profile analysis", {
                "action": "profile_analysis_resume_triage"
            })

    def _format_emails_for_llm(self, emails: list) -> str:
        """Formatea correos para enviar al LLM."""
        blocks = []
        for i, e in enumerate(emails, 1):
            to = e.get("to", "unknown")
            subject = e.get("subject", "No subject")
            body = e.get("body", "")[:2000]
            ts = e.get("timestamp", "")
            blocks.append(
                "--- Email {} (To: {}, Subject: {}, Time: {}) ---\n{}".format(
                    i, to, subject, ts, body
                )
            )
        return "\n\n".join(blocks)

    def _append_to_profile(self, patterns: StylePatterns) -> bool:
        """Añade patrones aprendidos a LEARNED_PROFILE.md bajo # AUTO LEARNING.

        Sigue el MISMO formato que AutoLearner para compatibilidad total.
        """
        if patterns.sample_count == 0:
            _json_log("action", "No patterns to write, skipping", {
                "reason": "zero_samples"
            })
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

            # Leer contenido actual
            current = ""
            if os.path.exists(PROFILE_PATH):
                with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                    current = f.read()

            # Si ya existe # AUTO LEARNING, insertar después de esa línea
            # Sino, añadir al final
            if "# AUTO LEARNING" in current:
                header_end = current.index("# AUTO LEARNING") + len("# AUTO LEARNING")
                rest = current[header_end:]
                first_nl = rest.index("\n") if "\n" in rest else len(rest)
                insert_point = header_end + first_nl + 1
                updated = (
                    current[:insert_point]
                    + "\n"
                    + session_tag
                    + "\n\n"
                    + markdown_block
                    + "\n"
                    + current[insert_point:]
                )
            else:
                updated = current.rstrip() + auto_learning_section

            # Atomic write
            dir_path = os.path.dirname(PROFILE_PATH)
            os.makedirs(dir_path, exist_ok=True)
            with open(TMP_PROFILE_PATH, "w", encoding="utf-8") as f:
                f.write(updated)
                f.flush()
                os.fsync(f.fileno())
            os.replace(TMP_PROFILE_PATH, PROFILE_PATH)

            # Verificación
            if os.path.exists(PROFILE_PATH):
                with open(PROFILE_PATH, "r", encoding="utf-8") as f:
                    verify_content = f.read()
                if "# AUTO LEARNING" in verify_content and session_tag in verify_content:
                    _json_log("action", "Style patterns successfully appended to profile", {
                        "section": "# AUTO LEARNING",
                        "session": session_tag,
                        "patterns_count": len(patterns.greeting_patterns) + len(patterns.signoff_patterns),
                        "file_size": len(verify_content),
                        "verified": True
                    })
                else:
                    _json_log("action", "WARNING: File written but content not found in verification!", {
                        "section_present": "# AUTO LEARNING" in verify_content,
                        "session_present": session_tag in verify_content
                    }, "WARNING")

            return True

        except Exception as e:
            logger.error(f"Error appending to profile: {e}")
            return False
