#!/bin/bash
# Orquestrador do ciclo de aprendizado diário.
# Roda 03:00 BRT (06:00 UTC) via cron.
#
# Sequência:
#   1. analyzer.py  — analisa conversas das últimas 24h por chamada per-lead
#   2. reducer.py   — consolida o dia, gera propostas, escreve relatório
#   3. promoter.py  — (Fase 5) aplica thresholds, promove/reverte mudanças

set -euo pipefail

cd /root/chef-sandra

LOG_DIR="/root/chef-sandra/knowledge/analyses/_runs"
mkdir -p "$LOG_DIR"

DATE_BR=$(TZ=America/Sao_Paulo date +%F)
LOG="$LOG_DIR/${DATE_BR}_daily_run.log"

{
  echo "=== daily_run @ $(date -Iseconds) (BRT: $DATE_BR) ==="

  echo "--- analyzer ---"
  python3 analyzer.py --hours 24 || echo "[WARN] analyzer falhou — seguindo"

  echo "--- reducer ---"
  python3 reducer.py --date "$DATE_BR" || echo "[WARN] reducer falhou — seguindo"

  if [ -f "/root/chef-sandra/promoter.py" ]; then
    echo "--- promoter ---"
    python3 promoter.py || echo "[WARN] promoter falhou — seguindo"
  else
    echo "(promoter.py ainda não existe — Fase 5 pendente)"
  fi

  echo "=== fim daily_run @ $(date -Iseconds) ==="
} 2>&1 | tee -a "$LOG"
