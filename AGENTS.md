# Sistema de aprendizado da Chef Sandra — guia operacional

## Como funciona

A Chef Sandra é um agente de vendas WhatsApp em espanhol. O sistema de
aprendizado **evolui o prompt do agente todos os dias**, com base nas
conversas reais. Ele não retreina o modelo — ele edita arquivos
versionados que entram no system prompt.

```
Cliente WhatsApp
      ↓
[evolution-api]   ← gateway WhatsApp (Node.js, porta 8080) — gerenciado por PM2
      ↓
[watcher.py]      ← polling, transcreve áudio, intercepta sinais
      │            de pós-pagamento, dispara entrega automática.
      │            Gerenciado por PM2 (id=1, name=watcher).
      ↓
[agent.py]        ← handle_message (computa) + commit_response (persiste).
                    Monta prompt em camadas, chama GPT-4o-mini.
      │
      ├─ knowledge/core_rules.md       (imutável: persona, fluxo, preços, REGLAS 1-14)
      ├─ knowledge/playbook.md         (curado: heurísticas validadas)
      ├─ knowledge/profiles/<slug>.md  (perfil detectado pelo classificador)
      └─ base_conhecimento.md          (info do produto)

Diariamente às 03:00 BRT (cron):
  daily_run.sh
    ├─ analyzer.py    — sales_analyst.md analisa cada conversa
    ├─ reducer.py     — daily_reducer.md consolida o dia
    └─ promoter.py    — aplica/reverte mudanças com base em thresholds e WoW

Toda segunda às 08:00 BRT:
  weekly_audit.py    — relatório semanal pra você (humano) ler
```

## Arquivos importantes

```
meu-agente/
├── agent.py                    # agente do WhatsApp (responde clientes)
├── watcher.py                  # polling do WhatsApp via Evolution API
├── analyzer.py                 # FASE 3 — analisa cada conversa do dia
├── reducer.py                  # FASE 4 — consolida o dia, gera propostas
├── promoter.py                 # FASE 5 — aplica/reverte mudanças
├── weekly_audit.py             # FASE 6 — relatório semanal
├── daily_run.sh                # orquestrador do ciclo diário
├── dados.sqlite                # leads, mensagens, análises, propostas, versões
├── base_conhecimento.md        # info do produto (curado por humano)
├── knowledge/
│   ├── core_rules.md           # IMUTÁVEL — persona, REGLAS, FLUJO PASO 1-8
│   ├── playbook.md             # curado — MANEJO DE OBJECIONES e frases que funcionam
│   ├── profiles/
│   │   ├── _index.md           # lista de perfis ativos (gerado)
│   │   └── <slug>.md           # 1 arquivo por perfil — CONTEXTO PT-BR + DIRETIVAS ES
│   ├── skills/
│   │   ├── sales_analyst.md         # skill que analisa cada conversa
│   │   ├── daily_reducer.md         # skill que consolida o dia
│   │   └── profile_classifier.md    # skill que classifica perfil em runtime
│   ├── analyses/
│   │   ├── YYYY-MM-DD.md       # relatório diário (humano-legível)
│   │   ├── YYYY-MM-DD.json     # output bruto da skill (audit)
│   │   ├── _runs/              # logs do cron
│   │   └── weekly/             # relatórios semanais
│   ├── proposals/              # área pra futura curadoria manual
│   └── playbook.versions/      # snapshots pra rollback
└── integrations/
    └── payments.py             # stub do webhook Kiwify/Stripe (TODO)
```

## Tabelas do banco

| Tabela | Função |
|---|---|
| `leads` | leads + perfil detectado + outcome inferido + flags de entrega |
| `messages` | histórico completo de mensagens |
| `sessions` | sessões de owner-mode (modo gerencial) |
| `followups` | follow-up de checkout (2h após link) |
| `price_followups` | follow-up de preço (30min sem resposta) |
| `conversation_analysis` | output da skill `sales_analyst` por lead |
| `proposals` | mudanças sugeridas pelo `reducer` aguardando promoção |
| `daily_metrics` | métricas agregadas por dia |
| `playbook_versions` | histórico de mudanças aplicadas (com diff e baseline) |

