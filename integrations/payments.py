"""
integrations/payments.py — interface única para "este lead pagou?"

Estado atual: stub. O analyzer.py chama get_payment_for_phone() para obter
ground truth de pagamento; sem webhook, retorna None e o analyzer cai em
inferência por LLM a partir do texto da conversa.

────────────────────────────────────────────────────────────────────────
Quando ligar Kiwify/Stripe — passo a passo:
────────────────────────────────────────────────────────────────────────

PASSO 1 — Subir endpoint webhook
    Opção A (recomendada): adicionar rota Flask/FastAPI dedicada num
    pequeno serviço Python rodando ao lado do watcher (porta 9000 por
    exemplo). PM2 cuida.

    Opção B: usar a Evolution API — não recomendado, ela é só pra WhatsApp.

    Opção C: um endpoint HTTP serverless (Cloudflare Worker, Vercel) que
    reposta o payload via POST pra um webhook na máquina via tunnel.

PASSO 2 — Configurar o provedor
    Kiwify: Configurações → Webhooks → Adicionar URL do passo 1, eventos
            "approved", "refunded", "chargeback".
    Stripe: Dashboard → Webhooks → Add endpoint, eventos
            "checkout.session.completed", "charge.refunded".

PASSO 3 — Validar assinatura
    Kiwify envia assinatura HMAC-SHA1 no header `X-Kiwify-Signature` —
    validar com a secret do painel.
    Stripe envia `Stripe-Signature` — validar com a stripe.Webhook lib.

PASSO 4 — Criar a tabela `payments`
    Schema esperado:

        CREATE TABLE payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone           TEXT,           -- normalizado (apenas dígitos)
            email           TEXT,
            amount          REAL,
            currency        TEXT DEFAULT 'USD',
            paid_at         INTEGER,        -- unix timestamp
            source          TEXT,           -- 'kiwify' | 'stripe'
            event_type      TEXT,           -- approved | refunded | chargeback
            transaction_id  TEXT UNIQUE,    -- id do provedor (idempotência)
            raw_json        TEXT
        );
        CREATE INDEX idx_payments_phone ON payments(phone);
        CREATE INDEX idx_payments_email ON payments(email);

PASSO 5 — Implementar get_payment_for_phone() abaixo
    Trocar `return None` por SELECT na tabela. Match prioritário por
    phone normalizado. Fallback por email se vier no checkout. Tolerância
    de 14 dias entre primeira mensagem do lead e o paid_at — pagamentos
    fora dessa janela provavelmente não são desse lead.

PASSO 6 — Reagir a refund/chargeback
    Quando event_type='refunded' ou 'chargeback', marcar
    leads.outcome='refunded' pra que o analyzer não conte como venda no
    aprendizado. Idealmente disparar follow-up de retenção.

NOTA SOBRE PRIVACIDADE:
    Persistir só phone normalizado, email, valor e ID. Nunca persistir
    dados completos do cartão (PCI). raw_json deve ser um payload já
    sanitizado pelo provedor — verifique o que vem.
"""

import sqlite3
from pathlib import Path
from typing import Optional


_DB_PATH = Path.home() / "meu-agente" / "dados.sqlite"


def get_payment_for_phone(phone: str, since_ts: int = 0) -> Optional[dict]:
    """Retorna {amount, currency, paid_at, source, transaction_id} se houver
    pagamento confirmado para este telefone após `since_ts`, ou None.

    Implementação atual: stub — retorna None até webhook estar plugado.
    """
    return None


def normalize_phone(phone: str) -> str:
    """Mantém apenas dígitos (formato BR: 55XXXXXXXXXXX)."""
    return "".join(c for c in phone if c.isdigit())
