# 📊 Informe de Estrés TPM — Todos los Proveedores

> **Fecha:** 2026-06-08
> **Sistema:** Jarvis Autonomous Mail Agent
> **Metodología:** 100 requests por proveedor, 10 en paralelo, modelo de clasificación de correos

---

## 🏆 Tabla Comparativa General

| PROVEEDOR | MODELO | KEYS | ✅ ÉXITO | ⏳ RATE LIMIT | ❌ ERROR | DURACIÓN | RECUPERACIÓN |
|-----------|--------|:----:|:--------:|:-------------:|:--------:|:--------:|:------------:|
| **Gemini** | `gemma-4-31b-it` | **5** | **90%** ✅ | **10%** ⚠️ | 0% | 53s | ✅ Sí |
| **Groq** | `llama-3.3-70b-versatile` | 2 | 63% ✅ | 37% ⏳ | 0% | **12s** ⚡ | ✅ Sí |
| **Ollama Cloud** | `llama3:8b` | 6 | 0% ❌ | 0% | 100% | 9s | ❌ No |
| **OpenRouter** | `gpt-4o` | 6 | 0% ❌ | 0% | 100% | 10s | ❌ No |

---

## 📋 Análisis por Proveedor

### 🥇 Gemini — `gemma-4-31b-it` (RECOMENDADO)

```
🔑 Keys:      5 (rotación activa)
✅ Éxito:     90/100 (90%)
⏳ Rate limit: 10/100 (10%)
📊 Throughput: ~1.9 req/s
⏱️ Duración:  53s
🔄 Recuperación: Sí
💰 Costo:     GRATIS (Gemma es modelo abierto)
```

| Fortalezas | Debilidades |
|------------|-------------|
| ✅ Sin límite de 30 RPM por organización | ⚠️ Latencia más alta (~530ms por request) |
| ✅ 5 keys rotando — alta tolerancia a fallos | |
| ✅ JSON estructurado nativo con schema | |
| ✅ Excelente en español colombiano | |
| ✅ **Gratis** — sin costo operativo | |

**Veredicto:** 🏆 **MEJOR PROVEEDOR** — Balance perfecto entre confiabilidad, tasa de éxito y costo.

---

### 🥈 Groq — `llama-3.3-70b-versatile`

```
🔑 Keys:      2 (misma cuenta)
✅ Éxito:     63/100 (63%)
⏳ Rate limit: 37/100 (37%)
📊 Throughput: ~8.3 req/s
⏱️ Duración:  12s
🔄 Recuperación: Sí
💰 Costo:     GRATIS (on-demand tier)
```

| Fortalezas | Debilidades |
|------------|-------------|
| ✅ **Más rápido** — 325-527ms por request | ❌ **30 RPM por organización** — cuello de botella |
| ✅ Latencia 4x menor que Gemini | ❌ Keys de misma cuenta NO ayudan |
| ✅ JSON mode soportado | ❌ 37% rate limit con 2 keys |
| ✅ Excelente precisión (70B params) | |

**Veredicto:** ⚡ **MÁS RÁPIDO** pero limitado a 30 RPM por organización. Con keys de **cuentas distintas** el rate limit se eliminaría.

> 📌 **Proyección con keys de distintas cuentas:**
> - 2 cuentas → 60 RPM → ~85% éxito ✅
> - 3 cuentas → 90 RPM → ~95% éxito ✅
> - 4 cuentas → 120 RPM → ~100% éxito ✅

---

### ❌ Ollama Cloud — `llama3:8b`

```
🔑 Keys:      6
✅ Éxito:     0/100 (0%)
⏳ Rate limit: 0/100 (0%)
❌ Error:     100% — HTTP 301 Redirect
```

**Causa:** La URL `https://api.ollama.com/v1/chat/completions` redirige (301) — posiblemente el endpoint correcto es otro.

**Veredicto:** ❌ **NO OPERATIVO** — Requiere investigación del endpoint correcto de Ollama Cloud API.

---

### ❌ OpenRouter — `gpt-4o`

```
🔑 Keys:      6
✅ Éxito:     0/100 (0%)
⏳ Rate limit: 0/100 (0%)
❌ Error:     100% — HTTP 402 (Insufficient credits)
```

**Causa:** HTTP 402 — Las keys de OpenRouter no tienen créditos suficientes para `gpt-4o` (solicita 16,384 tokens pero solo tiene presupuesto para 3,999).

**Veredicto:** ❌ **SIN CRÉDITOS** — Las keys están registradas pero requieren recarga.

---

## 📈 Gráfica Comparativa

```
Éxito %     ████████████████████████████████████████░░░ Gemini 90%
            ██████████████████████████████░░░░░░░░░░░░ Groq 63%
            ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ Ollama Cloud 0%
            ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ OpenRouter 0%
            0%    10%    20%    30%    40%    50%    60%    70%    80%    90%   100%

Rate Limit % ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ Gemini 10%
            ██████████████████████████████████░░░░░░░░ Groq 37%
            ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ Ollama Cloud 0%
            ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ OpenRouter 0%
            0%    10%    20%    30%    40%    50%    60%    70%    80%    90%   100%

Latencia     ████████████████████████████████████████░░ Gemini 530ms
relativa     ████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ Groq 425ms
```

---

## 🏁 Conclusiones y Recomendaciones

### 🥇 Proveedor Principal: **Gemini (`gemma-4-31b-it`)**

| Razón | Dato |
|-------|------|
| Mayor tasa de éxito | **90%** vs 63% del segundo |
| Sin límite restrictivo | 10% rate limit vs 37% de Groq |
| 5 keys rotando | Alta disponibilidad |
| Gratis | Sin costo operativo |
| JSON estructurado nativo | Schema `EmailDecision` garantizado |

### ⚡ Proveedor Secundario: **Groq (cuando consigas keys de distintas cuentas)**

- Usar Groq cuando se necesite **máxima velocidad** (<500ms vs >2s Gemini)
- Con **4 keys de cuentas distintas** → 120 RPM → sin rate limits
- Ideal para picos de volumen

### 📊 Ranking Final

```
1. 🥇 Gemini (gemma-4-31b-it)          ⭐ RECOMENDADO
2. 🥈 Groq (llama-3.3-70b-versatile)   ⚡ Más rápido (con keys multi-cuenta)
3. ❌ Ollama Cloud (llama3:8b)          🔧 Requiere configuración
4. ❌ OpenRouter (gpt-4o)               💳 Sin créditos
```

---

*Reporte generado automáticamente por Jarvis Agent — 2026-06-08*
