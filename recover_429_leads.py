#!/usr/bin/env python3
"""
recover_429_leads.py — Script único para recuperar os 4 leads que pediram
receitas e ficaram sem resposta enquanto a OpenAI estava em HTTP 429
(09/05 noite + 10/05 madrugada).

Esses leads nunca receberam NENHUMA resposta da Sandra — então a mensagem
não pode ser "te dejé pendiente la presentación" (template padrão do
recover.py). Tem que ser uma abertura de PASO 1 com desculpa pela demora.

Uso:
  python3 recover_429_leads.py [--dry-run]
"""

import argparse
import json
import logging
import sqlite3
import time
import urllib.request
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))

from agent import DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("recover_429")

EVOLUTION_URL     = "http://localhost:8080"
EVOLUTION_API_KEY = "aqYCBaeh-k_UL6-nbj0kKaKQxDSKkoPEi6rbBvtFsFY"
INSTANCE_NAME     = "meu-agente"

# Leads identificados via cruzamento do log do PM2 com a tabela `messages`:
# pediram "Quiero las recetas para diabéticos 🍞" e a última msg deles é
# user (sem resposta). Janela: 09/05 22:00 → 10/05 02:00 UTC.
TARGETS = [
    ("wa_59895151018", "59895151018", "Veroydany"),
    ("wa_59177497708", "59177497708", "ramirolasalle7"),  # pushName ruim, vai cair em saudação neutra
    ("wa_59894929863", "59894929863", "Susi"),
    ("wa_59896108653", "59896108653", "Raulito"),
]

OPENING = (
    "¡Hola! 😊 Disculpa la demora en responderte — soy la Chef Sandra. "
    "Vi tu mensaje sobre las recetas para diabéticos y quiero ayudarte. "
    "¿Cómo te llamas?"
)


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    sent = 0
    for lead_id, phone, label in TARGETS:
        logger.info(f"→ {phone} ({label})")
        if args.dry_run:
            logger.info(f"   [DRY-RUN] msg:\n{OPENING}\n")
            continue

        ok = send_whatsapp(phone, OPENING)
        if not ok:
            logger.error(f"   ❌ falha no envio — não registrando")
            continue

        now = int(time.time())
        c.execute(
            "INSERT INTO messages (lead_id, role, content, ts) VALUES (?, 'assistant', ?, ?)",
            (lead_id, OPENING, now)
        )
        # Marca pra não ser pego pelo recover.py padrão
        c.execute(
            "UPDATE leads SET daily_recovered_at = ? WHERE id = ?",
            (now, lead_id)
        )
        conn.commit()
        logger.info(f"   ✅ enviado e registrado")
        sent += 1
        time.sleep(2)

    conn.close()
    logger.info(f"=== fim: {sent}/{len(TARGETS)} enviados ===")


if __name__ == "__main__":
    main()