Colunas-chave em `leads` (além do trivial id/phone/name):
- `sent_checkout` — algum link de pagamento já foi enviado.
- `books_delivered` / `books_delivered_at` — flag e timestamp de quando os 5 PDFs foram entregues automaticamente. Bloqueia reentrega.
- `image_alert_sent` — dono já foi notificado uma vez sobre imagem inesperada deste lead (sem checkout enviado). Anti-spam.
- `paused` — humano assumiu a conversa; bot não responde.
- `profile_slug` / `profile_confidence` — saída do classificador.

## Portões anti-deriva

| Portão | Onde | O que faz |
|---|---|---|
| 1 — Threshold | promoter | Só promove com ≥N occurrences em ≥M dias distintos |
| 2 — Rollback WoW | promoter | Reverte mudança se métrica cai >15% (link/lead) ou >25% (pago/link) |
| 3 — Hard rules | promoter | Bloqueia mudanças em preço, persona, idioma, fluxo PASO 1-8 |
| 4 — Atypical mark | analyzer | Marca conversas atípicas, não contam pra propostas |
| 5 — Audit semanal | weekly_audit | Você revisa toda segunda; pode reverter manualmente |
| 6 — Versionamento | promoter | Toda mudança tem snapshot — rollback manual a qualquer momento |

## Thresholds calibrados (25-30 leads/dia)

| Tipo de mudança | Occurrences | Dias distintos |
|---|---:|---:|
| Refinar perfil existente | 4 | 2 |
| Criar novo perfil | 8 | 4 |
| Adicionar rebuttal de objeção | 4 | 2 |
| Atualizar playbook geral | 12 | 5 |

## Comandos úteis

```bash
# Status geral
python3 promoter.py --status

# Forçar rodada manual do ciclo diário
./daily_run.sh

# Rodar análise pontual
python3 analyzer.py --lead wa_5511...           # 1 lead específico
python3 analyzer.py --hours 48                  # últimas 48h

# Reduzir dia específico
python3 reducer.py --date 2026-05-04

# Ver o que o promoter faria sem aplicar
python3 promoter.py --dry-run

# Reverter versão específica
python3 promoter.py --rollback v20260504T182434_449700

# Audit semanal manual
python3 weekly_audit.py

# Ver versões aplicadas
sqlite3 dados.sqlite "SELECT version, change_type, target, datetime(created_at,'unixepoch','localtime') FROM playbook_versions ORDER BY created_at DESC LIMIT 20;"
```

## Editando manualmente

Você (humano) pode editar a qualquer momento:

- `knowledge/core_rules.md` — regras imutáveis (preços, persona, fluxo). Edite com cuidado.
- `knowledge/playbook.md` — heurísticas. Pode editar; o promoter vai apender em cima.
- `knowledge/profiles/<slug>.md` — perfil específico. Edite se quiser refinar à mão.
- `knowledge/skills/*.md` — comportamento das skills (como elas raciocinam). Edite se quiser mudar a forma como o sistema aprende.
- `base_conhecimento.md` — info do produto.

**Não precisa reiniciar o watcher** — `agent.py` lê os arquivos a cada
chamada de IA. A próxima mensagem do lead já vai com o prompt novo.

## Stub do webhook de pagamento

Sem ground truth de pagamento, o `outcome` cai 100% na inferência da
skill (com viés). Quando ligar Kiwify/Stripe, ler
`integrations/payments.py` — já tem o passo a passo completo.

## Fluxo pós-pagamento (entrega automática + trust model)

A partir do momento em que o lead dá QUALQUER sinal de que pagou ou está
esperando os arquivos, o sistema entrega os 5 PDFs (`conteudo/libro1.pdf`
… `libro5.pdf`) **automaticamente** e bypassa o LLM no turno. Decisão de
produto: confiamos na palavra do cliente — nunca pedimos comprovante.

**Sinais detectados (em `watcher.py`, ANTES de chamar `handle_message`)**:

