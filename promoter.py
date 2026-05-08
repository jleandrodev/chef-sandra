#!/usr/bin/env python3
"""
promoter.py — Fase 5 do sistema de aprendizado.

Responsável por aplicar mudanças no playbook/profiles a partir das
`proposals` registradas pelo `reducer.py`. Aplica os portões anti-deriva:

- Portão 1 (thresholds):    promove quando occ/dias batem o mínimo
- Portão 2 (rollback WoW):  reverte mudança se métrica primária cair >=15%
                            ou secundária cair >=25% (semana corrente vs anterior)
- Portão 3 (hard rules):    bloqueia propostas que toquem regras imutáveis
                            (preços, persona, idioma, fluxo PASO 1-8)
- Portão 4 (atypical):      já aplicado upstream pelo analyzer
- Portão 6 (versionamento): toda mudança gera entrada em playbook_versions
                            com diff e baseline de métrica — permite rollback

Uso:
    python3 promoter.py                  # ciclo completo (rollback + promoção)
    python3 promoter.py --dry-run        # mostra o que faria sem aplicar
    python3 promoter.py --rollback <ver> # reverte versão específica
    python3 promoter.py --status         # lista versões e propostas ativas
"""

import argparse
import difflib
import json
import re
import shutil
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from agent import _db, _read_file, _KNOWLEDGE_DIR


# ── Configuração — thresholds (calibrados pra ~25-30 leads/dia) ───────────────

THRESHOLDS = {
    "refine_profile":   {"occ": 4,  "days": 2},
    "new_profile":      {"occ": 8,  "days": 4},
    "new_rebuttal":     {"occ": 4,  "days": 2},
    "update_playbook":  {"occ": 12, "days": 5},
}

ROLLBACK_PRIMARY_DROP   = 0.15   # 15% queda relativa no link_sent_rate
ROLLBACK_SECONDARY_DROP = 0.25   # 25% queda relativa no payment_rate
STAGING_DAYS            = 7      # janela de observação antes de promover de staging→promoted
REPROMOTE_BLOCK_DAYS    = 14     # bloqueio de repromoção após rollback


# ── Hard rules — alvos imutáveis ──────────────────────────────────────────────

HARD_RULE_PATTERNS = [
    # Preços — não pode rebaixar/subir tier (todos os 4 valores apresentados juntos em PASO 5)
    re.compile(r"\$\s*5\.00\b"),       # PASO 5 — menor valor
    re.compile(r"\$\s*6\.90\b"),       # PASO 5 — valor básico
    re.compile(r"\$\s*9\.90\b"),       # PASO 5 — valor padrão
    re.compile(r"\$\s*12\.90\b"),      # PASO 5 — valor especial
    # Markers de mídia — não pode renomear (acoplado ao watcher)
    re.compile(r"\[\[ENVIAR_LIBROS\]\]"),
    re.compile(r"\[\[ENVIAR_PRUEBA_DEFAULT\]\]"),
    re.compile(r"\[\[ENVIAR_PRUEBA_OBJECION\]\]"),
    # Persona
    re.compile(r"\bChef\s+Sandra\b"),
    # Idioma
    re.compile(r"\b(en\s+español|responder\s+en\s+ingl[eé]s|en\s+portugu[eé]s)\b", re.I),
    # Fluxo PASO 1-8 (qualquer reescrita explícita)
    re.compile(r"\bPASO\s*[1-8]\s*[—:-]", re.I),
]


def _violates_hard_rules(body: str, p_type: str, target: str) -> Optional[str]:
    """Retorna motivo da violação se a proposta tenta sobrescrever regra
    imutável; None se OK. Liberal: só bloqueia se o body sugere REESCREVER
    o conteúdo da regra, não se apenas referencia.

    Heurística: se body contém os padrões hard-rule E ele é do tipo
    update_playbook ou afeta core, bloqueia."""
    body_lc = (body or "").lower()

    # Bloqueio absoluto: qualquer proposta de mexer em core_rules
    if "core_rules" in (target or "").lower():
        return "alvo é core_rules (imutável)"

    # update_playbook que tenta redefinir preços ou fluxo
    if p_type == "update_playbook":
        if re.search(r"(reduzir|baixar|subir|alterar|trocar)\s+(o\s+)?(precio|preço|valor)", body_lc):
            return "tenta alterar tier de preço (imutável)"
        for pat in HARD_RULE_PATTERNS[5:]:  # idioma, fluxo PASO
            if pat.search(body or ""):
                # só bloqueia se o body está propondo MUDAR esse aspecto
                if any(k in body_lc for k in ("reescrever", "alterar", "mudar", "trocar")):
                    return f"tenta alterar regra imutável: {pat.pattern}"

    return None


