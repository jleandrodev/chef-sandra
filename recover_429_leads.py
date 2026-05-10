#!/usr/bin/env python3
"""
recover_429_leads.py — Recuperação de leads atingidos por OUTAGE
(OpenAI HTTP 429, Evolution down, queda do watcher, etc.).

QUANDO USAR
-----------
Toda vez que a Sandra ficou impossibilitada de responder por algum
tempo e leads NOVOS (que mandaram a primeira mensagem "Quiero las
recetas..." e não receberam NADA) ficaram em silêncio. Diferente do
recover.py padrão (que assume conversa em andamento), este script abre
com PASO 1 + desculpa breve — apropriado pra quem nunca teve nenhuma
resposta.

INCIDENTE QUE MOTIVOU (referência histórica)
--------------------------------------------
09/05 noite + 10/05 madrugada — chave OpenAI do projeto Luke (mesmo
billing) estourou rate limit, derrubando também a Chef Sandra com
HTTP 429 nos retries. 5 leads pediram receitas e ficaram sem resposta:
Veroydany, ramirolasalle7, Susi, Raulito, Mabel.

COMO USAR EM UM PRÓXIMO INCIDENTE
---------------------------------
1. Identificar a janela do outage (logs do PM2 watcher / dashboard
   da OpenAI / horário do alerta).
2. Rodar a query abaixo no banco pra listar leads candidatos:

     SELECT l.id, l.name, l.phone,
            datetime(m.ts,'unixepoch') AS last_msg_at,
            substr(m.content,1,80) AS last_msg
     FROM leads l
     JOIN messages m ON m.id = (SELECT MAX(id) FROM messages WHERE lead_id = l.id)
     WHERE m.role = 'user'
       AND m.ts BETWEEN <inicio_outage_unix> AND <fim_outage_unix>
       AND COALESCE(l.paused,0) = 0
       AND l.daily_recovered_at IS NULL
       AND (l.outcome IS NULL OR l.outcome != 'paid')
     ORDER BY m.ts ASC;

3. Revisar manualmente — pular leads cuja última msg é fechamento real
   ("gracias chau"), responder só os que pediram algo e ficaram no vácuo.
4. Editar a lista TARGETS abaixo com (lead_id, phone, label).
5. Rodar dry-run pra conferir: `python3 recover_429_leads.py --dry-run`
6. Rodar real: `python3 recover_429_leads.py`

O script é IDEMPOTENTE: leads com daily_recovered_at já preenchido são
pulados automaticamente, então pode rodar sem medo se a lista crescer
por etapas.

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
    ("wa_59898307436", "59898307436", "Mabel"),  # mandou "Quiero..." + "Gracias!!!" sem ser respondida
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

    sent = skipped = 0
    for lead_id, phone, label in TARGETS:
        # Pula se já foi recuperado em rodada anterior (idempotência —
        # script pode ser re-rodado se TARGETS for ampliado depois)
        c.execute("SELECT daily_recovered_at FROM leads WHERE id = ?", (lead_id,))
        row = c.fetchone()
        if row and row[0]:
            logger.info(f"↷ {phone} ({label}): já recuperado em rodada anterior — pulando")
            skipped += 1
            continue

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