| Trigger | Detector | Gate adicional |
|---|---|---|
| `payment_text` | `is_payment_confirmation(text)` — keywords explícitas ("ya pagué", "ya compré", etc.) | nenhum — confiamos sempre |
| `awaiting_files` | `is_awaiting_files(text)` — pedidos do tipo "envíame los libros", "no me llegó nada" | só dispara se `_checkout_already_sent(lead_id)` |
| `image_proof` | `extract_message_data` setou `is_image=True` | só dispara se `_checkout_already_sent(lead_id)` |

Se algum trigger casar **e** `books_already_delivered(lead_id) == False`,
roda `deliver_purchase()`:

1. Manda mensagem de agradecimento calorosa em ES (com primeiro nome via `_safe_first_name`).
2. Envia os 5 PDFs com retry por arquivo (`_send_pdf_with_retry`, 3 tentativas, backoff 2s/4s).
3. Marca `books_delivered=1` no banco.
4. Notifica o dono via WhatsApp:
   - `notify_owner_sale` se todos os PDFs subiram.
   - `notify_owner_sale_partial_failure` se algum PDF falhou após retries — lista os arquivos pra envio manual.

**O LLM é totalmente bypassado nesse turno** — `handle_message` não roda. Isso evita revenda, pedido de comprovante, ou qualquer comportamento off-script da IA.

**Imagem sem checkout enviado**: o lead nunca recebeu link mas mandou foto.
- Se `image_alert_sent=0` → manda `notify_owner_unexpected_image` (uma única vez por lead).
- Se já enviado → ignora silenciosamente.

**Imagem em geral**: o agente NÃO lê imagens. O modelo `gpt-4o-mini`
suporta visão, mas o pipeline não baixa nem envia o conteúdo. A imagem
serve como sinal binário (mandou ou não).

**Regras 13 e 14 do prompt** (`knowledge/core_rules.md`) são fallback —
se o detector falhar e o turno chegar no LLM, a regra 13 manda agradecer
sem prometer envios e sem revender. A regra 14 manda pedir desculpas e
reformular quando a Sandra realmente não entender a mensagem.

## Resiliência: retry + commit pós-envio

Histórico antigo: `agent.py` gravava `assistant` no banco antes do envio
para o WhatsApp. Se a Evolution API desse timeout, o cliente nunca
recebia mas o histórico tinha a resposta — turnos seguintes ficavam fora
de sincronia.

Arquitetura atual:

- `handle_message(phone, name, text)` — somente computa a resposta. Não grava `assistant`, não chama `mark_checkout_sent`, não agenda followups.
- `commit_response(phone, name, response)` (em `agent.py`) — chamada pelo watcher **depois** de `dispatch_response` confirmar sucesso. É quem grava `assistant` no histórico, marca checkout como enviado e agenda followups.
- `send_whatsapp(phone, message, attempts=3)` — retry com backoff 2s/4s. `attempts=1` reservado pra alertas internos ao dono (evita loop quando a Evolution está totalmente fora).
- `dispatch_response` retorna `False` se qualquer sub-envio (texto ou mídia) falhar. Watcher dispara `notify_owner_send_failure` com preview da mensagem que ficou pendente.

Resultado: se a Evolution API morrer, o lead pode reenviar ou esperar; o histórico não fica fantasma; o dono é avisado por WhatsApp e responde manual enquanto a API não volta.

## Orquestração de processos (PM2)

O `watcher.py` e a `evolution-api` rodam sob **PM2** (`/root/.nvm/versions/node/v24.14.0/bin/pm2`).
Não use `kill` direto — o PM2 reinicia automaticamente. Use os comandos do PM2:

```bash
export PATH=/root/.nvm/versions/node/v24.14.0/bin:$PATH

pm2 list                   # status de tudo
pm2 logs watcher           # logs em tempo real do watcher
pm2 logs watcher --lines 200
pm2 restart watcher        # restart limpo (use após mexer em agent.py / watcher.py)
pm2 stop watcher           # para sem reiniciar
pm2 start watcher          # sobe de novo
pm2 describe watcher       # detalhes do processo (uptime, restarts, paths)
```