# ── Métricas WoW ──────────────────────────────────────────────────────────────

def _metrics_window(days_ago_start: int, days_ago_end: int) -> dict:
    """Soma daily_metrics num janela [today - start, today - end]."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    win_start = (today - timedelta(days=days_ago_start)).strftime("%Y-%m-%d")
    win_end   = (today - timedelta(days=days_ago_end)).strftime("%Y-%m-%d")
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT COALESCE(SUM(new_leads),0)         AS new_leads,
                  COALESCE(SUM(link_sent),0)         AS link_sent,
                  COALESCE(SUM(payment_confirmed),0) AS payment_confirmed
           FROM daily_metrics
           WHERE date >= ? AND date < ?""",
        (win_start, win_end)
    )
    row = c.fetchone()
    conn.close()
    new_leads = row["new_leads"] or 0
    link_sent = row["link_sent"] or 0
    paid      = row["payment_confirmed"] or 0
    return {
        "window":          f"{win_start}..{win_end}",
        "new_leads":       new_leads,
        "link_sent":       link_sent,
        "payment_confirmed": paid,
        "link_sent_rate":  (link_sent / new_leads) if new_leads else 0.0,
        "payment_rate":    (paid / link_sent)     if link_sent else 0.0,
    }


def _wow_health() -> dict:
    """Compara semana corrente (1..7d atrás) vs semana anterior (8..14d atrás)."""
    cur  = _metrics_window(7, 0)
    prev = _metrics_window(14, 7)

    def _drop_pct(now, before):
        if before <= 0:
            return 0.0
        return max(0.0, (before - now) / before)

    primary_drop   = _drop_pct(cur["link_sent_rate"], prev["link_sent_rate"])
    secondary_drop = _drop_pct(cur["payment_rate"],   prev["payment_rate"])

    return {
        "current":         cur,
        "previous":        prev,
        "primary_drop":    primary_drop,
        "secondary_drop":  secondary_drop,
        "trigger_rollback": (
            primary_drop   >= ROLLBACK_PRIMARY_DROP   or
            secondary_drop >= ROLLBACK_SECONDARY_DROP
        ),
        "thresholds": {
            "primary":   ROLLBACK_PRIMARY_DROP,
            "secondary": ROLLBACK_SECONDARY_DROP,
        },
    }


# ── Versionamento ─────────────────────────────────────────────────────────────

