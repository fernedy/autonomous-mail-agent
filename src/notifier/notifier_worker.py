import os
import asyncio
import httpx
import logging
import json
import re
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from shared.redis_client import RedisTaskBroker
from shared.utils import load_secret, extract_clean_reason

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("notifier")

def _json_log(event_type, message, data=None, level="INFO"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": "notifier",
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }, ensure_ascii=False))

class NotifierWorker:
    def __init__(self):
        self.broker = RedisTaskBroker()
        self.token = load_secret("telegram_bot_token")
        self.chat_id = load_secret("telegram_chat_id")
        self.queue_in = "queue:notifications"
        self.queue_out = "queue:user_decisions"
        self.user_states = {}
        # v5.3l: fail_counts y brain_failure eliminados.
        # El LLM Router externo maneja failover interno.
        # Ya no se trackean fallos consecutivos ni se sugiere /setprovider.
        # v6.0: LLM Router Gateway embebido — integración tipo API OpenAI.
        # El catálogo de modelos se consulta al endpoint OpenAI-compatible /models.
        self.router_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        # Cache de modelos del gateway (se refresca cada 60s)
        self._cached_providers = {}
        self._cache_time = 0.0

    # ─────────────────────────────────────────────────────────
    #  Gateway: cache de modelos y health
    # ─────────────────────────────────────────────────────────

    async def _refresh_router_cache(self) -> None:
        """Refresca el cache de modelos desde el LLM Router Gateway.

        v6.0: El gateway es embebido con integración tipo API OpenAI.
        Se consulta el endpoint estándar /models del OPENAI_BASE_URL y se
        expone como el pseudo-provider 'gateway' para el flujo de Telegram.
        """
        now = time.time()
        if now - self._cache_time < 60:
            return
        try:
            headers = {}
            api_key = os.getenv("OPENAI_API_KEY", "")
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.router_url}/models", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    models = [
                        m.get("id", "") for m in data.get("data", []) if m.get("id")
                    ]
                    self._cached_providers = {"gateway": {"has_keys": True, "models": models}}
                    self._cache_time = now
                    _json_log("clevel", "Gateway model cache refreshed", {
                        "models": len(models),
                        "base_url": self.router_url
                    })
        except Exception as e:
            _json_log("clevel", "Failed to refresh gateway model cache", {
                "error": str(e)[:100]
            }, "WARNING")

    async def _get_router_health(self) -> dict:
        """Obtiene el estado del LLM Router Gateway desde Redis.

        El heartbeat del brain publica la configuración efectiva del gateway
        en hive:status:router_health (compatible con el panel de Grafana).

        Returns:
            Dict con health data del gateway, o dict vacío si falla.
        """
        try:
            raw = await self.broker.client.get("hive:status:router_health")
            if raw:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                return json.loads(raw)
        except Exception:
            pass
        return {}

    async def _build_provider_keyboard(self) -> InlineKeyboardMarkup:
        """Construye teclado de selección de proveedores desde el router.

        Incluye opción 'Auto (Router)' y los proveedores con keys activas.
        Cada proveedor se muestra con su estado de salud si está disponible.
        """
        await self._refresh_router_cache()

        keyboard = []
        # Opción Auto primero (el gateway elige modelo por intención)
        keyboard.append([InlineKeyboardButton("🔄 Auto (Gateway por intención)", callback_data="set_auto")])

        if self._cached_providers:
            for p_name in sorted(self._cached_providers.keys()):
                p_info = self._cached_providers[p_name]
                if p_info.get("has_keys") and p_info.get("models"):
                    n_models = len(p_info["models"])
                    display_name = p_name.replace("_", " ").title()
                    keyboard.append([
                        InlineKeyboardButton(f"{display_name} ({n_models} modelos)", callback_data=f"set_provider|{p_name}")
                    ])
        else:
            # Fallback si el gateway no responde
            keyboard.append([
                InlineKeyboardButton("Gateway (OPENAI_BASE_URL)", callback_data="set_provider|gateway")
            ])

        keyboard.append([InlineKeyboardButton("❌ Cancelar", callback_data="cancel_provider_select")])
        return InlineKeyboardMarkup(keyboard)

    # ─────────────────────────────────────────────────────────
    #  Model page (paginación de modelos)
    # ─────────────────────────────────────────────────────────

    async def _build_model_page(self, chat_id) -> tuple:
        """Construye el texto y teclado para la página actual de modelos.

        v5.3g: Los modelos se leen del cache del router, no de API keys locales.

        Returns:
            (texto_del_mensaje, InlineKeyboardMarkup)
        """
        state = self.user_states.get(chat_id)
        if not state or state.get("step") != "AWAITING_MODEL_INDEX":
            return "", InlineKeyboardMarkup([[]])

        provider = state["provider"]
        models = state["models"]
        page = state.get("page", 0)

        PER_PAGE = 25
        total_pages = max(1, (len(models) + PER_PAGE - 1) // PER_PAGE)
        start_idx = page * PER_PAGE
        end_idx = min(start_idx + PER_PAGE, len(models))
        page_models = models[start_idx:end_idx]

        TELEGRAM_MAX = 4000
        header = f"MODELOS DISPONIBLES EN {provider.upper()}:"
        footer = "\n\nResponde con el NUMERO del modelo que deseas activar."
        max_body = TELEGRAM_MAX - len(header) - len(footer) - 80

        lines = []
        for idx, m in enumerate(page_models, start_idx + 1):
            # Mostrar solo el nombre del modelo, no el prefijo (ya está en el header)
            display_m = m.split("/", 1)[-1] if "/" in m else m
            line = f"{idx}. {display_m}"
            candidate_len = len("\n".join(lines + [line]))
            if candidate_len > max_body:
                break
            lines.append(line)

        page_info = f"📄 Pagina {page + 1}/{total_pages} - {start_idx + 1}-{end_idx} de {len(models)}"
        parts = [header, "", page_info, ""] + lines + [footer]
        text = "\n".join(parts)

        # Botones de navegación
        keyboard = []
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("◀️ Anterior", callback_data="more_models|prev"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Siguiente ▶️", callback_data="more_models|next"))
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append([InlineKeyboardButton("🔙 Volver a proveedores", callback_data="back_to_providers")])

        return text, InlineKeyboardMarkup(keyboard)

    async def _safe_send(self, chat_id: str, text: str, reply_markup=None, retries: int = 3):
        """Envía mensaje a Telegram con retry exponencial contra errores de red."""
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode='Markdown'
                )
                return  # Éxito
            except Exception as e:
                err_str = str(e).lower()
                # Solo reintentar en errores de red/DNS, no en parse de Markdown
                if "connect" in err_str or "resolve" in err_str or "dns" in err_str or "timeout" in err_str or "name or service" in err_str:
                    last_error = e
                    wait = 2 ** attempt  # 2, 4, 8 segundos
                    _json_log("clevel", f"Telegram send failed (attempt {attempt}/{retries}), retrying in {wait}s", {
                        "error": str(e)[:100]
                    }, "WARNING")
                    await asyncio.sleep(wait)
                else:
                    # Error no recuperable (p.ej. parse de Markdown) → reintentar sin parse_mode
                    try:
                        await self.app.bot.send_message(
                            chat_id=chat_id, text=text, reply_markup=reply_markup
                        )
                        return
                    except Exception as e2:
                        logger.warning(f"Send also failed without Markdown: {e2}")
                        last_error = e2
                        break

        # Si llegamos aquí, todos los reintentos fallaron
        _json_log("clevel", f"Telegram send failed after {retries} retries", {
            "error": str(last_error)[:150] if last_error else "unknown"
        }, "ERROR")
        # No relanzamos — el caller decide si continuar

    async def start(self):
        await self.broker.connect()
        self.app = ApplicationBuilder().token(self.token).build()
        self.app.add_handler(CallbackQueryHandler(self.handle_button))
        self.app.add_handler(MessageHandler(filters.COMMAND, self.handle_command))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text_input))

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

        # ── Test de conectividad a Telegram API ──
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                # Solo verificamos resolución DNS + conectividad, sin exponer el token
                resp = await client.get("https://api.telegram.org")
                if resp.status_code == 200:
                    _json_log("clevel", "Telegram API connectivity OK", {})
                else:
                    _json_log("clevel", "Telegram API responded (non-200 OK)", {
                        "status": resp.status_code
                    }, "INFO")
        except Exception as e:
            _json_log("clevel", "Telegram API connectivity FAILED — check DNS/network", {
                "error": str(e)[:100]
            }, "ERROR")
            # No bloqueamos — el retry en _safe_send lo manejará

        logger.info(f"Notifier HITL Online. Esperando ordenes del Director {self.chat_id}...")

        while True:
            try:
                notification = await self.broker.get_task(self.queue_in)
                if notification:
                    await self.send_decision_request(notification)
            except asyncio.CancelledError:
                break
            except Exception as e:
                _json_log("clevel", f"Error in main notification loop", {
                    "error": str(e)[:150]
                }, "ERROR")
                await asyncio.sleep(5)
            await asyncio.sleep(1)

    async def send_decision_request(self, data):
        def escape_md(text: str) -> str:
            """Escapa solo caracteres que afectan al Markdown ESTÁNDAR de Telegram.

            Telegram Markdown (NO MarkdownV2) solo interpreta:
            - _text_ → cursiva
            - *text* → negrita
            - `text` → código
            - ```text``` → bloque de código

            Caracteres como . - ! ( ) [ ] no necesitan escape.
            Escapar lo que no es necesario produce backslashes visibles (ej: \.).
            """
            if not text:
                return ""
            # Escapar SOLO lo que rompe Markdown estándar:
            # 1. Backslash mismo (para poder enviar backslashes literales)
            # 2. Guion bajo (activa/desactiva cursiva)
            # 3. Acento grave (activa/desactiva código inline)
            result = text.replace("\\", "\\\\")
            result = result.replace("_", "\\_")
            result = result.replace("`", "\\`")
            return result

        def inline_md(text: str) -> str:
            return escape_md(text).replace("\n", " ").replace("\r", " ")

        def _extract_reason(reasoning: str) -> str:
            """Extrae una RAZÓN limpia del reasoning estructurado del LLM.

            DELEGADA a extract_clean_reason() de shared/utils.py.
            Esta función solo aplica escape_md() para Telegram.
            """
            if not reasoning:
                return "Sin contexto disponible."
            return escape_md(extract_clean_reason(reasoning))

        if data.get("type") == "api_retry":
            text = (
                f"⚠️ *API {data['engine'].upper()} — Retry* "
                f"(intento {data.get('attempt', '?')}/{data.get('max_attempts', 5)})\n\n"
                f"`{escape_md(str(data.get('error', ''))[:200])}`"
            )
            await self._safe_send(chat_id=self.chat_id, text=text)
            return

        if data.get("type") == "slow_operation":
            text = (
                f"🐢 *Operación lenta*\n\n"
                f"*Operación:* `{data.get('operation')}`\n"
                f"*Duración:* {data.get('duration_seconds')}s\n"
                f"*Umbral:* {data.get('threshold_seconds')}s"
            )
            await self._safe_send(chat_id=self.chat_id, text=text)
            return

        if data.get("type") == "queue_backlog":
            text = (
                f"📊 *Backlog en cola*\n\n"
                f"*Cola:* `{data.get('queue')}`\n"
                f"*Tareas acumuladas:* {data.get('length')}\n"
                f"*Umbral:* {data.get('threshold')}"
            )
            await self._safe_send(chat_id=self.chat_id, text=text)
            return

        if data.get("type") == "learning_result":
            if data.get("success"):
                sample_count = data.get("sample_count", 0)
                tone = data.get("dominant_tone", "no detectado")
                avg_len = data.get("avg_length_chars", 0)
                greetings = data.get("greeting_patterns", [])
                signoffs = data.get("signoff_patterns", [])
                phrases = data.get("common_phrases", [])
                analysis_mode = data.get("analysis_mode", "📊 Heurística")
                behavioral_counts = data.get("behavioral_counts", {})

                lines = [
                    f"{data.get('icon', '🧠')} *AUTO-APRENDIZAJE COMPLETADO*\n",
                    f"📊 *Resumen del análisis*",
                    f"   • Correos analizados: `{sample_count}`",
                    f"   • Modo: {analysis_mode}",
                    f"   • Tono detectado: *{escape_md(tone)}*",
                    f"   • Longitud media: `{avg_len:,.0f}` caracteres\n",
                ]

                if greetings:
                    g_list = "\n".join(f"  • `{escape_md(g)}`" for g in greetings)
                    lines.append(f"👋 *Saludos detectados ({len(greetings)}):*\n{g_list}\n")

                if signoffs:
                    s_list = "\n".join(f"  • `{escape_md(s)}`" for s in signoffs)
                    lines.append(f"✍️ *Despedidas detectadas ({len(signoffs)}):*\n{s_list}\n")

                if phrases:
                    p_list = "\n".join(f"  • _{escape_md(p)}_" for p in phrases[:3])
                    lines.append(f"🔁 *Frases frecuentes:*\n{p_list}\n")

                # Behavioral rules summary
                bc = behavioral_counts
                if bc:
                    b_lines = []
                    dp = bc.get("decision_patterns", 0)
                    ak = bc.get("approval_keywords", 0)
                    et = bc.get("escalation_triggers", 0)
                    rs = bc.get("response_speed", "")
                    b_items = []
                    if dp:
                        b_items.append(f"Patrones de decisión: `{dp}`")
                    if ak:
                        b_items.append(f"Keywords de aprobación: `{ak}`")
                    if et:
                        b_items.append(f"Triggers de escalamiento: `{et}`")
                    if rs:
                        b_items.append(f"Velocidad respuesta: *{escape_md(rs)}*")
                    if b_items:
                        b_lines.append("🧠 *Reglas de comportamiento*")
                        for item in b_items:
                            b_lines.append(f"   • {item}")
                        lines.append("\n".join(b_lines) + "\n")

                lines.append(
                    "✅ Perfil actualizado — `LEARNED_PROFILE.md`\n"
                    "⏱ Próximo análisis automático en ~60 min\n"
                    "💡 Usa `/learn` para forzar un nuevo análisis ahora"
                )

                text = "\n".join(lines)
            else:
                text = (
                    f"{data.get('icon', '⚠️')} *{escape_md(data.get('title', 'Auto-Aprendizaje Falló'))}*\n\n"
                    f"El análisis de estilo no pudo completarse.\n"
                    f"Verifica que la sesión de Outlook esté activa y haya correos "
                    f"en la Bandeja de Salida."
                )

            await self._safe_send(chat_id=self.chat_id, text=text)
            return

        if data.get("type") == "system_alert":
            text = (
                f"{data.get('icon', '🔔')} *{escape_md(data.get('title', 'Alerta del sistema'))}*\n\n"
                f"{escape_md(data.get('message', ''))}"
            )
            await self._safe_send(chat_id=self.chat_id, text=text)
            return

        # v6.0: provider_degraded ELIMINADO.
        # El LLM Router Gateway es embebido y los fallos de la API OpenAI se
        # reportan como system_alert desde brain_worker.py con reintentos.

        # v5.3l: brain_failure ELIMINADO.
        #
        # ANTES: notifier trackeaba fail_counts por mail_id y tras 3 fallos
        # enviaba "3 FALLOS CONSECUTIVOS" con teclado de /setprovider.
        #
        # AHORA: El LLM Router externo maneja failover interno (8 providers
        # con 3 retries c/u). Si todos fallan, brain_worker.py envía un
        # system_alert único y reintenta con backoff exponencial.
        #
        # El tipo brain_failure se mantiene como legacy catch para evitar
        # colapsos si algún container viejo lo envía, pero se trata como
        # system_alert genérico: sin keyboard, sin fail_counts, sin /setprovider.
        if data.get("type") == "brain_failure":
            _json_log("clevel", "Received legacy brain_failure (treating as system_alert)", {
                "engine": data.get("engine"),
                "error": str(data.get('details', ''))[:100]
            }, "WARNING")
            text = (
                f"⚠️ *Router LLM: Error temporal*\n\n"
                f"El LLM Router no pudo completar la solicitud. "
                f"El sistema reintentará automáticamente con backoff.\n\n"
                f"*Detalle:* {escape_md(str(data.get('details', 'Error desconocido'))[:200])}\n\n"
                f"💡 Si el problema persiste, verifica el Router en /status."
            )
            await self._safe_send(chat_id=self.chat_id, text=text)
            return

        short_id = data.get('mail_id')[-30:] if data.get('mail_id') else "unknown"
        keyboard = [
            [InlineKeyboardButton("✅ Aceptar (Enviar)", callback_data=f"accept|{short_id}")],
            [InlineKeyboardButton("📝 Dejar en Borrador", callback_data=f"draft|{short_id}")],
            [InlineKeyboardButton("🗑 Descartar (Noise)", callback_data=f"discard|{short_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        subject = inline_md(data.get('subject', 'Sin Asunto'))
        reasoning_raw = data.get('reasoning', '')
        draft_raw = data.get('draft')
        draft = inline_md(draft_raw) if draft_raw else 'No se genero borrador.'
        sender = inline_md(data.get('sender', ''))

        # Determinar clasificación desde el reasoning del LLM
        decision_text = "⚡ ACTION"
        if 'Decision: Executive_Decision' in reasoning_raw or 'Decision: Executive' in reasoning_raw:
            decision_text = "🏛️ EXECUTIVE"
        elif 'Decision: Noise' in reasoning_raw:
            decision_text = "🔇 NOISE"

        # Extraer RAZÓN limpia para el campo Contexto:
        razon_limpia = _extract_reason(reasoning_raw)

        text = (
            f"🤖 *NUEVO CORREO* — {decision_text}\n\n"
            f"*Remitente:* {sender}\n"
            f"*Asunto:* {subject}\n\n"
            f"*Contexto:*\n{razon_limpia}\n\n"
            f"*Borrador Propuesto:*\n_{draft}_\n\n"
            f"¿Qué acción deseas tomar?"
        )

        await self._safe_send(chat_id=self.chat_id, text=text, reply_markup=reply_markup)

    async def handle_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        data_parts = query.data.split("|")
        action = data_parts[0]

        if action == "cancel_provider_select":
            try:
                await query.edit_message_text(text="⏹️  Operación cancelada. No se realizaron cambios.")
            except Exception:
                pass
            return

        if action == "set_auto":
            # v6.0: Modo Auto del gateway — borra el override hive:config:model
            # para que el gateway vuelva a elegir el modelo por intención
            # (variables de entorno LLM_GATEWAY_MODEL_*).
            await self.broker.client.delete("hive:config:model")
            _json_log("clevel", "config_changed_via_telegram", {
                "provider": "gateway",
                "model": "auto (por intención)",
                "chat_id": str(update.effective_chat.id) if update.effective_chat else self.chat_id
            })
            try:
                await query.edit_message_text(
                    text="🔄 *MODO AUTO DEL GATEWAY ACTIVADO*\n\n"
                         "El *LLM Router Gateway* detecta la intención de cada "
                         "solicitud y elige el modelo automáticamente según las "
                         "variables de entorno LLM\\_GATEWAY\\_MODEL\\_*.",
                    parse_mode='Markdown'
                )
            except Exception:
                await query.edit_message_text(
                    text="🔄 Modo Auto del gateway activado: el modelo se elige por intención."
                )
            return

        if action == "set_provider":
            provider = data_parts[1]
            chat_id = update.effective_chat.id if update.effective_chat else (
                context._chat_id if hasattr(context, "_chat_id") else self.chat_id
            )

            # v5.3g: Obtener modelos del cache del router (no más llamadas directas a APIs)
            await self._refresh_router_cache()
            p_info = self._cached_providers.get(provider, {})
            models = p_info.get("models", [])

            if not models:
                await query.edit_message_text(
                    text=f"❌ No hay modelos disponibles para {provider.upper()} en el router."
                )
                return

            self.user_states[chat_id] = {
                "step": "AWAITING_MODEL_INDEX",
                "provider": provider,
                "models": models,
                "page": 0
            }

            # Usar paginación: mostrar primera página con botones de navegación
            text, reply_markup = await self._build_model_page(chat_id)
            await query.edit_message_text(text=text, reply_markup=reply_markup)

        elif action == "more_models":
            direction = data_parts[1] if len(data_parts) > 1 else "next"
            chat_id = update.effective_chat.id if update.effective_chat else (
                context._chat_id if hasattr(context, "_chat_id") else self.chat_id
            )
            state = self.user_states.get(chat_id)
            if not state or state.get("step") != "AWAITING_MODEL_INDEX":
                return

            page = state.get("page", 0)
            if direction == "next":
                state["page"] = page + 1
            elif direction == "prev":
                state["page"] = max(0, page - 1)

            text, reply_markup = await self._build_model_page(chat_id)
            try:
                await query.edit_message_text(text=text, reply_markup=reply_markup)
            except Exception:
                pass
            return

        elif action == "back_to_providers":
            chat_id = update.effective_chat.id if update.effective_chat else (
                context._chat_id if hasattr(context, "_chat_id") else self.chat_id
            )
            # Limpiar estado actual
            if chat_id in self.user_states:
                del self.user_states[chat_id]
            # Mostrar teclado de proveedores desde el router
            reply_markup = await self._build_provider_keyboard()
            try:
                await query.edit_message_text(
                    text="*SELECCIONE MOTOR LLM:*",
                    reply_markup=reply_markup
                )
            except Exception:
                pass
            return

        elif action == "set_model":
            parts = query.data.split("|")
            if len(parts) >= 3:
                provider = parts[1]
                new_model = parts[2]

                await self.broker.client.set("hive:config:provider", provider)
                await self.broker.client.set("hive:config:model", new_model)

                await query.edit_message_text(
                    text=f"*NUEVO MODELO APLICADO*\nMotor: {provider}\nModelo: {new_model}\n\n"
                         f"El Cerebro detectara el cambio y reintentara la tarea."
                )

        elif action in ["accept", "draft", "discard"]:
            mail_id = data_parts[1] if len(data_parts) > 1 else "unknown"
            decision_payload = {"short_id": mail_id, "decision": action, "user": "Director"}
            await self.broker.push_task(self.queue_out, decision_payload)
            await query.edit_message_text(
                text=f"{query.message.text}\n\n*ORDEN RECIBIDA:* {action.upper()}"
            )

    async def handle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return
        cmd = update.message.text.strip().lower()

        if cmd == "/status":
            brain_beat = await self.broker.client.get("heartbeat:brain")
            vision_beat = await self.broker.client.get("heartbeat:vision")
            now = time.time()
            brain_status = "✅ Vivo" if brain_beat and now - float(brain_beat) < 180 else "💀 Sin señal"
            vision_status = "✅ Vivo" if vision_beat and now - float(vision_beat) < 180 else "💀 Sin señal"
            q_raw = await self.broker.client.llen("queue:raw_emails") or 0
            q_vision = await self.broker.client.llen("queue:vision_tasks") or 0
            q_notif = await self.broker.client.llen("queue:notifications") or 0
            prov_val = await self.broker.client.get("hive:config:provider")
            provider = prov_val.decode() if isinstance(prov_val, bytes) else (prov_val or "N/A")
            model_val = await self.broker.client.get("hive:config:model")
            model = model_val.decode() if isinstance(model_val, bytes) else (model_val or "N/A")

            # ── Estado del LLM Router Gateway ──
            # v6.0: Se lee desde Redis (publicado por el heartbeat del brain)
            router_health_text = "❓ No disponible"
            try:
                health_data = await self._get_router_health()
                if health_data:
                    status = health_data.get("status", "unknown")
                    engine = health_data.get("engine", "N/A")
                    default_model = health_data.get("default_model", "N/A")
                    status_icon = "🟢" if status == "online" else "🔴"
                    intent_models = health_data.get("intent_models", {})
                    lines = [
                        f"{status_icon} {status} ({engine})",
                        f"   Default: {default_model}",
                    ]
                    for intent, m in list(intent_models.items())[:4]:
                        lines.append(f"   {intent}: {m}")
                    router_health_text = "\n".join(lines)
            except Exception:
                pass

            # ── Estado de Graph API ──
            graph_status = "❓ No disponible"
            try:
                graph_raw = await self.broker.client.get("graph_api:health")
                if graph_raw:
                    if isinstance(graph_raw, bytes):
                        graph_raw = graph_raw.decode()
                    graph_data = json.loads(graph_raw)
                    g_status = graph_data.get("status", "unknown")
                    g_user = graph_data.get("user", "")
                    g_latency = graph_data.get("latency_ms", 0)
                    g_has_token = graph_data.get("has_token", False)

                    if g_status == "ok":
                        token_icon = "🔑" if g_has_token else "🔒"
                        graph_status = f"{token_icon} OK ({g_user}, {g_latency}ms)"
                    elif g_status == "no_token":
                        graph_status = "🔒 Sin token (revisar credenciales Client Credentials)"
                    else:
                        graph_status = f"❌ Error: {graph_data.get('error', 'desconocido')}"
            except Exception:
                pass

            msg = (
                f"📡 *JARVIS STATUS*\n\n"
                f"*Brain:* {brain_status}\n"
                f"*Vision:* {vision_status}\n"
                f"*Graph API:* {graph_status}\n"
                f"*Provider:* {provider}\n"
                f"*Modelo:* {model}\n"
                f"*Gateway:* {router_health_text}\n\n"
                f"*Colas:*\n"
                f"📥 raw\\_emails: {q_raw}\n"
                f"👁 vision\\_tasks: {q_vision}\n"
                f"🔔 notifications: {q_notif}\n\n"
                f"Usa /setprovider para ver o ajustar los modelos del gateway."
            )
            await update.message.reply_text(text=msg, parse_mode='Markdown')

        elif cmd == "/setprovider":
            reply_markup = await self._build_provider_keyboard()
            text = "*SELECCIONE MOTOR LLM:*"
            await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode='Markdown')

        elif cmd in ("/help", "/start"):
            msg = (
                "*COMANDOS DISPONIBLES*\n\n"
                "/status - Estado del sistema\n"
                "/setprovider - Ver/ajustar modelos del LLM Router Gateway\n"
                "/help - Esta ayuda\n"
                "/learn - Forzar auto-aprendizaje ahora"
            )
            await update.message.reply_text(text=msg, parse_mode='Markdown')

        elif cmd == "/learn":
            await update.message.reply_text(
                text="🧠 *Órden recibida. Forzando auto-aprendizaje...*\n\n"
                     "El Vision Worker ejecutará el análisis de estilo "
                     "en la Bandeja de Salida y actualizará el perfil.\n\n"
                     "Resultado disponible en unos minutos.",
                parse_mode='Markdown'
            )
            await self.broker.push_task("queue:commands", {
                "type": "force_auto_learning",
                "source": "telegram",
                "chat_id": str(update.effective_chat.id) if update.effective_chat else self.chat_id,
                "timestamp": time.time()
            })
            _json_log("clevel", "Manual auto-learning triggered via Telegram /learn", {
                "command": "/learn",
                "source": "telegram"
            })

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            return

        chat_id = update.effective_chat.id if update.effective_chat else (
            context._chat_id if hasattr(context, "_chat_id") else self.chat_id
        )

        state = self.user_states.get(chat_id)
        if not state or state.get("step") != "AWAITING_MODEL_INDEX":
            return

        text = update.message.text.strip().lower()
        models = state.get("models", [])
        provider = state.get("provider")

        # ── Cancelar ──
        cancel_words = {"0", "cancel", "cancelar", "exit", "salir", "quit", "x", "no"}
        if text in cancel_words:
            if chat_id in self.user_states:
                del self.user_states[chat_id]
            try:
                await update.message.reply_text(
                    text="⏹️  Operación cancelada. No se realizaron cambios al modelo."
                )
            except Exception:
                pass
            return

        if text.isdigit():
            index = int(text)
            if 1 <= index <= len(models):
                selected_model = models[index - 1]

                # v5.3g: Guardar modelo CON prefijo de provider (provider/model)
                # para que el router externo sepa a qué proveedor enrutar
                prefixed_model = f"{provider}/{selected_model}" if "/" not in selected_model else selected_model
                
                await self.broker.client.set("hive:config:provider", provider)
                await self.broker.client.set("hive:config:model", prefixed_model)

                _json_log("clevel", "config_changed_via_telegram", {
                    "provider": provider,
                    "model": prefixed_model,
                    "chat_id": str(chat_id)
                })

                try:
                    await update.message.reply_text(
                        text=f"Modelo {prefixed_model} configurado correctamente.",
                        parse_mode='Markdown'
                    )
                except Exception:
                    await update.message.reply_text(
                        text=f"Modelo {prefixed_model} configurado correctamente."
                    )

                if chat_id in self.user_states:
                    del self.user_states[chat_id]
                return

        try:
            await update.message.reply_text(
                text=f"Indice fuera de rango. Por favor ingresa un numero entre 1 y {len(models)}:",
                parse_mode='Markdown'
            )
        except Exception:
            await update.message.reply_text(
                text=f"Indice fuera de rango. Por favor ingresa un numero entre 1 y {len(models)}:"
            )

if __name__ == "__main__":
    worker = NotifierWorker()
    asyncio.run(worker.start())
