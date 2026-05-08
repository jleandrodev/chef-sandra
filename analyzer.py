#!/usr/bin/env python3
"""
analyzer.py — Fase 3 do sistema de aprendizado.

Roda a skill `sales_analyst.md` em cada conversa do dia (ou de um período
configurável) e persiste o resultado estruturado em `conversation_analysis`.

Uso:
    python3 analyzer.py                  # analisa últimas 24h
    python3 analyzer.py --hours 48       # últimas 48h
    python3 analyzer.py --since 2026-05-04   # desde data
    python3 analyzer.py --lead wa_55...  # lead específico
    python3 analyzer.py --reanalyze      # ignora análises existentes do dia

Idempotente por padrão: se uma conversa já foi analisada no dia corrente
(`analyzed_at` >= 00:00 de hoje), pula. Use --reanalyze pra forçar.

Outcome real (Kiwify/Stripe) é consultado via `integrations.payments`. Sem
webhook plugado, retorna None e o outcome cai 100% na inferência da skill.
"""

import argparse
import json
import re
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from agent import (
    _db, _read_file, _KNOWLEDGE_DIR, call_ai,
    OWNER_PHONE,
)
from integrations.payments import get_payment_for_phone


# ── Configuração ──────────────────────────────────────────────────────────────

ANALYZER_MODEL      = "gpt-4o-mini"
ANALYZER_MAX_TOKENS = 1200
MIN_USER_MSGS       = 2     # conversas com menos que isto entram como atypical=insuficiente
MAX_HISTORY_TO_SKILL = 50   # quantas mensagens do final passar pra skill


# ── Skill loader ──────────────────────────────────────────────────────────────

def _sales_analyst_prompt() -> str:
    return _read_file(_KNOWLEDGE_DIR / "skills" / "sales_analyst.md")


# ── Coleta de leads a analisar ────────────────────────────────────────────────

def _leads_with_activity_since(since_ts: int) -> list:
    """Retorna lead_ids que tiveram mensagem do user em ts >= since_ts."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT DISTINCT m.lead_id
           FROM messages m
           JOIN leads l ON l.id = m.lead_id
           WHERE m.ts >= ? AND m.role = 'user' AND l.id NOT LIKE 'owner_%'""",
        (since_ts,)
    )
    rows = [r["lead_id"] for r in c.fetchall()]
    conn.close()
    return rows


def _conversation_for(lead_id: str) -> dict:
    """Retorna {messages, lead_meta, user_msg_count}."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT id, name, phone, sent_checkout, profile_slug, profile_confidence, "
        "outcome, created_at FROM leads WHERE id = ?", (lead_id,)
    )
    lead = dict(c.fetchone() or {})
    c.execute(
        "SELECT role, content, ts FROM messages WHERE lead_id = ? ORDER BY ts ASC",
        (lead_id,)
    )
    msgs = [dict(r) for r in c.fetchall()]
    conn.close()
    user_count = sum(1 for m in msgs if m["role"] == "user")
    return {"messages": msgs, "lead_meta": lead, "user_msg_count": user_count}


def _was_analyzed_today(lead_id: str) -> bool:
    today_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT analyzed_at FROM conversation_analysis WHERE lead_id = ?", (lead_id,)
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    return (row["analyzed_at"] or 0) >= today_start


# ── Formatação pra skill ──────────────────────────────────────────────────────

def _format_conversation(messages: list, max_msgs: int = MAX_HISTORY_TO_SKILL) -> str:
    recent = messages[-max_msgs:]
    return "\n".join(
        f"[{m['role']} @{datetime.fromtimestamp(m['ts']).strftime('%H:%M')}]: {m['content']}"
        for m in recent
    )


def _build_user_prompt(convo: dict, payment_truth: Optional[dict]) -> str:
    lead = convo["lead_meta"]
    transcript = _format_conversation(convo["messages"])

    payment_block = (
        f"GROUND TRUTH DE PAGAMENTO: {json.dumps(payment_truth, ensure_ascii=False)}"
        if payment_truth else
        "GROUND TRUTH DE PAGAMENTO: indisponível (webhook ainda não plugado — infira do texto)"
    )

    perfis_ativos = []
    profiles_dir = _KNOWLEDGE_DIR / "profiles"
    if profiles_dir.exists():
        for p in sorted(profiles_dir.glob("*.md")):
            if p.name.startswith("_"):
                continue
            perfis_ativos.append(f"- {p.stem}")
    perfis_block = "\n".join(perfis_ativos) if perfis_ativos else "(nenhum)"

    return (
        f"=== METADATA ===\n"
        f"lead_id: {lead.get('id')}\n"
        f"phone: {lead.get('phone')}\n"
        f"name: {lead.get('name')}\n"
        f"created_at: {lead.get('created_at')}\n"
        f"sent_checkout: {bool(lead.get('sent_checkout'))}\n"
        f"profile_slug_atual: {lead.get('profile_slug') or 'null'}\n"
        f"\n"
        f"=== PERFIS JÁ ATIVOS (refine antes de propor novo) ===\n"
        f"{perfis_block}\n"
        f"\n"
        f"=== {payment_block} ===\n"
        f"\n"
        f"=== TRANSCRIPT ===\n"
        f"{transcript}\n"
        f"\n"
        f"Analise. Responda apenas com o JSON descrito na sua instrução."
    )


# ── Parsing do output da skill ────────────────────────────────────────────────

def _strip_json_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    return s


def _parse_skill_output(raw: str) -> dict:
    return json.loads(_strip_json_fences(raw))


# ── Persistência ──────────────────────────────────────────────────────────────

def _save_analysis(lead_id: str, parsed: dict, raw: str):
    conn = _db()
    c = conn.cursor()
    c.execute(
        """INSERT OR REPLACE INTO conversation_analysis
           (lead_id, analyzed_at, atypical, atypical_reason, outcome,
            outcome_confidence, profile_suggestion, what_worked,
            what_failed, objections_seen, evidence_quality, raw_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            lead_id,
            int(time.time()),
            1 if parsed.get("atypical") else 0,
            parsed.get("atypical_reason"),
            parsed.get("outcome"),
            float(parsed.get("outcome_confidence") or 0.0),
            json.dumps(parsed.get("profile_suggestion"),  ensure_ascii=False),
            json.dumps(parsed.get("what_worked"),         ensure_ascii=False),
            json.dumps(parsed.get("what_failed"),         ensure_ascii=False),
            json.dumps(parsed.get("objections_seen"),     ensure_ascii=False),
            parsed.get("evidence_quality"),
            raw,
        )
    )
    # Reflete outcome no lead (fica útil pra reduce diário e métricas)
    if parsed.get("outcome") and not parsed.get("atypical"):
        c.execute(
            "UPDATE leads SET outcome = ?, outcome_inferred_at = ? WHERE id = ?",
            (parsed["outcome"], int(time.time()), lead_id)
        )
    conn.commit()
    conn.close()


