#!/usr/bin/env python3
"""
reducer.py — Fase 4 do sistema de aprendizado.

Pipeline de redução diário:
  1. Lê todas as conversation_analysis com analyzed_at no dia alvo
  2. Calcula métricas brutas (programaticamente, sem LLM)
  3. Faz UMA chamada à skill `daily_reducer.md` pra clusterizar/sintetizar
  4. Persiste:
        - daily_metrics
        - knowledge/analyses/YYYY-MM-DD.md (relatório legível em PT-BR)
        - knowledge/analyses/YYYY-MM-DD.json (output bruto da skill, audit)
        - proposals (insert/incrementa) — sem promover, isso é Fase 5
  5. Imprime resumo no stdout

Uso:
    python3 reducer.py                   # dia corrente
    python3 reducer.py --date 2026-05-04 # dia específico
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from agent import _db, _read_file, _KNOWLEDGE_DIR, call_ai


REDUCER_MAX_TOKENS = 3000


# ── Coleta dos dados do dia ───────────────────────────────────────────────────

def _day_bounds(date_str: str) -> tuple:
    """Retorna (start_ts, end_ts) do dia local."""
    d = datetime.fromisoformat(date_str)
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())


def _analyses_in_day(start_ts: int, end_ts: int) -> list:
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT lead_id, atypical, atypical_reason, outcome, outcome_confidence,
                  profile_suggestion, what_worked, what_failed, objections_seen,
                  evidence_quality, raw_json
           FROM conversation_analysis
           WHERE analyzed_at >= ? AND analyzed_at < ?""",
        (start_ts, end_ts)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def _leads_created_in_day(start_ts: int, end_ts: int) -> int:
    """Conta leads criados (created_at) no dia, para 'new_leads'."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE created_at >= ? AND created_at < ?",
        (datetime.fromtimestamp(start_ts).isoformat(),
         datetime.fromtimestamp(end_ts).isoformat())
    )
    n = c.fetchone()["n"]
    conn.close()
    return n


def _outcome_counts(analyses: list) -> dict:
    counts = Counter()
    for a in analyses:
        if a["atypical"]:
            counts["atypical"] += 1
        else:
            counts[a["outcome"] or "unknown"] += 1
    return dict(counts)


def _avg_messages_per_lead(start_ts: int, end_ts: int) -> float:
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT lead_id, COUNT(*) AS n FROM messages
           WHERE ts >= ? AND ts < ? AND role = 'user'
           GROUP BY lead_id""",
        (start_ts, end_ts)
    )
    rows = c.fetchall()
    conn.close()
    if not rows:
        return 0.0
    return round(sum(r["n"] for r in rows) / len(rows), 2)


def _link_sent_in_day(start_ts: int, end_ts: int) -> int:
    """Aproxima 'link enviado': leads cuja sent_checkout=1 e updated_at no dia."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT COUNT(*) AS n FROM leads
           WHERE sent_checkout = 1 AND updated_at >= ? AND updated_at < ?""",
        (datetime.fromtimestamp(start_ts).isoformat(),
         datetime.fromtimestamp(end_ts).isoformat())
    )
    n = c.fetchone()["n"]
    conn.close()
    return n


def _payment_confirmed_in_day(analyses: list) -> int:
    return sum(1 for a in analyses
               if not a["atypical"] and a.get("outcome") == "paid")


# ── Compactação dos JSONs pra entrar na skill ─────────────────────────────────