def _new_version_id() -> str:
    return f"v{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _save_version(version: str, change_type: str, target: str,
                  proposal_id: Optional[int], diff: str,
                  metric_baseline: dict, note: str = ""):
    conn = _db()
    c = conn.cursor()
    c.execute(
        """INSERT OR REPLACE INTO playbook_versions
           (version, created_at, change_type, target, proposal_id, diff,
            metric_baseline, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (version, int(time.time()), change_type, target, proposal_id, diff,
         json.dumps(metric_baseline, ensure_ascii=False), note)
    )
    conn.commit()
    conn.close()


def _diff(old: str, new: str, fname: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"{fname} (before)",
        tofile=f"{fname} (after)",
        lineterm="\n",
    ))


def _save_snapshot(target_path: Path, version: str):
    """Copia arquivo atual pra knowledge/playbook.versions/<version>/<filename>."""
    snap_dir = _KNOWLEDGE_DIR / "playbook.versions" / version
    snap_dir.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        shutil.copy2(target_path, snap_dir / target_path.name)
    else:
        # marcador de "arquivo não existia" pra rollback restaurar inexistência
        (snap_dir / f"{target_path.name}.absent").write_text("")


def _restore_snapshot(target_path: Path, version: str):
    snap_dir = _KNOWLEDGE_DIR / "playbook.versions" / version
    snap_file = snap_dir / target_path.name
    absent_marker = snap_dir / f"{target_path.name}.absent"
    if absent_marker.exists():
        if target_path.exists():
            target_path.unlink()
        return True
    if snap_file.exists():
        shutil.copy2(snap_file, target_path)
        return True
    return False


# ── Aplicação física das mudanças ─────────────────────────────────────────────

def _profile_path(slug: str) -> Path:
    return _KNOWLEDGE_DIR / "profiles" / f"{slug}.md"


def _playbook_path() -> Path:
    return _KNOWLEDGE_DIR / "playbook.md"


def _apply_profile(slug: str, body: str, version: str) -> str:
    p = _profile_path(slug)
    old = _read_file(p) if p.exists() else ""
    _save_snapshot(p, version)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return _diff(old, body, p.name)


def _apply_rebuttal(category: str, body: str, version: str) -> str:
    """Adiciona uma linha em MANEJO DE OBJECIONES no playbook.md."""
    pb = _playbook_path()
    old = _read_file(pb)
    _save_snapshot(pb, version)
    line = f"- [{category}] {body.strip()}"
    if line in old:
        return ""  # já presente
    if "MANEJO DE OBJECIONES" in old:
        new = old.rstrip() + "\n" + line + "\n"
    else:
        new = old.rstrip() + "\n\n" + line + "\n"
    pb.write_text(new, encoding="utf-8")
    return _diff(old, new, pb.name)


def _apply_update_playbook(target: str, body: str, version: str) -> str:
    """Adiciona/atualiza seção 'FRASES QUE FUNCIONAN' no playbook."""
    pb = _playbook_path()
    old = _read_file(pb)
    _save_snapshot(pb, version)

    section_header = "════════════════════════════════════════\nFRASES QUE FUNCIONAN\n════════════════════════════════════════"
    entry_id = target.split(":", 1)[-1][:80]
    parsed = body
    try:
        parsed_obj = json.loads(body)
        phrase     = parsed_obj.get("agent_phrase_pattern") or entry_id
        principle  = parsed_obj.get("principle", "")
        line = f"- {phrase}  _(princípio: {principle})_"
    except Exception:
        line = f"- {entry_id}: {parsed[:200]}"

    if section_header in old:
        # já tem seção: appende
        if line in old:
            return ""
        new = old.rstrip() + "\n" + line + "\n"
    else:
        new = old.rstrip() + "\n\n" + section_header + "\n" + line + "\n"
    pb.write_text(new, encoding="utf-8")
    return _diff(old, new, pb.name)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_proposal(pid: int) -> dict:
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT * FROM proposals WHERE id = ?", (pid,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _list_pending() -> list:
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM proposals WHERE status = 'pending' ORDER BY occurrences DESC"
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def _list_staging() -> list:
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT * FROM proposals WHERE status = 'staging' ORDER BY promoted_at ASC"
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def _last_promoted_version() -> Optional[dict]:
    """Retorna a última versão aplicada que ainda esteja ativa (não rollbacked)."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT v.*
           FROM playbook_versions v
           WHERE v.change_type IN ('promote_staging','promote')
           ORDER BY v.created_at DESC LIMIT 1"""
    )
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _set_proposal_status(pid: int, status: str, version: str = None,
                         reason: str = None):
    conn = _db()
    c = conn.cursor()
    fields = ["status = ?"]
    args   = [status]
    if status == "staging":
        fields.append("promoted_at = ?")
        args.append(int(time.time()))
        if version:
            fields.append("promoted_version = ?")
            args.append(version)
    if status == "rolled_back":
        fields.append("rolled_back_at = ?")
        args.append(int(time.time()))
        if reason:
            fields.append("rolled_back_reason = ?")
            args.append(reason)
    if status in ("promoted", "rejected"):
        fields.append("decided_at = ?")
        args.append(int(time.time()))
        if reason:
            fields.append("decided_reason = ?")
            args.append(reason)
    args.append(pid)
    c.execute(f"UPDATE proposals SET {', '.join(fields)} WHERE id = ?", args)
    conn.commit()
    conn.close()


def _meets_threshold(p: dict) -> bool:
    t = THRESHOLDS.get(p["type"])
    if not t:
        return False
    return p["occurrences"] >= t["occ"] and p["distinct_days"] >= t["days"]


def _is_blocked_by_recent_rollback(p: dict) -> bool:
    """Bloqueia repromoção da mesma (type, target) por REPROMOTE_BLOCK_DAYS."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        """SELECT MAX(rolled_back_at) AS last_rb FROM proposals
           WHERE type = ? AND target = ? AND status = 'rolled_back'""",
        (p["type"], p["target"])
    )
    row = c.fetchone()
    conn.close()
    last_rb = (row["last_rb"] if row else None) or 0
    if last_rb == 0:
        return False
    return (time.time() - last_rb) < (REPROMOTE_BLOCK_DAYS * 86400)


def promote(p: dict, dry_run: bool = False) -> dict:
    """Aplica fisicamente a proposta. Retorna dict com versão, diff, status."""
    # Gate 3: hard rules
    violation = _violates_hard_rules(p["body"], p["type"], p["target"])
    if violation:
        if not dry_run:
            _set_proposal_status(p["id"], "rejected", reason=f"hard_rule: {violation}")
        return {"action": "rejected", "reason": f"hard_rule: {violation}"}

    # Gate 1: thresholds (já checado antes de chamar; redundância é OK)
    if not _meets_threshold(p):
        return {"action": "skip", "reason": "threshold not met"}

    # Anti-bombardeio: bloqueio pós-rollback
    if _is_blocked_by_recent_rollback(p):
        if not dry_run:
            _set_proposal_status(p["id"], "rejected",
                                 reason="repromotion blocked (recent rollback)")
        return {"action": "rejected", "reason": "recent rollback block"}

    version  = _new_version_id()
    baseline = _wow_health()["current"]
    diff_txt = ""

    if dry_run:
        return {"action": "would_promote", "version": version,
                "type": p["type"], "target": p["target"]}

    if   p["type"] in ("new_profile", "refine_profile"):
        diff_txt = _apply_profile(p["target"], p["body"], version)
    elif p["type"] == "new_rebuttal":
        diff_txt = _apply_rebuttal(p["target"], p["body"], version)
    elif p["type"] == "update_playbook":
        diff_txt = _apply_update_playbook(p["target"], p["body"], version)
    else:
        return {"action": "rejected", "reason": f"unknown type: {p['type']}"}

    _save_version(version, change_type="promote_staging",
                  target=p["target"], proposal_id=p["id"],
                  diff=diff_txt, metric_baseline=baseline,
                  note=f"{p['type']}: occ={p['occurrences']} days={p['distinct_days']}")
    _set_proposal_status(p["id"], "staging", version=version)

    return {"action": "promoted_staging", "version": version,
            "type": p["type"], "target": p["target"],
            "occ": p["occurrences"], "days": p["distinct_days"]}


def confirm_staging() -> list:
    """Para cada proposta em staging há ≥STAGING_DAYS dias e métrica saudável,
    promove de 'staging' → 'promoted'. Mudança física já foi aplicada — aqui
    só consolida o status."""
    out = []
    cutoff = int(time.time()) - STAGING_DAYS * 86400
    health = _wow_health()
    if health["trigger_rollback"]:
        return [{"action": "skip_confirm", "reason": "wow_unhealthy"}]

    for p in _list_staging():
        if (p["promoted_at"] or 0) > cutoff:
            continue  # ainda em janela de observação
        _set_proposal_status(p["id"], "promoted",
                             reason=f"staging completed clean (>={STAGING_DAYS}d)")
        out.append({"action": "confirmed", "id": p["id"], "type": p["type"],
                    "target": p["target"], "version": p["promoted_version"]})
    return out


def auto_rollback() -> Optional[dict]:
    """Se WoW indica deriva, reverte a versão mais recente em staging."""
    health = _wow_health()
    if not health["trigger_rollback"]:
        return None

    last = _last_promoted_version()
    if not last:
        return {"action": "skip_rollback", "reason": "no version to roll back"}

    return _do_rollback(last["version"], reason="wow_drift_auto",
                        health=health)


def _do_rollback(version: str, reason: str, health: dict = None) -> dict:
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT * FROM playbook_versions WHERE version = ?", (version,))
    row = c.fetchone()
    if not row:
        conn.close()
        return {"action": "error", "reason": f"version {version} not found"}
    target = row["target"]
    proposal_id = row["proposal_id"]
    conn.close()

    # restaura snapshot
    if target and "/" not in target and target != "playbook":
        path = _profile_path(target) if (_KNOWLEDGE_DIR / "profiles" / f"{target}.md").exists() else _playbook_path()
    else:
        path = _playbook_path()
    # tentativa robusta: restaura ambos se houver snapshot
    restored = []
    for fname in ("playbook.md", f"{target}.md"):
        candidate = (_KNOWLEDGE_DIR / "playbook.versions" / version / fname)
        absent    = (_KNOWLEDGE_DIR / "playbook.versions" / version / f"{fname}.absent")
        if candidate.exists() or absent.exists():
            target_path = (_KNOWLEDGE_DIR / "profiles" / fname) if fname != "playbook.md" else _playbook_path()
            if _restore_snapshot(target_path, version):
                restored.append(target_path.name)

    new_version = _new_version_id()
    _save_version(new_version, change_type="rollback",
                  target=target, proposal_id=proposal_id,
                  diff=f"reverted from version {version}",
                  metric_baseline=(health or _wow_health()),
                  note=f"reason={reason}; restored={restored}")

    if proposal_id:
        _set_proposal_status(proposal_id, "rolled_back",
                             reason=f"{reason} (orig version: {version})")
    return {"action": "rolled_back", "version": version,
            "new_version": new_version, "restored": restored, "reason": reason}


def run_cycle(dry_run: bool = False) -> dict:
    out = {"started_at": datetime.now().isoformat(timespec="seconds")}

    # 1) checa rollback automático ANTES de qualquer promoção nova
    rb = auto_rollback() if not dry_run else None
    out["rollback"] = rb

    # 2) confirma staging vencido
    out["confirmed"] = confirm_staging() if not dry_run else []

    # 3) promove pendentes que batem threshold
    pending = _list_pending()
    out["evaluated"] = len(pending)
    out["promotions"] = []
    for p in pending:
        if not _meets_threshold(p):
            out["promotions"].append({
                "id": p["id"], "type": p["type"], "target": p["target"],
                "action": "skip", "reason": "threshold not met",
                "occ": p["occurrences"], "days": p["distinct_days"],
            })
            continue
        out["promotions"].append({"id": p["id"], **promote(p, dry_run=dry_run)})

    out["wow_health"] = _wow_health()
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────

def status_dump():
    health = _wow_health()
    print("=== WoW Health ===")
    print(json.dumps(health, ensure_ascii=False, indent=2))

    conn = _db()
    c = conn.cursor()
    print("\n=== Proposals (top 20 por status/occurrences) ===")
    for r in c.execute(
        "SELECT id, status, type, target, occurrences, distinct_days, "
        "first_seen_date, last_seen_date FROM proposals "
        "ORDER BY status, occurrences DESC LIMIT 20"
    ):
        d = dict(r)
        print(f"  #{d['id']:3} [{d['status']:11}] {d['type']:18} {d['target']:40} "
              f"occ={d['occurrences']:3} days={d['distinct_days']:2}")
    print("\n=== Versions (últimas 10) ===")
    for r in c.execute(
        "SELECT version, change_type, target, datetime(created_at,'unixepoch','localtime') as ts, note "
        "FROM playbook_versions ORDER BY created_at DESC LIMIT 10"
    ):
        d = dict(r)
        print(f"  {d['version']}  {d['change_type']:18}  {d['target']:30}  {d['ts']}  {d['note'][:60]}")
    conn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback", type=str, help="versão a reverter manualmente")
    parser.add_argument("--status",  action="store_true")
    args = parser.parse_args()

    if args.status:
        status_dump()
        return

    if args.rollback:
        res = _do_rollback(args.rollback, reason="manual")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    res = run_cycle(dry_run=args.dry_run)
    print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