def _save_failure(lead_id: str, reason: str, raw: str):
    """Marca como atípica com motivo de falha técnica — não polui o aprendizado."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        """INSERT OR REPLACE INTO conversation_analysis
           (lead_id, analyzed_at, atypical, atypical_reason, raw_json)
           VALUES (?, ?, 1, ?, ?)""",
        (lead_id, int(time.time()), f"analyzer_failure: {reason}", raw)
    )
    conn.commit()
    conn.close()


# ── Loop principal ────────────────────────────────────────────────────────────

def analyze_one(lead_id: str, force: bool = False) -> dict:
    if not force and _was_analyzed_today(lead_id):
        return {"lead_id": lead_id, "skipped": "already_analyzed_today"}

    convo = _conversation_for(lead_id)
    if convo["user_msg_count"] < MIN_USER_MSGS:
        # Não desperdiça API call; já marca atípica por amostra insuficiente
        _save_analysis(lead_id, {
            "atypical": True,
            "atypical_reason": f"sample insufficient: {convo['user_msg_count']} user msgs",
            "outcome": "in_progress",
            "outcome_confidence": 0.0,
        }, raw="(skipped — short conversation)")
        return {"lead_id": lead_id, "atypical": True, "reason": "short_conversation"}

    phone = convo["lead_meta"].get("phone")
    payment = get_payment_for_phone(phone) if phone else None

    user_prompt = _build_user_prompt(convo, payment)
    skill = _sales_analyst_prompt()

    raw = call_ai(
        [{"role": "user", "content": user_prompt}],
        max_tokens=ANALYZER_MAX_TOKENS,
        system=skill,
        timeout=60,
        response_format={"type": "json_object"},
    )

    try:
        parsed = _parse_skill_output(raw)
    except Exception as e:
        _save_failure(lead_id, f"parse_error: {e}", raw)
        return {"lead_id": lead_id, "error": "parse_error", "raw_first_chars": raw[:120]}

    _save_analysis(lead_id, parsed, raw)
    return {
        "lead_id":         lead_id,
        "atypical":        bool(parsed.get("atypical")),
        "outcome":         parsed.get("outcome"),
        "outcome_conf":    parsed.get("outcome_confidence"),
        "profile_slug":    (parsed.get("profile_suggestion") or {}).get("slug"),
    }


def analyze_period(since_ts: int, force: bool = False, only_lead: str = None):
    if only_lead:
        leads = [only_lead]
    else:
        leads = _leads_with_activity_since(since_ts)

    print(f"[analyzer] {len(leads)} leads para processar (since={datetime.fromtimestamp(since_ts).isoformat()})")
    summary = {"total": len(leads), "analyzed": 0, "skipped": 0, "atypical": 0, "errors": 0}

    for i, lid in enumerate(leads, 1):
        try:
            res = analyze_one(lid, force=force)
            if "skipped" in res:
                summary["skipped"] += 1
            elif "error" in res:
                summary["errors"] += 1
            else:
                summary["analyzed"] += 1
                if res.get("atypical"):
                    summary["atypical"] += 1
            print(f"  [{i}/{len(leads)}] {lid}: {res}")
        except Exception as e:
            summary["errors"] += 1
            print(f"  [{i}/{len(leads)}] {lid}: EXCEPTION {e}")
            traceback.print_exc()

    print(f"[analyzer] resumo: {summary}")
    return summary


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours",     type=int, default=24, help="horas pra trás (default 24)")
    parser.add_argument("--since",     type=str, help="ISO date YYYY-MM-DD (override --hours)")
    parser.add_argument("--lead",      type=str, help="analisa só esse lead_id")
    parser.add_argument("--reanalyze", action="store_true", help="força reanálise mesmo se já analisado hoje")
    args = parser.parse_args()

    if args.since:
        since_ts = int(datetime.fromisoformat(args.since).timestamp())
    else:
        since_ts = int(time.time()) - args.hours * 3600

    analyze_period(since_ts, force=args.reanalyze, only_lead=args.lead)


if __name__ == "__main__":
    main()