def _compact_analyses_for_skill(analyses: list, max_per_lead_chars: int = 1000) -> str:
    out = []
    for a in analyses:
        try:
            ps = json.loads(a["profile_suggestion"]) if a["profile_suggestion"] else None
            ww = json.loads(a["what_worked"])         if a["what_worked"]         else []
            wf = json.loads(a["what_failed"])         if a["what_failed"]         else []
            obj= json.loads(a["objections_seen"])     if a["objections_seen"]     else []
        except json.JSONDecodeError:
            continue

        slim = {
            "lead_id":   a["lead_id"],
            "atypical":  bool(a["atypical"]),
            "outcome":   a.get("outcome"),
            "conf":      a.get("outcome_confidence"),
            "profile":   ps,
            "worked":    ww,
            "failed":    wf,
            "objections": obj,
        }
        s = json.dumps(slim, ensure_ascii=False)
        if len(s) > max_per_lead_chars:
            # truncate cleanly — drop deepest fields
            slim["worked"]    = slim["worked"][:2]
            slim["failed"]    = slim["failed"][:2]
            slim["objections"]= slim["objections"][:3]
            s = json.dumps(slim, ensure_ascii=False)
        out.append(s)
    return "[\n  " + ",\n  ".join(out) + "\n]"


def _list_active_profiles_md() -> str:
    profiles_dir = _KNOWLEDGE_DIR / "profiles"
    if not profiles_dir.exists():
        return "(nenhum)"
    items = []
    for p in sorted(profiles_dir.glob("*.md")):
        if p.name.startswith("_"):
            continue
        head = p.read_text(encoding="utf-8").splitlines()[:6]
        items.append(f"- **{p.stem}**\n  " + "\n  ".join(head))
    return "\n".join(items) if items else "(nenhum)"


def _pending_proposals_summary() -> str:
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT type, target, occurrences, distinct_days, status
           FROM proposals
           WHERE status IN ('pending','staging')
           ORDER BY occurrences DESC LIMIT 50"""
    )
    rows = c.fetchall()
    conn.close()
    if not rows:
        return "(nenhuma)"
    return "\n".join(
        f"- [{r['status']}] {r['type']} :: {r['target']} (occ={r['occurrences']}, dias={r['distinct_days']})"
        for r in rows
    )


# ── Persistência ──────────────────────────────────────────────────────────────

def _save_daily_metrics(date_str: str, metrics: dict):
    conn = _db()
    c = conn.cursor()
    c.execute(
        """INSERT OR REPLACE INTO daily_metrics
           (date, new_leads, link_sent, payment_confirmed, atypical_count,
            avg_messages_per_lead, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            date_str,
            metrics.get("new_leads",         0),
            metrics.get("link_sent",         0),
            metrics.get("payment_confirmed", 0),
            metrics.get("atypical_count",    0),
            metrics.get("avg_messages_per_lead", 0.0),
            int(time.time()),
        )
    )
    conn.commit()
    conn.close()


