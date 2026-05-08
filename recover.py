#!/usr/bin/env python3
"""
recover.py — Recuperação diária de leads em silêncio.

Procura conversas que ficaram paradas (sem mensagem do lead há ≥ N horas)
e que não foram cobertas pelos followups automáticos (price_followup de 30min,
checkout followup de 2h). Para cada lead encontrado:
  1. Classifica em qual etapa do funil parou.
  2. Gera, via LLM, uma frase curta de gancho específica àquele lead.
  3. Compõe a mensagem (gancho + bloco de retomada da etapa).
  4. Envia via Evolution API e registra no histórico do lead.
  5. Marca leads.daily_recovered_at — cada lead só recebe recover UMA vez.

Uso:
  python3 recover.py [--dry-run] [--max-hours 24] [--limit 30]
    --dry-run    : não envia, só imprime o que enviaria
    --max-hours  : silêncio mínimo (padrão: 24h)
    --limit      : máximo de envios por execução (padrão: 30)
"""

import argparse
import json
import logging
import re
import sqlite3
import time
import urllib.request
import urllib.error
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))

from agent import (
    init_db, call_ai, is_payment_confirmation,
    ALL_CHECKOUTS, OWNER_PHONE, _PRICE_PATTERN,
    DB_PATH, MAX_HISTORY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("recover")

# ── Evolution API ─────────────────────────────────────────────────────────────
EVOLUTION_URL     = "http://localhost:8080"
EVOLUTION_API_KEY = "aqYCBaeh-k_UL6-nbj0kKaKQxDSKkoPEi6rbBvtFsFY"
INSTANCE_NAME     = "meu-agente"


def send_whatsapp(phone: str, text: str) -> bool:
    req = urllib.request.Request(
        f"{EVOLUTION_URL}/message/sendText/{INSTANCE_NAME}",
        data=json.dumps({"number": phone, "text": text, "delay": 3000}).encode(),
        headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
        return bool(result.get("key") or result.get("id"))
    except Exception as e:
        logger.error(f"Evolution erro: {e}")
        return False


# ── Detecção de etapa ─────────────────────────────────────────────────────────

STAGE_PRE_PRICE          = "pre_price"
STAGE_PRICE_SEEN         = "price_seen"
STAGE_LINK_SENT          = "link_sent"
STAGE_LINK_FOLLOWUP_DONE = "link_followup_done"


def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def find_stalled_leads(max_hours: int) -> list:
    """Leads silentes há ≥max_hours, não pausados, não recuperados, não dono,
    sem confirmação de pagamento."""
    cutoff_ts = int(time.time()) - max_hours * 3600
    conn = _db()
    c = conn.cursor()
    c.execute("""
        SELECT l.id, l.name, l.phone, l.sent_checkout, l.outcome,
               (SELECT MAX(ts) FROM messages WHERE lead_id = l.id) AS last_ts,
               (SELECT followup_sent FROM followups WHERE lead_id = l.id) AS link_fu_done
        FROM leads l
        WHERE COALESCE(l.paused, 0) = 0
          AND l.daily_recovered_at IS NULL
          AND l.phone != ?
          AND (l.outcome IS NULL OR l.outcome != 'paid')
    """, (OWNER_PHONE,))
    rows = []
    for r in c.fetchall():
        if r["last_ts"] is None or r["last_ts"] > cutoff_ts:
            continue
        rows.append(dict(r))
    conn.close()
    return rows


def _load_messages(lead_id: str) -> list:
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT role, content, ts FROM messages WHERE lead_id = ? ORDER BY ts ASC",
        (lead_id,)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def classify_stage(lead_row: dict, messages: list) -> str:
    assistant_texts = [m["content"] for m in messages if m["role"] == "assistant"]
    user_texts      = [m["content"] for m in messages if m["role"] == "user"]

    # Confirmação de pagamento em qualquer mensagem do lead → pula
    for t in user_texts:
        if is_payment_confirmation(t):
            return None  # não recuperar — já pagou

    has_link  = any(any(c in t for c in ALL_CHECKOUTS) for t in assistant_texts)
    has_price = any(_PRICE_PATTERN.search(t) for t in assistant_texts)
    link_fu_done = bool(lead_row.get("link_fu_done"))

    if has_link and link_fu_done:
        return STAGE_LINK_FOLLOWUP_DONE
    if has_link:
        return STAGE_LINK_SENT
    if has_price:
        return STAGE_PRICE_SEEN
    return STAGE_PRE_PRICE


# ── Gancho personalizado via LLM ──────────────────────────────────────────────

_HOOK_SYSTEM = (
    "Eres asistente de Chef Sandra. A partir del historial abajo, escribe UNA "
    "sola frase corta en español (máximo 14 palabras) para retomar la "
    "conversación con calidez — referenciando un DETALLE CONCRETO mencionado "
    "por el lead.\n\n"
    "Reglas:\n"
    "- DEBE ser una AFIRMACIÓN, NUNCA una pregunta. Sin '¿' ni '?'.\n"
    "- DEBE referenciar un detalle concreto: un alimento específico que extrañe "
    "(pan, postres, tortas...), la persona para quien es (mamá, hijo, esposo...), "
    "la profesión/uso (buffet, consultorio, cumpleaños...), o una preocupación "
    "específica (glucosa, peso, familia). Ej buenos:\n"
    "  • 'Sé que extrañas el pan de cada mañana, María 💚'\n"
    "  • 'Pensando en lo que me contaste de tu mamá y los postres'\n"
    "  • 'Imaginé tu buffet ofreciendo opciones para diabéticos'\n"
    "- PROHIBIDO usar frases genéricas como 'sé que buscas recetas saludables' "
    "o 'espero que estés bien' — eso es flojo.\n"
    "- Si en el historial NO hay un detalle concreto suficiente para personalizar, "
    "devuelve exactamente: GENERIC\n"
    "- NO uses comillas, NO empieces con 'Hola', NO firmes.\n"
    "- Devuelve SOLO la frase."
)


def _format_transcript(messages: list, max_pairs: int = 10) -> str:
    msgs = messages[-(max_pairs * 2):]
    lines = []
    for m in msgs:
        who = "Sandra" if m["role"] == "assistant" else "Lead"
        body = m["content"].replace("\n", " ").strip()
        if len(body) > 200:
            body = body[:200] + "…"
        lines.append(f"{who}: {body}")
    return "\n".join(lines)


def generate_hook(messages: list) -> str:
    transcript = _format_transcript(messages)
    user_msg = f"Conversación:\n{transcript}\n\nFrase de retomada:"
    try:
        out = call_ai(
            [{"role": "user", "content": user_msg}],
            max_tokens=60,
            system=_HOOK_SYSTEM,
        )
    except Exception as e:
        logger.warning(f"Hook LLM falhou: {e}")
        return ""

    out = (out or "").strip().strip('"\'').strip()
    out = out.replace("\n", " ")
    if not out or out.upper() == "GENERIC" or len(out) > 200:
        return ""
    if out.lower().startswith("hola"):
        out = re.sub(r"^hola[^,.!?]*[,.!?]\s*", "", out, flags=re.I).strip()
    # rejeita perguntas (modelo ignorou a regra)
    if "?" in out or "¿" in out:
        return ""
    # capitaliza primeira letra
    if out and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


# ── Composição da mensagem ────────────────────────────────────────────────────

def _first_name(name: str) -> str:
    if not name:
        return "amig@"
    return name.split()[0]


def compose_message(name: str, stage: str, hook: str) -> str:
    n = _first_name(name)
    intro = f"Hola {n} 😊 {hook}" if hook else f"Hola {n} 😊"

    if stage == STAGE_PRE_PRICE:
        body = ("Te dejé pendiente la presentación completa de los libros — "
                "¿quieres que retomemos por aquí? 💚")
    elif stage == STAGE_PRICE_SEEN:
        body = ("Recordando que el valor lo eliges tú: $5.00 / $6.90 / $9.90 / "
                "$12.90 USD (solo convierte a tu moneda local). "
                "¿Con cuál te quedas y te paso el link al toque? 💚")
    elif stage == STAGE_LINK_SENT:
        body = ("¿Pudiste avanzar con el link de pago, o quieres que te lo "
                "reenvíe? Cualquier duda en el proceso me cuentas por aquí "
                "y te ayudo 💚")
    elif stage == STAGE_LINK_FOLLOWUP_DONE:
        body = ("Pasaron algunos días desde el link y aún no te vi por aquí. "
                "Si quedó alguna duda en el camino, escríbeme — quiero que "
                "tengas acceso al material 💚")
    else:
        body = "¿Cómo va todo? Si necesitas algo, escríbeme por aquí 💚"

    return f"{intro}\n\n{body}"


# ── Persistência ──────────────────────────────────────────────────────────────

def mark_recovered(lead_id: str, message_text: str):
    """Marca o lead como recuperado e registra a mensagem no histórico."""
    now = int(time.time())
    conn = _db()
    c = conn.cursor()
    c.execute("UPDATE leads SET daily_recovered_at = ? WHERE id = ?", (now, lead_id))
    c.execute(
        "INSERT INTO messages (lead_id, role, content, ts) VALUES (?, 'assistant', ?, ?)",
        (lead_id, message_text, now)
    )
    conn.commit()
    conn.close()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="não envia, só imprime")
    ap.add_argument("--max-hours", type=int, default=24,
                    help="silêncio mínimo em horas (padrão: 24)")
    ap.add_argument("--limit", type=int, default=30,
                    help="máximo de envios por execução (padrão: 30)")
    ap.add_argument("--sleep", type=float, default=2.0,
                    help="pausa entre envios em segundos (padrão: 2)")
    args = ap.parse_args()

    init_db()  # garante coluna daily_recovered_at

    leads = find_stalled_leads(args.max_hours)
    logger.info(f"📋 {len(leads)} lead(s) silentes há ≥{args.max_hours}h não recuperados")

    attempted = sent = skipped = 0
    for lead in leads:
        if attempted >= args.limit:
            logger.info(f"⏸  limite de {args.limit} atingido, parando")
            break

        messages = _load_messages(lead["id"])
        stage = classify_stage(lead, messages)
        if stage is None:
            logger.info(f"  ↷ {lead['phone']} ({lead['name']}): pagou — pulando")
            skipped += 1
            continue

        hook = generate_hook(messages)
        text = compose_message(lead["name"], stage, hook)
        attempted += 1

        logger.info(f"  → {lead['phone']} ({lead['name']}) [stage={stage}]")
        logger.info(f"     hook: {hook or '(genérico)'}")
        if args.dry_run:
            logger.info(f"     [DRY-RUN] msg:\n{text}\n")
            continue

        ok = send_whatsapp(lead["phone"], text)
        if ok:
            mark_recovered(lead["id"], text)
            logger.info(f"     ✅ enviado e registrado")
            sent += 1
            time.sleep(args.sleep)
        else:
            logger.error(f"     ❌ falha no envio — não marcado como recuperado")

    logger.info(f"=== fim: {sent} enviados, {skipped} pulados (de {attempted} tentativas) ===")


if __name__ == "__main__":
    main()