**Restart count alto?** Cada modificação em `agent.py` ou `watcher.py`
exige `pm2 restart watcher` pra entrar em vigor. Arquivos em
`knowledge/*.md` são lidos a cada turno, **não precisam restart**.

**Histórico**: havia também um `chef-sandra.service` em `/etc/systemd/system/`
(`Restart=always`). Foi desabilitado em 2026-05-06 porque conflitava com
o PM2 (ambos tentavam subir o mesmo `watcher.py`, brigando por arquivo
de log e gerando exit 209/STDOUT em loop). O arquivo continua existindo
no disco mas com `WantedBy=multi-user.target` removido — pode ser
reabilitado com `systemctl enable chef-sandra.service` se algum dia o
PM2 sair de cena. **Não rode os dois ao mesmo tempo.**

## Notificações que o dono recebe (via WhatsApp em `OWNER_PHONE`)

| Evento | Função | Quando |
|---|---|---|
| 🎉 VENDA REALIZADA | `notify_owner_sale` | `deliver_purchase` enviou os 5 PDFs com sucesso |
| ⚠️ VENDA com falha parcial | `notify_owner_sale_partial_failure` | Algum PDF falhou após retries — lista o que precisa ir manual |
| 🖼️ Imagem sem checkout | `notify_owner_unexpected_image` | Lead mandou imagem mas nunca recebeu link de pagamento (1x por lead) |
| ⚠️ Falha ao responder lead | `notify_owner_send_failure` | `send_whatsapp` falhou em todos os retries — preview da mensagem incluído |

## Sanitização de nomes

Duas fontes possíveis de nome do lead:

1. **`_extract_name_from_history(lead_id)`** em `agent.py` — fonte primária
   pra qualquer mensagem fixa (followup, deliver_purchase, intercept
   pós-entrega). Procura no histórico a primeira mensagem da Sandra que
   pergunta o nome ("¿Cómo te llamas?" e variações via `_NAME_QUESTION_RE`)
   e pega a próxima resposta do user. Tira prefixos comuns ("Me llamo X",
   "Soy X" via `_NAME_INTRO_RE`), capitaliza primeira letra e sanitiza
   com `_safe_first_name`. **Se não achar → "" → saudação neutra ("Hola").**
   Caso real que motivou: lead com `pushName="Traducir Al Español"`
   (configuração de tradutor automático no perfil do WhatsApp) — o
   pushName sozinho fazia a Sandra mandar "Hola Traducir 😊".
2. **`_safe_first_name(raw)`** em `agent.py` — sanitizador de pushName
   (rejeita emails `@`, dígitos `helgase1956`, lowercase
   `bettyvillanuevapardo`, blocklist `compañeros`/`familia`/`Lead`/etc.).
   Continua sendo usado em `strip_placeholders` (pra substituir
   `[nombre]` que o LLM eventualmente vaze) e como helper interno do
   `_extract_name_from_history`.

Ordem de prioridade nos pontos de uso:
- `deliver_purchase` (agradecimento pós-pagamento) → histórico
- Intercept pós-entrega ("ya pagué" reincidente) → histórico
- Follow-up de checkout (2h após link) → histórico
- Follow-up de preço (30min após PASO 5 sem resposta) → histórico
- `strip_placeholders` (placeholder `[nombre]` vazado pelo LLM em
  resposta IA) → ainda usa `lead_name` (lookup_lead_name + sender_name),
  mas é raro porque o LLM normalmente já pega o nome do contexto.

## Detector de fechamento de cortesia

`_is_courtesy_close(lead_text, last_assistant_text)` em `agent.py` evita
loop de despedidas. Se a última mensagem da Sandra contém marcador de
despedida ("que tengas", "estoy aquí", "lindo día", etc.) E a mensagem
do lead tem ≤10 palavras formadas só por vocabulário de cortesia
(`gracias`, `igualmente`, `chau`, `bendiciones`, etc.) — `handle_message`
retorna `None` e o bot não responde. Conversa fecha sem o bot
monopolizar a última palavra.