def _upsert_proposal(p_type: str, target: str, body: str,
                     evidence_lead_ids: list, today: str):
    """Insert ou incrementa proposta. Adiciona dias distintos."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT id, evidence_lead_ids, occurrences, first_seen_date, last_seen_date, "
        "distinct_days, status FROM proposals WHERE type = ? AND target = ?",
        (p_type, target)
    )
    row = c.fetchone()
    if row is None:
        c.execute(
            """INSERT INTO proposals
               (type, target, body, evidence_lead_ids, occurrences,
                distinct_days, first_seen_date, last_seen_date, status)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'pending')""",
            (p_type, target, body, json.dumps(evidence_lead_ids, ensure_ascii=False),
             len(evidence_lead_ids), today, today)
        )
    else:
        prev_ids   = set(json.loads(row["evidence_lead_ids"] or "[]"))
        new_ids    = prev_ids.union(evidence_lead_ids)
        first_date = row["first_seen_date"]
        last_date  = row["last_seen_date"]
        # distinct_days: incrementa só se today > last_date
        distinct_days = row["distinct_days"]
        if today != last_date:
            distinct_days += 1
        c.execute(
            """UPDATE proposals
               SET body = ?, evidence_lead_ids = ?, occurrences = ?,
                   distinct_days = ?, last_seen_date = ?
               WHERE id = ?""",
            (body, json.dumps(sorted(new_ids), ensure_ascii=False),
             len(new_ids), distinct_days, today, row["id"])
        )
    conn.commit()
    conn.close()


# Stopwords PT/ES para tokens de slug. Não são discriminantes — removê-las
# evita que "diabetico-em-busca-de-receitas" e "diabetico-que-busca-receitas"
# fiquem com Jaccard 0.5 quando são semanticamente o mesmo cluster.
_SLUG_STOPWORDS = {
    "de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas",
    "que", "para", "por", "com", "sem", "e", "ou", "a", "o", "as", "os",
    "um", "uma", "uns", "umas", "del", "la", "el", "los", "las", "y",
}

_SLUG_MATCH_THRESHOLD = 0.6  # Jaccard mínimo para considerar dois slugs equivalentes


def _slug_tokens(slug: str) -> set:
    return {t for t in (slug or "").lower().split("-") if t and t not in _SLUG_STOPWORDS}


def _slug_similarity(a: str, b: str) -> float:
    ta, tb = _slug_tokens(a), _slug_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _active_profile_slugs() -> list:
    """Slugs já em uso: arquivos em profiles/ + propostas ativas (não rejeitadas
    nem revertidas). Retorna lista de slugs únicos preservando precedência:
    profiles existentes > staging/promoted > pending."""
    seen, ordered = set(), []

    profiles_dir = _KNOWLEDGE_DIR / "profiles"
    if profiles_dir.is_dir():
        for f in profiles_dir.glob("*.md"):
            if f.stem == "_index":
                continue
            if f.stem not in seen:
                seen.add(f.stem)
                ordered.append(f.stem)

    conn = _db()
    try:
        c = conn.cursor()
        c.execute(
            """SELECT target FROM proposals
               WHERE type IN ('new_profile', 'refine_profile')
                 AND status IN ('staging', 'promoted', 'pending')
               ORDER BY CASE status
                          WHEN 'promoted' THEN 0
                          WHEN 'staging'  THEN 1
                          ELSE 2 END,
                        last_seen_date DESC"""
        )
        for row in c.fetchall():
            t = row["target"]
            if t and t not in seen:
                seen.add(t)
                ordered.append(t)
    finally:
        conn.close()
    return ordered


def _normalize_profile_slug(proposed_slug: str) -> str:
    """Se o slug proposto pelo LLM tem similaridade ≥ threshold com algum slug
    já ativo, reusa o existente. Caso contrário, mantém o proposto.
    A precedência de _active_profile_slugs() garante que profiles consolidados
    e propostas em staging/promoted vencem propostas pending mais novas."""
    if not proposed_slug:
        return proposed_slug
    best_slug, best_score = proposed_slug, 0.0
    for existing in _active_profile_slugs():
        if existing == proposed_slug:
            return existing
        score = _slug_similarity(proposed_slug, existing)
        if score > best_score:
            best_slug, best_score = existing, score
    if best_score >= _SLUG_MATCH_THRESHOLD:
        print(f"  [normalize] '{proposed_slug}' → '{best_slug}' (jaccard={best_score:.2f})")
        return best_slug
    return proposed_slug


def _ingest_proposals(reducer_output: dict, today: str):
    """Materializa profile_clusters → proposals(type='new_profile' ou 'refine_profile'),
    objection_patterns → proposals(type='new_rebuttal'),
    what_works_repeated → proposals(type='update_playbook')."""

    # Profile clusters
    for cluster in reducer_output.get("profile_clusters", []):
        raw_slug = cluster.get("canonical_slug")
        if not raw_slug:
            continue
        slug   = _normalize_profile_slug(raw_slug)
        body   = cluster.get("draft_profile_md") or ""
        ev_ids = cluster.get("evidence_lead_ids") or []
        # detecta se está refinando perfil existente
        is_refine = (_KNOWLEDGE_DIR / "profiles" / f"{slug}.md").exists()
        p_type = "refine_profile" if is_refine else "new_profile"
        _upsert_proposal(p_type, slug, body, ev_ids, today)

    # Objection patterns
    for obj in reducer_output.get("objection_patterns", []):
        cat = obj.get("category")
        if not cat:
            continue
        # aceita tanto draft_rebuttal_es (novo, correto) quanto _pt (legado/erro de skill)
        body = obj.get("draft_rebuttal_es") or obj.get("draft_rebuttal_pt") or ""
        ev_ids = [e.get("lead_id") for e in (obj.get("evidence") or []) if e.get("lead_id")]
        _upsert_proposal("new_rebuttal", cat, body, ev_ids, today)

    # What works repeated → atualização do playbook
    for ww in reducer_output.get("what_works_repeated", []):
        target_key = "what_works:" + (ww.get("agent_phrase_pattern") or "")[:80]
        ev_ids = [e.get("lead_id") for e in (ww.get("evidence") or []) if e.get("lead_id")]
        _upsert_proposal("update_playbook", target_key,
                         json.dumps(ww, ensure_ascii=False), ev_ids, today)


# ── Geração do relatório legível ──────────────────────────────────────────────

def _render_markdown_report(date_str: str, metrics: dict, reducer_out: dict) -> str:
    out = [f"# Análise diária — {date_str}", ""]
    out.append(f"_Gerado em {datetime.now().isoformat(timespec='seconds')}_")
    out.append("")
    out.append("## Métricas")
    out.append(f"- Leads novos: {metrics['new_leads']}")
    out.append(f"- Links enviados: {metrics['link_sent']}")
    out.append(f"- Pagamentos confirmados (inferidos): {metrics['payment_confirmed']}")
    out.append(f"- Conversas atípicas: {metrics['atypical_count']}")
    out.append(f"- Mensagens médias por lead: {metrics['avg_messages_per_lead']}")
    out.append(f"- Outcomes: {json.dumps(metrics.get('outcome_counts', {}), ensure_ascii=False)}")
    out.append("")
    out.append("## Resumo")
    out.append(reducer_out.get("summary_pt", "_(skill não retornou)_"))
    out.append("")

    clusters = reducer_out.get("profile_clusters") or []
    if clusters:
        out.append("## Perfis sugeridos (clusters)")
        for c in clusters:
            occ = len(c.get("evidence_lead_ids") or [])
            out.append(f"### `{c.get('canonical_slug')}` — {c.get('label')}")
            out.append(f"- Dor central: {c.get('core_pain')}")
            out.append(f"- Evidências: {occ} leads — slugs vistos: {c.get('evidence_slugs_seen')}")
            out.append("")

    objs = reducer_out.get("objection_patterns") or []
    if objs:
        out.append("## Objeções recorrentes")
        for o in objs:
            rebuttal = o.get('draft_rebuttal_es') or o.get('draft_rebuttal_pt') or ''
            out.append(f"- **{o.get('category')}** — {o.get('occurrences')}x — sugerido (ES): \"{rebuttal[:160]}\"")
        out.append("")

    wks = reducer_out.get("what_works_repeated") or []
    if wks:
        out.append("## O que funcionou (repetido)")
        for w in wks:
            out.append(f"- ({w.get('occurrences')}x) **{w.get('agent_phrase_pattern')}** — {w.get('principle')}")
        out.append("")

    fails = reducer_out.get("what_fails_repeated") or []
    if fails:
        out.append("## O que falhou (repetido)")
        for f in fails:
            out.append(f"- ({f.get('occurrences')}x) **{f.get('agent_phrase_pattern')}** — {f.get('hypothesis')}")
        out.append("")

    anom = reducer_out.get("anomalies") or []
    if anom:
        out.append("## Anomalias")
        for a in anom:
            out.append(f"- {a}")
        out.append("")

    return "\n".join(out)


def _strip_json_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    return s


# ── Pipeline ──────────────────────────────────────────────────────────────────

def reduce_day(date_str: str) -> dict:
    start_ts, end_ts = _day_bounds(date_str)
    analyses = _analyses_in_day(start_ts, end_ts)

    new_leads     = _leads_created_in_day(start_ts, end_ts)
    link_sent     = _link_sent_in_day(start_ts, end_ts)
    pay_conf      = _payment_confirmed_in_day(analyses)
    avg_msgs      = _avg_messages_per_lead(start_ts, end_ts)
    outcome_cnts  = _outcome_counts(analyses)
    atyp_count    = sum(1 for a in analyses if a["atypical"])

    metrics = {
        "new_leads":         new_leads,
        "link_sent":         link_sent,
        "payment_confirmed": pay_conf,
        "atypical_count":    atyp_count,
        "avg_messages_per_lead": avg_msgs,
        "outcome_counts":    outcome_cnts,
    }

    if not analyses:
        print(f"[reducer] nenhuma análise no dia {date_str}; gravando métricas zeradas.")
        _save_daily_metrics(date_str, metrics)
        return {"metrics": metrics, "reducer_output": None}

    user_prompt = (
        f"=== DATA ===\n{date_str}\n\n"
        f"=== MÉTRICAS BRUTAS ===\n{json.dumps(metrics, ensure_ascii=False, indent=2)}\n\n"
        f"=== PERFIS ATIVOS ATUAIS ===\n{_list_active_profiles_md()}\n\n"
        f"=== PROPOSTAS PENDENTES (acumuladas) ===\n{_pending_proposals_summary()}\n\n"
        f"=== ANÁLISES INDIVIDUAIS DO DIA ({len(analyses)}) ===\n"
        f"{_compact_analyses_for_skill(analyses)}\n\n"
        f"Sintetize. Responda apenas com o JSON descrito na sua instrução."
    )

    skill = _read_file(_KNOWLEDGE_DIR / "skills" / "daily_reducer.md")

    raw = call_ai(
        [{"role": "user", "content": user_prompt}],
        max_tokens=REDUCER_MAX_TOKENS,
        system=skill,
        timeout=120,                                  # reducer pode demorar
        response_format={"type": "json_object"},      # garante JSON parseável
    )

    try:
        reducer_out = json.loads(_strip_json_fences(raw))
    except Exception as e:
        print(f"[reducer] parse_error: {e}\n--- raw (primeiros 400 chars) ---\n{raw[:400]}")
        # Salva métricas mesmo assim
        _save_daily_metrics(date_str, metrics)
        # Salva raw em arquivo de erro
        err_path = _KNOWLEDGE_DIR / "analyses" / f"{date_str}.error.txt"
        err_path.write_text(raw, encoding="utf-8")
        return {"metrics": metrics, "reducer_output": None, "error": str(e)}

    # Persistência
    _save_daily_metrics(date_str, metrics)
    _ingest_proposals(reducer_out, date_str)

    analyses_dir = _KNOWLEDGE_DIR / "analyses"
    analyses_dir.mkdir(parents=True, exist_ok=True)
    md_path   = analyses_dir / f"{date_str}.md"
    json_path = analyses_dir / f"{date_str}.json"
    md_path.write_text(_render_markdown_report(date_str, metrics, reducer_out), encoding="utf-8")
    json_path.write_text(json.dumps(reducer_out, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[reducer] {date_str}: {len(analyses)} análises consolidadas.")
    print(f"  - métricas: new={new_leads} link={link_sent} paid={pay_conf} atyp={atyp_count}")
    print(f"  - clusters propostos: {len(reducer_out.get('profile_clusters', []))}")
    print(f"  - objeções repetidas: {len(reducer_out.get('objection_patterns', []))}")
    print(f"  - what_works:        {len(reducer_out.get('what_works_repeated', []))}")
    print(f"  - relatório: {md_path}")

    return {"metrics": metrics, "reducer_output": reducer_out, "report_path": str(md_path)}


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, default=None,
                        help="YYYY-MM-DD (default: hoje)")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    reduce_day(date_str)


if __name__ == "__main__":
    main()
