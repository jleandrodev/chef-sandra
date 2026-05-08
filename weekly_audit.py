#!/usr/bin/env python3
"""
weekly_audit.py — Fase 6 do sistema de aprendizado.

Gera um relatório semanal em PT-BR no diretório `knowledge/analyses/weekly/`
consolidando:
- Métricas WoW (semana corrente vs anterior)
- Mudanças aplicadas no período (versions criadas, rollbacks)
- Propostas em staging e em pending
- Saúde geral do agente (tendência)

Pensado pra você (dono) ler em 2 minutos toda segunda-feira.

Uso:
    python3 weekly_audit.py            # semana terminada ontem
    python3 weekly_audit.py --week-of 2026-04-27   # semana específica
"""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from agent import _db, _KNOWLEDGE_DIR
from promoter import _wow_health


def _week_bounds(end_date_str: str) -> tuple:
    end = datetime.fromisoformat(end_date_str).replace(hour=0,minute=0,second=0,microsecond=0)
    start = end - timedelta(days=7)
    return start, end


def _versions_in_window(start: datetime, end: datetime) -> list:
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT version, change_type, target, proposal_id, note,
                  datetime(created_at,'unixepoch','localtime') AS ts, metric_baseline
           FROM playbook_versions
           WHERE created_at >= ? AND created_at < ?
           ORDER BY created_at ASC""",
        (int(start.timestamp()), int(end.timestamp()))
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def _proposals_summary() -> dict:
    conn = _db()
    c = conn.cursor()
    out = {}
    for status in ("pending","staging","promoted","rejected","rolled_back"):
        c.execute("SELECT COUNT(*) AS n FROM proposals WHERE status = ?", (status,))
        out[status] = c.fetchone()["n"]
    c.execute(
        """SELECT type, target, occurrences, distinct_days
           FROM proposals WHERE status = 'pending' ORDER BY occurrences DESC LIMIT 10"""
    )
    out["top_pending"] = [dict(r) for r in c.fetchall()]
    c.execute(
        """SELECT type, target, occurrences, distinct_days
           FROM proposals WHERE status = 'staging' ORDER BY promoted_at DESC LIMIT 10"""
    )
    out["staging_list"] = [dict(r) for r in c.fetchall()]
    conn.close()
    return out


def _daily_metrics_window(start: datetime, end: datetime) -> list:
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT * FROM daily_metrics
           WHERE date >= ? AND date < ? ORDER BY date""",
        (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def _atypical_rate(metrics_rows: list) -> float:
    total = sum(r["new_leads"] or 0 for r in metrics_rows)
    atyp  = sum(r["atypical_count"] or 0 for r in metrics_rows)
    return (atyp / total) if total else 0.0


def render(end_date_str: str) -> str:
    start, end = _week_bounds(end_date_str)
    versions = _versions_in_window(start, end)
    proposals_sum = _proposals_summary()
    metrics_rows  = _daily_metrics_window(start, end)
    health        = _wow_health()

    promotions = [v for v in versions if v["change_type"] == "promote_staging"]
    rollbacks  = [v for v in versions if v["change_type"] == "rollback"]

    out = []
    out.append(f"# Audit semanal — {start.strftime('%Y-%m-%d')} a {(end - timedelta(days=1)).strftime('%Y-%m-%d')}")
    out.append("")
    out.append(f"_Gerado em {datetime.now().isoformat(timespec='seconds')}_")
    out.append("")
    out.append("## Saúde geral")
    cur = health["current"]; prev = health["previous"]
    out.append(f"| Métrica | Semana atual | Semana anterior | Δ |")
    out.append(f"|---|---:|---:|---:|")
    def _delta(a, b):
        if b == 0:
            return "—"
        d = (a - b) / b
        return f"{d:+.1%}"
    out.append(f"| Leads novos | {cur['new_leads']} | {prev['new_leads']} | {_delta(cur['new_leads'], prev['new_leads'])} |")
    out.append(f"| Links enviados | {cur['link_sent']} | {prev['link_sent']} | {_delta(cur['link_sent'], prev['link_sent'])} |")
    out.append(f"| Pagamentos confirmados | {cur['payment_confirmed']} | {prev['payment_confirmed']} | {_delta(cur['payment_confirmed'], prev['payment_confirmed'])} |")
    out.append(f"| Taxa link/lead | {cur['link_sent_rate']:.1%} | {prev['link_sent_rate']:.1%} | — |")
    out.append(f"| Taxa pago/link | {cur['payment_rate']:.1%} | {prev['payment_rate']:.1%} | — |")
    out.append("")
    out.append(f"**Trigger de rollback automático:** {'⚠️ SIM (queda detectada)' if health['trigger_rollback'] else '✅ não — métricas saudáveis'}")
    out.append("")
    atyp_rate = _atypical_rate(metrics_rows)
    if atyp_rate > 0.30:
        out.append(f"⚠️ **Taxa de conversas atípicas:** {atyp_rate:.0%} — acima de 30%. Pode indicar tráfego pago fora do ICP, idioma errado, ou bug do agente. Investigar.")
    else:
        out.append(f"Taxa de conversas atípicas: {atyp_rate:.0%}")
    out.append("")

    out.append("## Mudanças aplicadas na semana")
    if promotions:
        out.append("### Promoções (staging)")
        for v in promotions:
            out.append(f"- `{v['version']}` {v['change_type']} — **{v['target']}** ({v['ts']}) — {v['note']}")
    else:
        out.append("- Nenhuma promoção esta semana.")
    out.append("")
    if rollbacks:
        out.append("### Rollbacks")
        for v in rollbacks:
            out.append(f"- `{v['version']}` rollback — **{v['target']}** ({v['ts']}) — {v['note']}")
    else:
        out.append("### Rollbacks\n- Nenhum rollback esta semana.")
    out.append("")

    out.append("## Pipeline de propostas")
    out.append(f"- pending: {proposals_sum['pending']}")
    out.append(f"- staging: {proposals_sum['staging']}")
    out.append(f"- promoted: {proposals_sum['promoted']}")
    out.append(f"- rejected: {proposals_sum['rejected']}")
    out.append(f"- rolled_back: {proposals_sum['rolled_back']}")
    out.append("")
    if proposals_sum["top_pending"]:
        out.append("### Top 10 propostas pendentes (precisando de mais evidência)")
        for p in proposals_sum["top_pending"]:
            out.append(f"- [{p['type']}] **{p['target']}** — occ={p['occurrences']} dias={p['distinct_days']}")
        out.append("")
    if proposals_sum["staging_list"]:
        out.append("### Staging (em janela de observação)")
        for p in proposals_sum["staging_list"]:
            out.append(f"- [{p['type']}] **{p['target']}**")
        out.append("")

    out.append("## Métricas diárias (semana atual)")
    if metrics_rows:
        out.append("| Data | Novos | Links | Pagos | Atip | Msgs/lead |")
        out.append("|---|---:|---:|---:|---:|---:|")
        for r in metrics_rows:
            out.append(f"| {r['date']} | {r['new_leads']} | {r['link_sent']} | {r['payment_confirmed']} | {r['atypical_count']} | {r['avg_messages_per_lead']} |")
    else:
        out.append("(sem dados)")
    out.append("")

    out.append("---")
    out.append("_Audit gerado automaticamente pelo `weekly_audit.py`. Para reverter mudança específica:_")
    out.append("`python3 promoter.py --rollback <versao>`")
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--week-of", type=str, default=None,
                        help="Data ISO de fechamento da semana (default: hoje)")
    args = parser.parse_args()

    end_str = args.week_of or datetime.now().strftime("%Y-%m-%d")
    md = render(end_str)

    out_dir = _KNOWLEDGE_DIR / "analyses" / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"week_ending_{end_str}.md"
    path.write_text(md, encoding="utf-8")
    print(f"[weekly_audit] {path}")
    print()
    print(md)


if __name__ == "__main__":
    main()