## Logs

```bash
# Watcher (mensagens entrando/saindo)
pm2 logs watcher
tail -f /root/meu-agente/watcher.log

# Cron diário
tail -f knowledge/analyses/_runs/cron.log

# Cron semanal
tail -f knowledge/analyses/_runs/weekly.log

# Daily run específico
tail -f knowledge/analyses/_runs/2026-05-04_daily_run.log
```

## Histórico de mudanças relevantes

### 2026-05-06 (continuação) — Reset de teste + intercept pós-entrega

- **Reset automático em `/teste`**: cada vez que o dono entra em modo teste, `reset_lead_delivery_state(wa_OWNER_PHONE)` zera `books_delivered`, `books_delivered_at`, `sent_checkout`, `image_alert_sent`. Sem isso, o segundo `/teste` em diante caía na fallback do LLM porque o flag de entrega ficava persistido do teste anterior.
- **Intercept pós-entrega no watcher**: se um trigger de pagamento (`payment_text`/`awaiting_files`/`image_proof`) bate mas `books_already_delivered=1`, o watcher manda uma mensagem fixa apontando que os PDFs já foram enviados e **não chama a IA**. Evita o LLM emitir `[[ENVIAR_LIBROS]]` (marker que despacha capas de PNG do PASO 4) em contexto pós-pagamento, que estava confundindo cliente.
- **Regra 13 reforçada**: prompt agora proíbe explicitamente o marker `[[ENVIAR_LIBROS]]` em contexto pós-pagamento (defesa em profundidade caso o intercept do watcher falhe).
- **Caso que motivou**: dono testou /teste duas vezes seguidas. No segundo teste, "Ya hice el pago" não disparou auto-entrega (books_delivered=1 do teste anterior), o LLM agradeceu via rule 13, depois ao perguntar "donde están los libros?" o LLM emitiu `[[ENVIAR_LIBROS]]` e o watcher enviou as 5 capas PNG (libro1.png … libro5.png) em vez dos PDFs.

### 2026-05-06 — Trust model + entrega automática + resiliência

- **Entrega automática dos 5 PDFs**: `watcher.py` intercepta sinais de pós-pagamento (texto/imagem) antes do LLM e dispara `deliver_purchase` — manda agradecimento + envia `libro1.pdf`…`libro5.pdf` + comemora venda com o dono. LLM bypassed.
- **Trust model**: nunca pedimos comprovante. Imagem após checkout = comprovante presumido. Texto "ya pagué" = aceito sem verificação.
- **Anti-revenda**: regra 13 do prompt + bypass do LLM. Regra 14 cobre mensagens que a Sandra não entende ("Disculpa, no entendí…").
- **Detector de cortesia**: bot não monopoliza última palavra (não responde a "gracias" depois de despedida).
- **Sanitização de pushName**: follow-ups e placeholders param de gerar coisas como "Hola helgase1956@gmail.com" ou "Hola compañeros".
- **Retry universal**: `send_whatsapp` (texto) e `_send_pdf_with_retry` (PDFs) — 3 tentativas com backoff 2s/4s. Caso de falha que motivou: lead `+598 99 592 666` (Miriam) ficou sem resposta porque a Evolution API deu timeout único; antes a resposta era gravada no histórico mesmo sem chegar ao lead.
- **`commit_response`**: `agent.py` separou compute (`handle_message`) de persist (`commit_response`). Histórico só recebe `assistant` após o WhatsApp confirmar entrega. Se falhar 3x, dono recebe alerta com preview da mensagem perdida.
- **PM2 canonical**: `chef-sandra.service` do systemd foi desabilitado por conflitar com o PM2 (mesmo `watcher.py` rodando duas vezes, exit 209/STDOUT em loop). Use `pm2 restart watcher`.
- **Schema novo em `leads`**: `books_delivered`, `books_delivered_at`, `image_alert_sent`. Migração idempotente em `init_db`.
