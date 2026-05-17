#!/usr/bin/env python3
"""
agent.py — Agente de vendas WhatsApp — Chef Sandra
Produto: Panadería Inteligente: Recetas Seguras para Diabéticos
"""

import os
import random
import re
import sys
import json
import socket
import sqlite3
import time
import logging
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_logger = logging.getLogger(__name__)


def _load_env_file(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env_file(Path(__file__).parent / ".env")


# ── Configuração ──────────────────────────────────────────────────────────────

AI_PROVIDER = (os.environ.get("AI_PROVIDER") or "openai").strip().lower()
AI_MODEL    = (os.environ.get("AI_MODEL") or "gpt-4o-mini").strip()

_PROVIDER_DEFAULTS = {
    "openai":   {"url": "https://api.openai.com/v1/chat/completions",   "key_env": "OPENAI_API_KEY"},
    "deepseek": {"url": "https://api.deepseek.com/v1/chat/completions", "key_env": "DEEPSEEK_API_KEY"},
}
if AI_PROVIDER not in _PROVIDER_DEFAULTS:
    raise RuntimeError(
        f"AI_PROVIDER='{AI_PROVIDER}' não suportado. Use um de: {list(_PROVIDER_DEFAULTS)}"
    )
AI_API_URL = (os.environ.get("AI_API_URL") or _PROVIDER_DEFAULTS[AI_PROVIDER]["url"]).strip()
_key_env   = _PROVIDER_DEFAULTS[AI_PROVIDER]["key_env"]
AI_API_KEY = (os.environ.get(_key_env) or os.environ.get("AI_API_KEY") or "").strip()
if not AI_API_KEY:
    raise RuntimeError(
        f"{_key_env} não definida. Configure no .env (chef-sandra/.env) ou exporte no ambiente."
    )

CHECKOUT_BASIC   = "https://pay.hotmart.com/B105738743A?off=b06dsju5"  # $6.90  — contribuição básica
CHECKOUT_LITE    = "https://pay.hotmart.com/B105738743A?off=fu80jd8q"  # $7.90  — contribuição intermediária
CHECKOUT_MAIN    = "https://pay.hotmart.com/B105738743A?off=c85jcg6l"  # $9.90  — contribuição padrão
CHECKOUT_PREMIUM = "https://pay.hotmart.com/B105738743A?off=blbtdrbb"  # $12.90 — contribuição especial
CHECKOUT_MINIMUM = "https://pay.hotmart.com/B105738743A?off=7kz5vp3l"  # $5.00  — oferta de objeção (não default)
CHECKOUT_DONATION = "https://buy.stripe.com/cNi9AS9ui5orgAYbq01Jm01"   # livre  — link aberto, último recurso

ALL_CHECKOUTS = [CHECKOUT_BASIC, CHECKOUT_LITE, CHECKOUT_MAIN, CHECKOUT_PREMIUM, CHECKOUT_MINIMUM, CHECKOUT_DONATION]

FOLLOWUP_DELAY = 7200        # 2 horas em segundos (após envio do link)
PRICE_FOLLOWUP_DELAY = 1800  # 30 min em segundos (após envio do preço sem resposta)

# Recovery: cadência crescente (em segundos) pra reengajar lead que sumiu.
# 30min → 4h → 1d. Cada stage só dispara se o cliente ainda não respondeu.
# Quiet hours: bot não manda recovery dentro desse intervalo (formato 24h,
# fuso TIMEZONE).
RECOVERY_STAGES_SECONDS    = [1800, 14400, 86400]
RECOVERY_QUIET_HOURS_START = 22
RECOVERY_QUIET_HOURS_END   = 8
TIMEZONE                   = "America/Sao_Paulo"

# Kill switch dos disparos automáticos no watcher. Mantido como defesa
# em camadas, mas em 12/05/2026 o owner decidiu DESLIGAR permanentemente
# o fluxo de followups + recovery na Sandra (commit_response não mais
# agenda nada — ver nota lá). Esse kill switch fica como segunda barreira
# caso alguém reintroduza scheduling sem perceber.
DISPATCH_ENABLED = False

OWNER_PHONE = "5544997317509"  # Número do dono — modo gerencial

# Confirmação de pagamento — APENAS formas em pretérito de 1ª pessoa.
# REMOVIDO: "pague" / "ya pague" (subjuntivo em ES: "para que él pague"
# disparava entrega indevida — caso Mónica +598 em 17/05/2026).
# REMOVIDO: "compré" / "comprei" soltos (matchavam dentro de "antes de
# comprar", "no compré", etc).
PAYMENT_KEYWORDS = [
    # ES — pretérito 1ª pessoa (inequívoco)
    "pagué", "ya pagué", "acabo de pagar", "acabé de pagar",
    "realicé el pago", "hice el pago", "completé el pago", "finalicé el pago",
    "ya compré", "ya hice la compra", "ya transferí",
    # PT — pretérito 1ª pessoa
    "paguei", "já paguei", "acabei de pagar", "fiz o pagamento", "fiz o pix",
    "transferi", "já transferi", "já comprei", "fiz a compra", "finalizei o pagamento",
]

# Padrões que indicam INTENÇÃO / FUTURO / 3ª pessoa / negação. Se qualquer
# um aparecer no texto, `is_payment_confirmation` retorna False mesmo que
# tenha matchado um keyword acima — evita disparar entrega quando o lead
# está só anunciando que vai pagar, ou que terceiro vai pagar por ele.
PAYMENT_INTENT_NEGATIVE = [
    # ES — futuro / intenção / 3ª pessoa / subjuntivo / negação
    "voy a pagar", "vamos a pagar", "iré a pagar", "iría a pagar",
    "quiero pagar", "cuando pague", "si pago", "si pagara",
    "antes de pagar", "para pagar", "para que pague",
    "para que él pague", "para que ella pague",
    "que él pague", "que ella pague", "que pague",
    "le paso a", "le pasé a", "le pase a", "le mandé a", "le mande a",
    "le pedí a", "le pedi a", "le dije a",
    "mi hijo va a", "mi hija va a", "mi esposo va a", "mi esposa va a",
    "mi marido va a", "mi pareja va a",
    "va a pagar", "va a hacer el pago", "va a hacer el pix",
    "después pago", "luego pago", "más tarde pago", "mañana pago",
    "ahorita pago", "todavía no", "aún no", "no he pagado", "no pagué",
    "estoy por pagar", "estoy pagando",
    # PT — mesmas categorias
    "vou pagar", "vou fazer o pix", "vou fazer o pagamento",
    "vou transferir", "quero pagar", "quando pagar", "quando eu pagar",
    "se pagar", "se eu pagar", "antes de pagar", "pra pagar",
    "pra que pague", "para que pague",
    "passei pra", "passei pro", "pedi pro", "pedi pra", "falei pro",
    "falei pra", "disse pro", "disse pra",
    "meu filho vai", "minha filha vai", "marido vai", "esposa vai",
    "depois eu pago", "logo eu pago", "amanhã pago", "ainda vou",
    "ainda não", "ainda não paguei", "não paguei",
    "tô pagando", "estou pagando",
]

# Lead pedindo os arquivos depois de já ter recebido o link de checkout —
# pode ter pagado mas não ter dito explicitamente "pagué". Só faz sentido
# quando _checkout_already_sent já é True; senão é falso positivo do funnel.
AWAITING_FILES_KEYWORDS = [
    # ES
    "envíame los libros", "envíamelos", "mándame los libros", "mándamelos",
    "manda los libros", "envia los libros", "envía los libros",
    "manda el material", "envía el material", "manden el material",
    "donde están los libros", "dónde están los libros",
    "cuándo llegan los libros", "cuando llegan los libros",
    "cuándo me llegan", "cuando me llegan", "cuándo me los envías",
    "no me llegó", "no me ha llegado", "no recibí nada", "no he recibido nada",
    "no me llegaron", "no llegaron los libros",
    "estoy esperando los libros", "esperando los libros",
    "esperando el material", "esperando el envío", "esperando que me envíes",
    "necesito los libros", "necesito el material",
    "quiero los libros ya", "ya quiero los libros",
    # PT-BR
    "me envia os livros", "me manda os livros", "manda os livros",
    "cadê os livros", "cadê o material",
    "não recebi nada", "não chegou nada", "estou esperando os livros",
    "estou esperando o material",
]

TRIGGER_EXACT    = "Quiero las recetas para diabéticos 🍞"
TRIGGER_KEYWORDS = ["recetas", "diabéticos", "panadería", "sandra", "libro"]

MAX_HISTORY = 40    # máximo de mensagens enviadas para a IA (evita tokens excessivos)
DB_PATH     = Path.home() / "chef-sandra" / "dados.sqlite"

# ── Knowledge layer (carregado dinâmicamente a cada chamada) ──────────────────
# Fase 1 do sistema de aprendizado: o prompt do agente é montado em camadas
# a partir de arquivos versionados em ./knowledge/. Comportamento idêntico ao
# antigo SYSTEM_PROMPT enquanto playbook.md tiver a seed inicial e nenhum
# perfil estiver ativo.

_KNOWLEDGE_DIR = Path.home() / "chef-sandra" / "knowledge"
_KB_PATH       = Path.home() / "chef-sandra" / "base_conhecimento.md"


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _load_core_rules() -> str:
    text = _read_file(_KNOWLEDGE_DIR / "core_rules.md")
    return (text
            .replace("{CHECKOUT_BASIC}",    CHECKOUT_BASIC)
            .replace("{CHECKOUT_LITE}",     CHECKOUT_LITE)
            .replace("{CHECKOUT_MAIN}",     CHECKOUT_MAIN)
            .replace("{CHECKOUT_PREMIUM}",  CHECKOUT_PREMIUM)
            .replace("{CHECKOUT_MINIMUM}",  CHECKOUT_MINIMUM)
            .replace("{CHECKOUT_DONATION}", CHECKOUT_DONATION))


def _load_playbook() -> str:
    return _read_file(_KNOWLEDGE_DIR / "playbook.md")


def _load_profile(slug: str) -> str:
    """Lê o arquivo do perfil e mantém apenas a seção '# DIRETIVAS' em diante.
    A seção '# CONTEXTO' (PT-BR, descritiva pra humano) é descartada antes
    de injetar no system prompt da Chef Sandra — só DIRETIVAS (ES) entram."""
    if not slug:
        return ""
    raw = _read_file(_KNOWLEDGE_DIR / "profiles" / f"{slug}.md")
    if not raw:
        return ""
    # Se o arquivo tem a seção '# DIRETIVAS', recorta a partir dela.
    m = re.search(r'^#\s*DIRETIVAS.*$', raw, flags=re.MULTILINE | re.IGNORECASE)
    if m:
        return raw[m.start():]
    return raw  # legado / arquivos sem seções estruturadas


def _load_knowledge_base() -> str:
    return _read_file(_KB_PATH)


def build_system_prompt(profile_slug: str = None) -> str:
    """Monta o system prompt em camadas:
    1. core_rules    — imutável (identidade, REGLAS, FLUJO PASO 1-8)
    2. playbook      — curado pelo promoter (heurísticas validadas)
    3. perfil ativo  — opcional, injetado quando classificador detecta
    4. base de conhecimento do produto
    """
    blocks = []

    blocks.append(_load_core_rules().rstrip("\n"))

    pb = _load_playbook().rstrip("\n")
    if pb:
        blocks.append(pb)

    if profile_slug:
        prof = _load_profile(profile_slug).rstrip("\n")
        if prof:
            blocks.append(
                "════════════════════════════════════════\n"
                f"PERFIL DETECTADO: {profile_slug}\n"
                "════════════════════════════════════════\n"
                "\n"
                f"{prof}"
            )

    kb = _load_knowledge_base()
    base_block = (
        "════════════════════════════════════════\n"
        "BASE DE CONOCIMIENTO DEL PRODUCTO\n"
        "════════════════════════════════════════\n"
        "Usa las informaciones abajo para responder preguntas específicas sobre recetas, ingredientes y contenido:\n"
        "\n"
        f"{kb}"
    )
    blocks.append(base_block)

    return "\n\n".join(blocks) + "\n"


# Compat: SYSTEM_PROMPT continua exportado, computado uma vez no import.
# (call_ai recompõe a cada chamada quando profile_slug é passado.)
_KNOWLEDGE_BASE = _load_knowledge_base()  # mantido para qualquer caller externo
SYSTEM_PROMPT   = build_system_prompt()


OWNER_SYSTEM_PROMPT = """Eres Chef Sandra, asistente personal del dueño del negocio.
Con él hablas de forma natural, amigable y directa — sin intentar vender nada.
Cuando te pidan información sobre ventas o clientes, usa los datos que te proporciono para dar un resumen claro y útil.
Responde en el mismo idioma del mensaje recibido (español o portugués).
"""


# ── IA ────────────────────────────────────────────────────────────────────────

def call_ai(messages: list, max_tokens: int = 512, system: str = None,
            profile_slug: str = None, timeout: int = 30,
            response_format: dict = None, attempts: int = 3) -> str:
    """Chama OpenAI Chat Completions com retry+backoff para falhas transitórias.

    Retry em: timeout (socket.timeout), URLError, HTTPError 5xx/429.
    Sem retry em: HTTPError 4xx (≠429) — erro permanente, não vai melhorar.
    Esgotadas as tentativas, RAISE — o caller (watcher) decide se avisa o dono.
    NÃO retornamos string de erro pro lead; isso vazaria mensagem técnica
    ('Lo siento, hubo un error técnico…') sem contexto e atrapalharia o funil.
    """
    url = AI_API_URL
    if system is not None:
        sys_prompt = system
    else:
        # Recompõe a cada chamada — permite editar playbook.md ou
        # profiles/*.md sem reiniciar o watcher.
        sys_prompt = build_system_prompt(profile_slug=profile_slug)
    data = {
        "model": AI_MODEL,
        "messages": [{"role": "system", "content": sys_prompt}] + messages,
        "max_tokens": max_tokens,
        "temperature": 0.7
    }
    if response_format:
        data["response_format"] = response_format
    headers = {
        "Authorization": f"Bearer {AI_API_KEY}",
        "Content-Type": "application/json"
    }
    body = json.dumps(data).encode()

    last_err = None
    for attempt in range(attempts):
        # Recria o Request a cada attempt — urllib não reaproveita o handle
        # se a chamada anterior consumiu o body stream.
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read())
                return _sanitize(result["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            last_err = e
            transient = (e.code >= 500) or (e.code == 429)
            if not transient:
                # 4xx (auth, payload inválido, etc.) — não adianta retry.
                _logger.error(f"OpenAI HTTP {e.code} (sem retry): {e}")
                raise
            if attempt < attempts - 1:
                wait = 2 * (attempt + 1)
                _logger.warning(
                    f"↻ OpenAI HTTP {e.code} — retry em {wait}s "
                    f"(tentativa {attempt + 2}/{attempts})"
                )
                time.sleep(wait)
        except (socket.timeout, urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < attempts - 1:
                wait = 2 * (attempt + 1)
                _logger.warning(
                    f"↻ OpenAI timeout/URL error — retry em {wait}s "
                    f"(tentativa {attempt + 2}/{attempts}): {e}"
                )
                time.sleep(wait)

    _logger.error(f"❌ OpenAI falhou após {attempts} tentativas: {last_err}")
    raise RuntimeError(f"OpenAI unreachable after {attempts} attempts: {last_err}")


def _sanitize(text: str) -> str:
    """Remove formatação markdown de links — WhatsApp não renderiza [texto](url)."""
    return re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\2', text)


# ── Sanitizador de placeholders vazados ──────────────────────────────────────
# Camada defensiva: se o LLM emitir [nombre], [tu nombre], [familiar] etc.
# literalmente, o substitui pelo nome real do lead se for "seguro" (parece um
# nome de pessoa) ou apaga o placeholder + vírgula órfã sem deixar buraco.

# Os negative lookarounds (?<!\[) e (?!\]) preservam marcadores de mídia
# tipo [[ENVIAR_LIBROS]] — o sanitizador NÃO pode tocar nesses, senão o
# watcher não detecta e o cliente recebe "[]" literal.
_NAME_PLACEHOLDER_RE = re.compile(
    r"(?<!\[)\[\s*(?:tu\s+)?(?:nombre|name|familiar)\s*\](?!\])",
    re.IGNORECASE,
)
# Genérico: qualquer [palavra] solitário que NÃO seja
#   - parte de marcador [[X]] (lookbehind/lookahead de [ e ])
#   - link markdown [texto](url) (lookahead de ()
_GENERIC_PLACEHOLDER_RE = re.compile(
    r"(?<!\[)\[[A-Za-zÀ-ÿ_][A-Za-zÀ-ÿ0-9_ ]{0,40}\](?!\])(?!\()"
)
# Nome humano "limpo": começa com MAIÚSCULA (inclui acentuadas Á-Ý e Ñ),
# 2-30 chars, só letras + apóstrofo/hífen. Exigir maiúscula filtra usernames
# tipo "bettyvillanuevapardo". Email/dígitos são rejeitados antes.
_SAFE_NAME_RE = re.compile(r"^[A-ZÀ-Ý][A-Za-zÀ-ÿ'\- ]{1,29}$")

# Pushnames genéricos do WhatsApp / etiquetas de grupo / fallback do watcher.
# Comparação em lowercase. Mantém só palavras-isca; evita nomes próprios reais.
_NAME_BLOCKLIST = {
    "lead", "amig@", "amigo", "amiga", "amigos", "amigas",
    "compañero", "compañera", "compañeros", "compañeras",
    "familia", "equipo", "hermanos", "hermanas", "niños", "niñas",
    "mamá", "mama", "papá", "papa", "amor", "mi amor",
    "contacto", "info", "soporte", "whatsapp", "business", "cliente",
    # Saudações / monossílabos que leads digitam quando perguntados o nome
    # (visto em produção: lead responde "Hola" ao "¿Cómo te llamas?", virava
    # "Hola Hola" no template de followup). Bloqueamos pra cair em saudação
    # neutra em vez de duplicar a palavra.
    "hola", "holaa", "holaaa", "buenas", "buenos", "buen", "buen día",
    "buenas tardes", "buenas noches", "buen día",
    "ola", "oi", "hi", "hello", "hey",
    "gracias", "ok", "sí", "si", "no", "vale", "dale", "perfecto",
}


def _safe_first_name(raw: str) -> str:
    """Devolve o primeiro nome se 'raw' parece um nome humano; senão ''.
    Rejeita: vazio, email, username com dígitos, lowercase-only, blocklist
    de etiquetas genéricas (compañeros, familia, lead, etc.)."""
    if not raw:
        return ""
    stripped = raw.strip()
    if not stripped:
        return ""
    # Email inteiro vira "" (helgase1956@gmail.com)
    if "@" in stripped:
        return ""
    first = stripped.split()[0]
    # Username com dígitos (helgase1956) vira ""
    if any(ch.isdigit() for ch in first):
        return ""
    # Etiqueta genérica (compañeros, Lead, familia) vira ""
    if first.lower() in _NAME_BLOCKLIST:
        return ""
    # Precisa começar com maiúscula — filtra "bettyvillanuevapardo"
    if _SAFE_NAME_RE.match(first):
        return first
    return ""


# Pergunta da Sandra que pede o nome do lead (PASO 1). Cobrimos variações
# comuns. Quando bate, a próxima mensagem do user é candidata a ser o nome.
_NAME_QUESTION_RE = re.compile(
    r"(c[oó]mo te llamas|cu[aá]l es tu nombre|tu nombre|me dices tu nombre|"
    r"te llamas|c[oó]mo te llamás)",
    re.IGNORECASE,
)
# Prefixos comuns na resposta do lead que carregam o nome real:
# "Soy Cristina", "Me llamo Cristina", "Mi nombre es Cristina", etc.
_NAME_INTRO_RE = re.compile(
    r"^\s*(?:hola[,\s]+)?(?:me\s+llamo|mi\s+nombre\s+es|soy|me\s+dicen|"
    r"me\s+llaman|aqu[ií]\s+es)\s+",
    re.IGNORECASE,
)


def _extract_name_from_history(lead_id: str) -> str:
    """Extrai o primeiro nome real do lead do histórico da conversa,
    procurando a resposta à pergunta de PASO 1 ("¿Cómo te llamas?"). Mais
    confiável que `pushName` do WhatsApp — leads costumam colocar
    apelidos/etiquetas/configurações de tradução ali (ex: 'Traducir Al
    Español') que não devem ser usadas como nome em saudações.

    Retorna "" se não encontrar — chamadores devem cair em saudação
    neutra ("Hola") em vez de inventar nome."""
    if not lead_id:
        return ""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM messages WHERE lead_id = ? ORDER BY ts ASC, id ASC",
        (lead_id,),
    )
    rows = c.fetchall()
    conn.close()

    awaiting_response = False
    for r in rows:
        role = r["role"]
        content = (r["content"] or "")
        if role == "assistant" and _NAME_QUESTION_RE.search(content):
            awaiting_response = True
            continue
        if awaiting_response and role == "user":
            raw = content.strip()
            # Tira prefixos comuns ("me llamo X", "soy X")
            raw = _NAME_INTRO_RE.sub("", raw)
            tokens = raw.split()
            if not tokens:
                return ""
            first = tokens[0].strip(".,;:!?¡¿\"'()[]{}")
            # Capitaliza primeira letra pra passar pelo _SAFE_NAME_RE
            # (lead pode ter escrito "cristina" tudo minúsculo)
            if first:
                first = first[:1].upper() + first[1:]
            return _safe_first_name(first)
    return ""


def strip_placeholders(text: str, lead_name: str = None) -> str:
    """Limpa qualquer placeholder vazado pelo LLM. Substitui placeholders de
    NOME pelo nome real do lead (se conhecido e plausível); senão remove o
    placeholder + vírgula órfã. Demais placeholders genéricos viram vazio."""
    if not text:
        return text

    safe_name = _safe_first_name(lead_name)

    # 1) Placeholders de nome: substitui pelo nome real, ou apaga
    if safe_name:
        text = _NAME_PLACEHOLDER_RE.sub(safe_name, text)
    else:
        # remove ", [nombre]" e "[nombre]," primeiro pra evitar vírgula órfã
        text = re.sub(r"\s*,\s*" + _NAME_PLACEHOLDER_RE.pattern, "",
                      text, flags=re.IGNORECASE)
        text = re.sub(_NAME_PLACEHOLDER_RE.pattern + r"\s*,?", "",
                      text, flags=re.IGNORECASE)

    # 2) Outros placeholders genéricos restantes (não são markdown link)
    # Preserva o nome já substituído acima — então _GENERIC_PLACEHOLDER_RE só
    # encontra coisas como [link], [familiar], [profesion]...
    text = re.sub(r"\s*,\s*" + _GENERIC_PLACEHOLDER_RE.pattern, "", text)
    text = re.sub(_GENERIC_PLACEHOLDER_RE.pattern + r"\s*,?", "", text)

    # 3) Limpa artefatos de cleanup — IMPORTANTE: só mexe em espaços/tabs,
    # nunca em newlines (caso contrário templates multi-linha tipo PASO 5
    # viram parede de texto).
    text = re.sub(r"[ \t]+([!?.,;:])", r"\1", text)        # " !" → "!"
    text = re.sub(r"[!?.,;:][ \t]*([!?.,;:])", r"\1", text) # "!." → "."
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"^[ \t,;:]+", "", text)
    # remove linhas que ficaram só com pontuação solta após cleanup
    text = re.sub(r"\n[ \t,;:]+", "\n", text)
    return text.strip()


# ── Banco de dados ────────────────────────────────────────────────────────────

def _db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            messages_json TEXT NOT NULL,
            last_activity INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id TEXT PRIMARY KEY,
            name TEXT,
            phone TEXT UNIQUE,
            source TEXT,
            sent_checkout INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            ts INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS followups (
            lead_id TEXT PRIMARY KEY,
            phone TEXT NOT NULL,
            name TEXT,
            checkout_sent_at INTEGER NOT NULL,
            followup_sent INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS price_followups (
            lead_id TEXT PRIMARY KEY,
            phone TEXT NOT NULL,
            name TEXT,
            price_sent_at INTEGER NOT NULL,
            followup_sent INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS recoveries (
            lead_id          TEXT PRIMARY KEY,
            phone            TEXT NOT NULL,
            name             TEXT,
            stage            INTEGER DEFAULT 0,
            next_attempt_at  INTEGER NOT NULL,
            last_attempt_at  INTEGER,
            status           TEXT DEFAULT 'pending',
            source           TEXT DEFAULT 'auto',
            created_at       INTEGER NOT NULL,
            updated_at       INTEGER NOT NULL
        )
    """)

    # Stash durável dos markers de recovery extraídos em handle_message até
    # serem consumidos por commit_response. Antes era um dict em memória, mas
    # um restart entre handle e commit (janela ~1-3s) perdia o marker. TTL
    # curto via housekeeping abaixo — entradas órfãs > 1h são lixo benigno.
    c.execute("""
        CREATE TABLE IF NOT EXISTS pending_recovery_actions (
            phone       TEXT PRIMARY KEY,
            kind        TEXT,
            value       INTEGER,
            created_at  INTEGER NOT NULL
        )
    """)
    # Housekeeping: remove entradas órfãs (> 1h). Só roda no boot, sem
    # custo notável — a tabela tende a ficar pequena (1 linha por lead ativo
    # em transição entre handle e commit).
    c.execute(
        "DELETE FROM pending_recovery_actions "
        "WHERE created_at < strftime('%s','now') - 3600"
    )

    # ── Fase 1: tabelas do sistema de aprendizado ──────────────────────────
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversation_analysis (
            lead_id              TEXT PRIMARY KEY,
            analyzed_at          INTEGER NOT NULL,
            atypical             INTEGER DEFAULT 0,
            atypical_reason      TEXT,
            outcome              TEXT,
            outcome_confidence   REAL,
            profile_suggestion   TEXT,    -- JSON
            what_worked          TEXT,    -- JSON
            what_failed          TEXT,    -- JSON
            objections_seen      TEXT,    -- JSON
            evidence_quality     TEXT,
            raw_json             TEXT     -- skill output completo, pra auditoria
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS proposals (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            type                 TEXT NOT NULL,    -- new_profile|refine_profile|new_rebuttal|update_playbook
            target               TEXT NOT NULL,    -- slug do perfil ou seção
            body                 TEXT NOT NULL,    -- conteúdo proposto (markdown)
            evidence_lead_ids    TEXT NOT NULL,    -- JSON array
            occurrences          INTEGER DEFAULT 1,
            distinct_days        INTEGER DEFAULT 1,
            first_seen_date      TEXT NOT NULL,
            last_seen_date       TEXT NOT NULL,
            status               TEXT DEFAULT 'pending', -- pending|staging|promoted|rejected|rolled_back|expired
            promoted_at          INTEGER,
            promoted_version     TEXT,
            rolled_back_at       INTEGER,
            rolled_back_reason   TEXT,
            decided_at           INTEGER,
            decided_reason       TEXT,
            UNIQUE(type, target)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_metrics (
            date                 TEXT PRIMARY KEY,
            new_leads            INTEGER DEFAULT 0,
            link_sent            INTEGER DEFAULT 0,
            payment_confirmed    INTEGER DEFAULT 0,
            atypical_count       INTEGER DEFAULT 0,
            avg_messages_per_lead REAL DEFAULT 0,
            computed_at          INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS playbook_versions (
            version              TEXT PRIMARY KEY,    -- ex: 2026-05-04T03:00:00
            created_at           INTEGER NOT NULL,
            change_type          TEXT,                -- promote|rollback|seed|manual
            target               TEXT,                -- arquivo afetado
            proposal_id          INTEGER,
            diff                 TEXT,                -- diff aplicado
            metric_baseline      TEXT,                -- JSON com métricas no momento
            note                 TEXT
        )
    """)

    # ── Fase 1+2: novas colunas em leads (idempotente) ─────────────────────
    for ddl in (
        "ALTER TABLE leads ADD COLUMN profile_slug TEXT",
        "ALTER TABLE leads ADD COLUMN profile_confidence REAL",
        "ALTER TABLE leads ADD COLUMN outcome TEXT",
        "ALTER TABLE leads ADD COLUMN outcome_inferred_at INTEGER",
        "ALTER TABLE leads ADD COLUMN classified_at_msg_count INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN classified_at_ts INTEGER",
        "ALTER TABLE leads ADD COLUMN paused INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN daily_recovered_at INTEGER",
        "ALTER TABLE leads ADD COLUMN books_delivered INTEGER DEFAULT 0",
        "ALTER TABLE leads ADD COLUMN books_delivered_at INTEGER",
        "ALTER TABLE leads ADD COLUMN image_alert_sent INTEGER DEFAULT 0",
    ):
        try:
            c.execute(ddl)
        except sqlite3.OperationalError:
            pass  # coluna já existe

    conn.commit()
    conn.close()


def get_lead_profile(lead_id: str) -> str:
    """Retorna o profile_slug atualmente associado ao lead, ou None."""
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT profile_slug FROM leads WHERE id = ?", (lead_id,))
    row = c.fetchone()
    conn.close()
    return row["profile_slug"] if row and row["profile_slug"] else None


def get_lead_classification_state(lead_id: str) -> dict:
    """Retorna {profile_slug, profile_confidence, classified_at_msg_count}."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT profile_slug, profile_confidence, classified_at_msg_count "
        "FROM leads WHERE id = ?", (lead_id,)
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return {"profile_slug": None, "profile_confidence": 0.0, "classified_at_msg_count": 0}
    return {
        "profile_slug":            row["profile_slug"],
        "profile_confidence":      row["profile_confidence"] or 0.0,
        "classified_at_msg_count": row["classified_at_msg_count"] or 0,
    }


def update_lead_classification(lead_id: str, slug: str, confidence: float, msg_count: int):
    conn = _db()
    c = conn.cursor()
    c.execute(
        "UPDATE leads SET profile_slug = ?, profile_confidence = ?, "
        "classified_at_msg_count = ?, classified_at_ts = ?, updated_at = ? "
        "WHERE id = ?",
        (slug, confidence, msg_count, int(time.time()),
         datetime.now().isoformat(), lead_id)
    )
    conn.commit()
    conn.close()


# ── Fase 2: classificador de perfil ──────────────────────────────────────────

CLASSIFIER_FIRST_AT_MSGS = 2   # primeira classificação após N msgs do user
CLASSIFIER_REFRESH_EVERY = 5   # reclassifica a cada N msgs novas


def _load_active_profiles() -> list:
    """Lê knowledge/profiles/*.md (exceto _index.md) e retorna lista de
    {slug, content}. Vazia até o promoter (Fase 5) criar o primeiro perfil."""
    profiles_dir = _KNOWLEDGE_DIR / "profiles"
    if not profiles_dir.exists():
        return []
    out = []
    for p in sorted(profiles_dir.glob("*.md")):
        if p.name.startswith("_"):
            continue
        out.append({"slug": p.stem, "content": p.read_text(encoding="utf-8")})
    return out


def _format_conversation_for_classifier(messages: list, max_msgs: int = 20) -> str:
    """Formata histórico em texto plano [role]: content para o classificador.
    Limita aos últimos max_msgs para economizar tokens."""
    recent = messages[-max_msgs:]
    return "\n".join(f"[{m['role']}]: {m['content']}" for m in recent)


def _build_classifier_prompt(profiles: list, current_state: dict, conversation: str) -> str:
    profiles_block = "\n\n".join(
        f"## {p['slug']}\n{p['content']}" for p in profiles
    ) if profiles else "(nenhum perfil ativo — retorne {slug:null, confidence:0})"

    cur_slug = current_state.get("profile_slug") or "null"
    cur_conf = current_state.get("profile_confidence") or 0.0

    return (
        f"=== PERFIS ATIVOS ===\n{profiles_block}\n\n"
        f"=== CLASSIFICAÇÃO ATUAL ===\n"
        f"slug: {cur_slug}\nconfidence: {cur_conf}\n\n"
        f"=== CONVERSA ===\n{conversation}\n"
    )


def _classifier_skill_prompt() -> str:
    return _read_file(_KNOWLEDGE_DIR / "skills" / "profile_classifier.md")


def classify_profile(lead_id: str, messages: list) -> dict:
    """Classifica o lead em um dos perfis ativos. Retorna
    {slug, confidence, reasoning}. Se não há perfis ativos, atalha sem
    chamar a API (custo zero)."""
    profiles = _load_active_profiles()
    state = get_lead_classification_state(lead_id)
    if not profiles:
        return {"slug": None, "confidence": 0.0,
                "reasoning": "no active profiles"}

    convo = _format_conversation_for_classifier(messages)
    user_prompt = _build_classifier_prompt(profiles, state, convo)
    system = _classifier_skill_prompt()

    raw = call_ai(
        [{"role": "user", "content": user_prompt}],
        max_tokens=200,
        system=system,
    )

    try:
        # tenta parse direto; se vier com cerca, tenta extrair JSON
        s = raw.strip()
        if s.startswith("```"):
            s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
        data = json.loads(s)
        slug       = data.get("slug")
        confidence = float(data.get("confidence") or 0.0)
        reasoning  = data.get("reasoning", "")
        # gate: confiança baixa → não aplica
        if confidence < 0.6:
            slug = None
        return {"slug": slug, "confidence": confidence, "reasoning": reasoning}
    except Exception as e:
        return {"slug": None, "confidence": 0.0,
                "reasoning": f"parse_error: {e}: {raw[:80]}"}


def should_classify(lead_id: str, current_user_msg_count: int) -> bool:
    state = get_lead_classification_state(lead_id)
    last  = state["classified_at_msg_count"]
    if last == 0 and current_user_msg_count >= CLASSIFIER_FIRST_AT_MSGS:
        return True
    if last > 0 and (current_user_msg_count - last) >= CLASSIFIER_REFRESH_EVERY:
        return True
    return False


def maybe_classify(lead_id: str, messages: list):
    """Conveniência chamada pelo handle_message — só classifica se passou no
    gate, e só persiste se a classificação tem confiança útil. Sem perfis
    ativos, é um no-op silencioso (sem custo de API)."""
    user_msg_count = sum(1 for m in messages if m.get("role") == "user")
    if not should_classify(lead_id, user_msg_count):
        return
    if not _load_active_profiles():
        # incrementa contador pra não disparar de novo a cada msg
        update_lead_classification(
            lead_id,
            get_lead_classification_state(lead_id)["profile_slug"],
            get_lead_classification_state(lead_id)["profile_confidence"],
            user_msg_count,
        )
        return

    result = classify_profile(lead_id, messages)
    update_lead_classification(
        lead_id, result["slug"], result["confidence"], user_msg_count
    )


def load_session(lead_id: str) -> list:
    """Carrega o histórico completo da conversa direto da tabela messages.
    Limita ao MAX_HISTORY mais recentes para não estourar tokens."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT role, content FROM messages WHERE lead_id = ? ORDER BY ts ASC",
        (lead_id,)
    )
    rows = c.fetchall()
    conn.close()
    messages = [{"role": r["role"], "content": r["content"]} for r in rows]
    if len(messages) > MAX_HISTORY:
        messages = messages[-MAX_HISTORY:]
    return messages


def create_lead(phone: str, name: str = None):
    conn = _db()
    c = conn.cursor()
    now = datetime.now().isoformat()
    lead_id = f"wa_{phone}"
    c.execute(
        """INSERT INTO leads (id, phone, name, source, created_at, updated_at)
           VALUES (?, ?, ?, 'whatsapp', ?, ?)
           ON CONFLICT(id) DO UPDATE SET name=COALESCE(?, name), updated_at=?""",
        (lead_id, phone, name, now, now, name, now)
    )
    conn.commit()
    conn.close()
    return lead_id


def add_message(lead_id: str, role: str, content: str):
    conn = _db()
    c = conn.cursor()
    c.execute("INSERT INTO messages (lead_id, role, content, ts) VALUES (?, ?, ?, ?)",
              (lead_id, role, content, int(time.time())))
    conn.commit()
    conn.close()


def mark_checkout_sent(lead_id: str):
    conn = _db()
    c = conn.cursor()
    c.execute("UPDATE leads SET sent_checkout=1, updated_at=? WHERE id=?",
              (datetime.now().isoformat(), lead_id))
    conn.commit()
    conn.close()


def schedule_followup(lead_id: str, phone: str, name: str = None):
    """Agenda follow-up 2h após envio do link de checkout."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO followups (lead_id, phone, name, checkout_sent_at, followup_sent)
           VALUES (?, ?, ?, ?, 0)
           ON CONFLICT(lead_id) DO UPDATE SET checkout_sent_at=excluded.checkout_sent_at, followup_sent=0""",
        (lead_id, phone, name, int(time.time()))
    )
    conn.commit()
    conn.close()


def get_pending_followups() -> list:
    """Retorna leads que receberam o link há mais de FOLLOWUP_DELAY e ainda não tiveram follow-up.
    Leads pausados (humano assumiu) ficam fora da fila."""
    conn = _db()
    c = conn.cursor()
    cutoff = int(time.time()) - FOLLOWUP_DELAY
    c.execute(
        """SELECT f.lead_id, f.phone, f.name FROM followups f
           LEFT JOIN leads l ON l.id = f.lead_id
           WHERE f.followup_sent = 0 AND f.checkout_sent_at <= ?
             AND COALESCE(l.paused, 0) = 0""",
        (cutoff,)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def mark_followup_sent(lead_id: str):
    conn = _db()
    c = conn.cursor()
    c.execute("UPDATE followups SET followup_sent = 1 WHERE lead_id = ?", (lead_id,))
    conn.commit()
    conn.close()


def schedule_price_followup(lead_id: str, phone: str, name: str = None):
    """Agenda follow-up 30min após envio do preço, caso o lead não responda."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO price_followups (lead_id, phone, name, price_sent_at, followup_sent)
           VALUES (?, ?, ?, ?, 0)
           ON CONFLICT(lead_id) DO UPDATE SET price_sent_at=excluded.price_sent_at, name=COALESCE(excluded.name, name), followup_sent=0""",
        (lead_id, phone, name, int(time.time()))
    )
    conn.commit()
    conn.close()


def cancel_price_followup(lead_id: str):
    """Cancela follow-up de preço pendente (chamado quando o lead responde)."""
    conn = _db()
    c = conn.cursor()
    c.execute("DELETE FROM price_followups WHERE lead_id = ? AND followup_sent = 0", (lead_id,))
    conn.commit()
    conn.close()


def get_pending_price_followups() -> list:
    """Leads que receberam o preço há ≥30min e ainda não responderam nem receberam followup.
    Leads pausados (humano assumiu) ficam fora da fila."""
    conn = _db()
    c = conn.cursor()
    cutoff = int(time.time()) - PRICE_FOLLOWUP_DELAY
    c.execute(
        """SELECT f.lead_id, f.phone, f.name FROM price_followups f
           LEFT JOIN leads l ON l.id = f.lead_id
           WHERE f.followup_sent = 0 AND f.price_sent_at <= ?
             AND COALESCE(l.paused, 0) = 0""",
        (cutoff,)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def mark_price_followup_sent(lead_id: str):
    conn = _db()
    c = conn.cursor()
    c.execute("UPDATE price_followups SET followup_sent = 1 WHERE lead_id = ?", (lead_id,))
    conn.commit()
    conn.close()


def record_assistant_message(lead_id: str, content: str):
    """Append assistant message to history — usado pelo watcher quando envia
    mensagem de recovery gerada pelo LLM, pra que ela vire parte do histórico."""
    add_message(lead_id, "assistant", content)


# ── Cold outreach templates ──────────────────────────────────────────────────
#
# Lista de variações pra abertura de conversa fria (1ª mensagem manual,
# antes do lead responder qualquer coisa). Mesma intenção em todos
# (saudação → identificação como Sandra → pergunta o nome, alinhado com
# PASO 1 do playbook), mas vocabulário/estrutura/emoji variam pra reduzir
# o padrão de "bot mandando bulk" que WhatsApp e leads detectam.
#
# Uso: nas próximas cold outreaches, em vez de mandar texto fixo,
# importar pick_opener() e enviar o que ele devolver — cada lead vê uma
# mensagem ligeiramente diferente.

OPENING_TEMPLATES = [
    "¡Hola! 😊 Soy la Chef Sandra. ¿Cómo te llamas?",
    "Hola, ¿qué tal? 💚 Soy la Chef Sandra. ¿Cuál es tu nombre?",
    "¡Buenas! Aquí Chef Sandra 🍞 ¿Con quién tengo el gusto?",
    "Hola, amig@ 😊 Soy Sandra, la chef. Antes de seguir, ¿me dices tu nombre?",
    "¡Hola! Soy la Chef Sandra 💚 Para empezar bien, ¿cómo te llamas?",
    "Hola 👋 Soy Sandra. Antes de nada, ¿me dices cómo te llamas?",
]


def pick_opener() -> str:
    """Devolve um opener aleatório de OPENING_TEMPLATES."""
    return random.choice(OPENING_TEMPLATES)


# ── Recoveries (cadência genérica de reengajamento de lead frio) ──────────────
#
# Toda vez que a Sandra responde, agendamos uma tentativa de recovery em
# stage 0 (default 30min). Cliente respondendo cancela. Se ele continuar
# em silêncio, watcher avança stage 1 (4h) e stage 2 (1d). Após stage 2,
# desiste.
#
# Markers que o LLM pode emitir na resposta pra sobrescrever a cadência:
#   [RECOVERY_AT:Ns]  → cliente pediu retorno em N segundos. Override do
#                       schedule (uma tentativa única naquele horário).
#   [RECOVERY_OFF]    → cliente recusou claramente; nunca mais reengaja.
#
# Coexiste com o recover.py legacy (script CLI single-shot diário): quando
# o sistema novo dispara, ele seta leads.daily_recovered_at, e o recover.py
# pula leads que já têm essa coluna preenchida — evita double-message.

def _now_dt_in_tz(ts: int = None):
    ts = ts if ts is not None else int(time.time())
    return datetime.fromtimestamp(ts, tz=ZoneInfo(TIMEZONE))


def _is_quiet_hour(dt: datetime) -> bool:
    qs = RECOVERY_QUIET_HOURS_START
    qe = RECOVERY_QUIET_HOURS_END
    if qs == qe:
        return False
    h = dt.hour
    if qs < qe:
        return qs <= h < qe
    return h >= qs or h < qe


def _next_valid_recovery_time(ts: int) -> int:
    """Empurra ts pra fora da janela de silêncio se necessário."""
    qs = RECOVERY_QUIET_HOURS_START
    qe = RECOVERY_QUIET_HOURS_END
    if qs == qe:
        return ts
    dt = _now_dt_in_tz(ts)
    if not _is_quiet_hour(dt):
        return ts
    target = dt.replace(hour=qe, minute=0, second=0, microsecond=0)
    if qs > qe and dt.hour >= qs:
        target += timedelta(days=1)
    return int(target.timestamp())


def schedule_recovery(lead_id: str, phone: str, name: str = None,
                      override_seconds: int = None, source: str = "auto"):
    if not RECOVERY_STAGES_SECONDS:
        return
    delay = override_seconds if override_seconds is not None else RECOVERY_STAGES_SECONDS[0]
    next_at = _next_valid_recovery_time(int(time.time()) + int(delay))
    now = int(time.time())
    conn = _db()
    c = conn.cursor()
    c.execute(
        """INSERT INTO recoveries (lead_id, phone, name, stage, next_attempt_at,
                                   status, source, created_at, updated_at)
           VALUES (?, ?, ?, 0, ?, 'pending', ?, ?, ?)
           ON CONFLICT(lead_id) DO UPDATE SET
              phone           = excluded.phone,
              name            = COALESCE(excluded.name, name),
              stage           = 0,
              next_attempt_at = excluded.next_attempt_at,
              status          = 'pending',
              source          = excluded.source,
              updated_at      = excluded.updated_at""",
        (lead_id, phone, name, next_at, source, now, now)
    )
    conn.commit()
    conn.close()


def cancel_recovery(lead_id: str):
    conn = _db()
    c = conn.cursor()
    c.execute(
        "UPDATE recoveries SET status='cancelled', updated_at=? "
        "WHERE lead_id=? AND status='pending'",
        (int(time.time()), lead_id)
    )
    conn.commit()
    conn.close()


def mark_recovery_given_up(lead_id: str):
    conn = _db()
    c = conn.cursor()
    c.execute(
        "UPDATE recoveries SET status='given_up', updated_at=? WHERE lead_id=?",
        (int(time.time()), lead_id)
    )
    conn.commit()
    conn.close()


def get_pending_recoveries() -> list:
    """Recoveries que devem disparar agora — exclui leads pausados e leads
    que já confirmaram pagamento (outcome='paid')."""
    now = int(time.time())
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT r.lead_id, r.phone, r.name, r.stage "
        "FROM recoveries r "
        "LEFT JOIN leads l ON l.id = r.lead_id "
        "WHERE r.status = 'pending' "
        "  AND r.next_attempt_at <= ? "
        "  AND COALESCE(l.paused, 0) = 0 "
        "  AND (l.outcome IS NULL OR l.outcome != 'paid')",
        (now,)
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


def advance_recovery(lead_id: str):
    """Após enviar uma tentativa: avança pra próxima stage. Se passou da
    última, marca status='done'. Também seta leads.daily_recovered_at pra
    o recover.py legacy não duplicar o disparo."""
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT stage FROM recoveries WHERE lead_id=?", (lead_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return
    next_stage = int(row["stage"]) + 1
    now = int(time.time())
    if next_stage >= len(RECOVERY_STAGES_SECONDS):
        c.execute(
            "UPDATE recoveries SET status='done', last_attempt_at=?, updated_at=? "
            "WHERE lead_id=?",
            (now, now, lead_id)
        )
    else:
        delay = RECOVERY_STAGES_SECONDS[next_stage]
        next_at = _next_valid_recovery_time(now + int(delay))
        c.execute(
            "UPDATE recoveries SET stage=?, next_attempt_at=?, "
            "  last_attempt_at=?, updated_at=? WHERE lead_id=?",
            (next_stage, next_at, now, now, lead_id)
        )
    # Marca pro recover.py legacy não disparar de novo no mesmo lead
    c.execute("UPDATE leads SET daily_recovered_at=? WHERE id=? AND daily_recovered_at IS NULL",
              (now, lead_id))
    conn.commit()
    conn.close()


# Markers que o LLM pode emitir dentro da resposta:
_RECOVERY_AT_PATTERN  = re.compile(r'\[RECOVERY_AT:\s*(\d+)\s*s?\s*\]', re.IGNORECASE)
_RECOVERY_OFF_PATTERN = re.compile(r'\[RECOVERY_OFF\]', re.IGNORECASE)
_RECOVERY_AT_MIN_S    = 60
_RECOVERY_AT_MAX_S    = 30 * 86400  # 30 dias — teto de sanidade


def _extract_recovery_marker(response: str):
    """Retorna (resposta_limpa, kind, value):
        kind ∈ {None, 'at', 'off'}
        value = delay_s quando kind=='at', None caso contrário
    OFF tem prioridade sobre AT se ambos aparecerem."""
    if not response:
        return response, None, None
    cleaned = response
    if _RECOVERY_OFF_PATTERN.search(cleaned):
        cleaned = _RECOVERY_OFF_PATTERN.sub('', cleaned).strip()
        cleaned = _RECOVERY_AT_PATTERN.sub('', cleaned).strip()
        return cleaned, 'off', None
    m = _RECOVERY_AT_PATTERN.search(cleaned)
    if m:
        delay = int(m.group(1))
        delay = max(_RECOVERY_AT_MIN_S, min(delay, _RECOVERY_AT_MAX_S))
        cleaned = _RECOVERY_AT_PATTERN.sub('', cleaned).strip()
        return cleaned, 'at', delay
    return cleaned, None, None


def generate_recovery_message(lead_id: str, name: str, stage: int) -> str:
    """LLM gera mensagem curta de reengajamento contextual ao histórico.
    Em español — mercado de Chef Sandra. Retorna None se não tiver
    histórico ou se a chamada falhar."""
    history = load_session(lead_id)
    if not history:
        return None
    profile_slug = get_lead_profile(lead_id)
    base_system = build_system_prompt(profile_slug=profile_slug)
    stage_label = (
        "30 minutos" if stage == 0 else
        "4 horas"    if stage == 1 else
        "1 día"
    )
    instr = (
        "\n\n════════════════════════════════════════\n"
        "[CONTEXTO — REENGANCHE DE LEAD FRÍO]\n"
        "════════════════════════════════════════\n"
        f"El cliente dejó de responder. Esta es la tentativa de reenganche "
        f"(stage={stage}: {stage_label} desde el último mensaje). El cliente NO confirmó pago, "
        "NO compró, y NO terminó el funnel — solo se quedó callado.\n"
        "\n"
        "REGLAS POSITIVAS:\n"
        "1. Manda UN solo mensaje corto (máx 2 líneas), cálido, sin repetir literalmente lo que ya dijiste.\n"
        "2. Retoma un punto ESPECÍFICO de la conversación (un alimento que extrañe, persona para quien busca, "
        "preocupación concreta como glucosa/peso/familia, una duda no resuelta). NO uses plantillas genéricas "
        "tipo '¿cómo va todo?' / '¿estás ahí?' / '¿pudiste completar la compra?'.\n"
        "3. Tono humano, paciente, curioso. NO insistas.\n"
        "\n"
        "🛑 PROHIBICIONES ABSOLUTAS (fallar en cualquiera de estas es un BUG de producción):\n"
        "❌ NUNCA agradezcas la compra ni uses tono post-venta. PROHIBIDO emitir frases tipo "
        "'¡qué alegría por tu compra!' / 'mil gracias por tu compra' / 'espero que disfrutes las recetas' / "
        "'gracias por confiar en mí' / cualquier variación que sugiera que el lead pagó. El lead NO pagó. "
        "Esto pasó en producción (bug 11/05): la recovery stage 1 felicitó por una compra que nunca ocurrió.\n"
        "❌ NUNCA mandes link de pago (Hotmart o cualquier otro URL de checkout) ni datos de pago "
        "(clave Pix, CPF, Wise email/routing/account). Si ya mandaste link antes en el historial, NO lo repitas. "
        "Recovery NO es para reenviar links — es para reabrir la conversación. Esto pasó en producción "
        "(bug 11/05): la recovery mandó el mismo link Hotmart 3 veces al mismo lead.\n"
        "❌ NUNCA saludes como si fuera el PRIMER mensaje ('¡Hola! Soy la Chef Sandra. ¿Cómo te llamas?'). "
        "El lead ya te conoce — eso ya está en el historial. Retoma desde donde quedó. Esto pasó en producción "
        "(bug 11/05): la recovery se presentó de nuevo del cero, perdiendo todo el rapport.\n"
        "❌ NUNCA emitas markers ([[ENVIAR_LIBROS]], [[ENVIAR_PRUEBA_*]], [[ENVIAR_CLAVE_PIX]], "
        "[[ENVIAR_WISE_*]], [RECOVERY_*], [DELAYED_REPLY], [HANDOFF], [ASK_OWNER]). Esos markers son del "
        "flujo principal — en recovery NO se procesan, salen como texto literal al cliente y queda lixo "
        "tipo '[[ENVIAR_LIBROS]]' en el chat. Esto pasó en producción (bug 11/05).\n"
        "❌ NUNCA presentes el precio de nuevo ('los 5 libros por $9.90 USD') ni la lista de métodos de pago. "
        "Si el lead quiere saber, va a preguntar — ahí responde el flujo normal en el próximo turno.\n"
        "❌ NO uses prólogos tipo 'Aquí va un mensaje:' — devuelve SOLO el texto al cliente.\n"
        "\n"
        "Ejemplos buenos (retoma punto específico, tono leve):\n"
        "  • 'Pensando en lo que me dijiste de tu marido, Marcia — ¿alguna duda con los libros que te frene?'\n"
        "  • '¿Pudiste mirar los panes que te quedan más cómodos, Gladys? Si surgió alguna duda, acá estoy 💚'\n"
        "  • 'Sé que estás cuidando a tu mamá, Lucía — cualquier cosa que dificulte avanzar, contame 💚'\n"
        "\n"
        "Si el historial NO tiene contexto suficiente para personalizar (conversación muy corta o sin detalles), "
        "manda algo neutro pero NUNCA repitas saludo de inicio. Ejemplo: '¿Alguna duda que pueda aclararte, "
        "[nombre]? Acá sigo 💚'"
    )
    try:
        return call_ai(history, system=base_system + instr, max_tokens=200)
    except Exception as e:
        _logger.error(f"Falha ao gerar recovery msg pra {lead_id}: {e}")
        return None


def get_sales_stats() -> str:
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM leads")
    total_leads = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) as n FROM leads WHERE sent_checkout = 1")
    with_checkout = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) as n FROM followups WHERE followup_sent = 1")
    followups_done = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) as n FROM followups WHERE followup_sent = 0")
    followups_pending = c.fetchone()["n"]
    c.execute("SELECT COUNT(*) as n FROM messages WHERE role = 'user'")
    total_msgs = c.fetchone()["n"]
    # Leads mais recentes
    c.execute("SELECT name, phone, created_at FROM leads ORDER BY created_at DESC LIMIT 5")
    recent = c.fetchall()
    conn.close()

    recent_lines = "\n".join(
        f"  • {r['name'] or 'sem nome'} ({r['phone']}) — {r['created_at'][:16]}"
        for r in recent
    ) or "  (nenhum)"

    return (
        f"Total de leads: {total_leads}\n"
        f"Receberam link de checkout: {with_checkout}\n"
        f"Follow-ups enviados: {followups_done}\n"
        f"Follow-ups pendentes: {followups_pending}\n"
        f"Total de mensagens recebidas: {total_msgs}\n"
        f"Leads mais recentes:\n{recent_lines}"
    )


# ── Lógica do agente ──────────────────────────────────────────────────────────

def is_trigger(text: str) -> bool:
    return bool(text and text.strip())


def is_owner(phone: str) -> bool:
    p = phone.replace("+", "").replace(" ", "").replace("-", "")
    return p in (OWNER_PHONE, OWNER_PHONE.replace("554499", "55449"))


def is_payment_confirmation(text: str) -> bool:
    """True só se o texto afirma pagamento em pretérito 1ª pessoa E não
    contém marcadores de intenção/futuro/3ª pessoa. Caso real Mónica
    +598 17/05/2026: 'le pasé a mi hijo para que él pague el PIX' não é
    confirmação — o filho ainda vai pagar."""
    t = text.lower()
    if any(neg in t for neg in PAYMENT_INTENT_NEGATIVE):
        return False
    return any(kw in t for kw in PAYMENT_KEYWORDS)


def is_awaiting_files(text: str) -> bool:
    """Lead pedindo os arquivos depois do checkout. Use em conjunto com
    _checkout_already_sent para evitar disparo no início do funil."""
    t = text.lower()
    return any(kw in t for kw in AWAITING_FILES_KEYWORDS)


_PRICE_PATTERN = re.compile(r'\$\s?(?:5\.00|6\.90|7\.90|9\.90|12\.90)')


def _response_mentions_tier_price(text: str) -> bool:
    """Detecta se a resposta menciona um dos preços de PASO 5
    ($6.90/$7.90/$9.90/$12.90 default, $5.00 oferta de objeção)."""
    return bool(_PRICE_PATTERN.search(text))


# Sinais de que a última mensagem do bot já foi uma despedida.
_ASSISTANT_FAREWELL_MARKERS = (
    "que tengas", "que tengan", "lindo día", "lindo dia",
    "linda noche", "linda tarde", "buen día", "buen dia",
    "buenas noches", "hasta pronto", "hasta luego",
    "estoy aquí", "estoy aqui", "para lo que necesites",
    "cuando estés list", "cuando puedas me", "te espero",
    "cuídate", "cuidate",
)

# Vocabulário de mensagens puramente de cortesia/encerramento.
_COURTESY_VOCAB = {
    "gracias", "muchas", "muchisimas", "muchísimas", "mil",
    "ok", "okay", "vale", "bueno", "claro", "perfecto", "genial",
    "chau", "chao", "adios", "adiós", "bye", "hasta", "pronto", "luego",
    "igual", "igualmente", "para", "ti", "tí", "vos", "tu", "tú", "usted",
    "lindo", "linda", "lindas", "lindos", "bonito", "bonita",
    "buen", "buena", "buenas", "buenos",
    "día", "dia", "noche", "noches", "tarde", "tardes", "mañana",
    "que", "tengas", "tenga", "tengan",
    "bendiciones", "bendecida", "bendecido", "bendiga",
    "saludos", "abrazo", "abrazos", "beso", "besos",
    "sandra", "chef", "señora", "doña", "amiga", "querida",
    "y", "a", "de", "con", "en", "el", "la",
    "si", "sí", "no", "te", "me",
}


def _is_courtesy_close(lead_text: str, last_assistant_text: str) -> bool:
    """True quando o bot já se despediu e o lead só está retribuindo cortesia
    ('gracias', 'igualmente', 'lindo día', emojis). Nesses casos NÃO respondemos
    — não monopolizamos a última palavra, evita loop de despedidas."""
    if not lead_text or not last_assistant_text:
        return False
    asst_lower = last_assistant_text.lower()
    if not any(m in asst_lower for m in _ASSISTANT_FAREWELL_MARKERS):
        return False
    # Normaliza: tira pontuação/emojis e dígitos, sobra só palavras.
    cleaned = re.sub(r"[^\w\s]", " ", lead_text.lower(), flags=re.UNICODE)
    cleaned = re.sub(r"\d+", " ", cleaned)
    words = [w for w in cleaned.split() if w]
    if not words:
        # só emojis depois de despedida → também é fechamento
        return True
    if len(words) > 10:
        return False
    return all(w in _COURTESY_VOCAB for w in words)


def _checkout_already_sent(lead_id: str) -> bool:
    """Verifica se o link de checkout já foi enviado para este lead."""
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT sent_checkout FROM leads WHERE id = ?", (lead_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row["sent_checkout"])


def books_already_delivered(lead_id: str) -> bool:
    """Verifica se os PDFs dos livros já foram enviados para este lead."""
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT books_delivered FROM leads WHERE id = ?", (lead_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row["books_delivered"])


def mark_books_delivered(lead_id: str):
    """Marca que os 5 PDFs já foram entregues — evita reentrega em mensagens
    subsequentes ('ya pagué', 'gracias', etc.) do mesmo lead."""
    conn = _db()
    c = conn.cursor()
    now = int(time.time())
    c.execute(
        "UPDATE leads SET books_delivered = 1, books_delivered_at = ?, "
        "updated_at = ? WHERE id = ?",
        (now, datetime.now().isoformat(), lead_id)
    )
    conn.commit()
    conn.close()


def reset_lead_delivery_state(lead_id: str):
    """Limpa flags que decidem auto-entrega: books_delivered, sent_checkout,
    image_alert_sent. Usado quando o dono entra em /teste pra cada sessão de
    teste começar limpa, e em troubleshooting de leads."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "UPDATE leads SET books_delivered = 0, books_delivered_at = NULL, "
        "sent_checkout = 0, image_alert_sent = 0, updated_at = ? "
        "WHERE id = ?",
        (datetime.now().isoformat(), lead_id)
    )
    conn.commit()
    conn.close()


def has_image_alert_been_sent(lead_id: str) -> bool:
    """True se já avisamos o dono uma vez sobre imagem inesperada deste lead.
    Usado pra não spammar — só alertamos uma vez por lead."""
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT image_alert_sent FROM leads WHERE id = ?", (lead_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row["image_alert_sent"])


def mark_image_alert_sent(lead_id: str):
    """Marca que já notificamos o dono sobre imagem deste lead sem checkout."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "UPDATE leads SET image_alert_sent = 1, updated_at = ? WHERE id = ?",
        (datetime.now().isoformat(), lead_id)
    )
    conn.commit()
    conn.close()


def _normalize_phone(raw: str) -> str:
    """Extrai apenas dígitos do que o dono digitou. Aceita formatos como
    '+55 44 9720-8122', '5544972081 22', '4497208122', etc."""
    return re.sub(r"\D", "", raw or "")


def _find_lead_by_phone(phone_digits: str) -> dict:
    """Busca lead pelo número digitado. Tenta match exato, depois sufixo
    (caso o dono tenha digitado sem o 55 inicial). Retorna dict com id/name/phone
    ou None."""
    if not phone_digits:
        return None
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT id, name, phone, paused FROM leads WHERE phone = ?", (phone_digits,))
    row = c.fetchone()
    if not row:
        c.execute(
            "SELECT id, name, phone, paused FROM leads WHERE phone LIKE ? ORDER BY length(phone) ASC",
            (f"%{phone_digits}",)
        )
        row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def _lookup_lead_name(lead_id: str) -> str:
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT name FROM leads WHERE id = ?", (lead_id,))
    row = c.fetchone()
    conn.close()
    return (row["name"] if row and row["name"] else "") or ""


def is_lead_paused(lead_id: str) -> bool:
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT paused FROM leads WHERE id = ?", (lead_id,))
    row = c.fetchone()
    conn.close()
    return bool(row and row["paused"])


def _set_lead_paused(lead_id: str, paused: bool):
    conn = _db()
    c = conn.cursor()
    c.execute("UPDATE leads SET paused = ?, updated_at = ? WHERE id = ?",
              (1 if paused else 0, datetime.now().isoformat(), lead_id))
    conn.commit()
    conn.close()


def list_paused_leads() -> list:
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT id, name, phone FROM leads WHERE paused = 1 ORDER BY updated_at DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows


_OWNER_HELP = (
    "🛠️ *Comandos do owner*\n"
    "\n"
    "*🧪 Modo teste*\n"
    "*/teste*\n"
    "   Eu te respondo como se você fosse cliente, seguindo o funil completo.\n"
    "   A conversa NÃO é salva no banco.\n"
    "*/sair*\n"
    "   Sai do modo teste e volta pro modo gerenciador.\n"
    "\n"
    "*⏸️ Pausar leads*\n"
    "*/pausar* _<número>_\n"
    "   Eu paro de responder esse lead (você assume).\n"
    "*/voltar* _<número>_\n"
    "   Eu retomo a conversa com esse lead.\n"
    "*/pausados*\n"
    "   Lista todos os leads pausados.\n"
    "\n"
    "_Aceita o número em qualquer formato:_ +55 44 9720-8122, 5544972081 22, etc.\n"
    "\n"
    "*ℹ️ Ajuda*\n"
    "*/comandos*\n"
    "   Mostra esta lista."
)


# ── Modo teste do owner (em memória, não persiste no banco) ───────────────────
_owner_test_mode    = False
_owner_test_session = []  # lista de {"role", "content"}


def is_owner_test_mode() -> bool:
    return _owner_test_mode


def _set_owner_test_mode(enabled: bool):
    global _owner_test_mode
    _owner_test_mode = enabled
    if not enabled:
        _owner_test_session.clear()


def _handle_owner_test_message(text: str) -> str:
    """Roda o fluxo do cliente em memória, sem tocar no banco. Markers de
    mídia ([[ENVIAR_LIBROS]] etc) funcionam normalmente porque o watcher
    intercepta a resposta."""
    _owner_test_session.append({"role": "user", "content": text})
    if len(_owner_test_session) > MAX_HISTORY:
        del _owner_test_session[:-MAX_HISTORY]
    response = call_ai(list(_owner_test_session), profile_slug=None)
    response = strip_placeholders(response, lead_name=None)
    _owner_test_session.append({"role": "assistant", "content": response})
    return response


def handle_owner_command(text: str) -> str:
    """Parser de comandos do dono. Retorna a resposta a enviar de volta.
    Se o texto não for comando reconhecido, retorna None (cai no modo IA)."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower().lstrip("/")
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("ajuda", "help", "comandos"):
        return _OWNER_HELP

    if cmd in ("teste", "test"):
        _set_owner_test_mode(True)
        # Cada /teste começa do zero: limpa flags de entrega que possam ter
        # ficado de testes anteriores (books_delivered, sent_checkout, etc).
        # Sem isso, o segundo /teste pula a auto-entrega de PDFs porque o
        # banco ainda mostra books_delivered=1 da sessão anterior.
        # IMPORTANTE: o WhatsApp pode entregar a mensagem do owner com phone
        # de 12 dígitos (formato BR antigo: 554497317509) ou 13 (com o 9
        # extra: 5544997317509). is_owner() aceita os dois, então o lead
        # acaba sendo gravado num OU noutro id. Resetamos AMBOS pra garantir
        # que a próxima mensagem do owner caia num lead limpo.
        reset_lead_delivery_state(f"wa_{OWNER_PHONE}")
        reset_lead_delivery_state(f"wa_{OWNER_PHONE.replace('554499', '55449')}")
        return ("🧪 Modo teste ON — vou te responder como se você fosse cliente, "
                "seguindo o funil completo. A conversa NÃO é salva no banco.\n"
                "Manda 'Hola' (ou qualquer coisa) pra começar. /sair pra voltar ao modo gerenciador.")

    if cmd in ("sair", "fim", "encerrar"):
        if not is_owner_test_mode():
            return "ℹ️  Você não estava em modo teste."
        _set_owner_test_mode(False)
        return "✅ Modo teste OFF — voltei pro modo gerenciador. Conversa de teste descartada."

    if cmd in ("pausados", "listar"):
        leads = list_paused_leads()
        if not leads:
            return "✅ Nenhum lead pausado no momento."
        lines = [f"⏸️  {len(leads)} lead(s) pausado(s):"]
        for l in leads:
            lines.append(f"  • {l['name'] or 'sem nome'} — +{l['phone']}")
        return "\n".join(lines)

    if cmd in ("pausar", "voltar", "retomar", "assumir", "despausar"):
        digits = _normalize_phone(arg)
        if not digits:
            return f"❌ Faltou o número. Ex: /{cmd} +55 44 9720-8122"
        lead = _find_lead_by_phone(digits)
        if not lead:
            return f"❌ Lead não encontrado com o número {arg.strip()}."

        target_paused = cmd in ("pausar", "assumir")
        was_paused    = bool(lead["paused"])
        nome = lead["name"] or "sem nome"
        fone = lead["phone"]

        if target_paused and was_paused:
            return f"ℹ️  {nome} (+{fone}) já estava pausado."
        if not target_paused and not was_paused:
            return f"ℹ️  {nome} (+{fone}) já estava ativo."

        _set_lead_paused(lead["id"], target_paused)
        if target_paused:
            return f"⏸️  Pausado: {nome} (+{fone}). Followups suspensos. /voltar quando quiser que eu retome."
        return f"▶️  Retomei: {nome} (+{fone}). Já volto a responder se ele(a) escrever."

    return None  # comando desconhecido → cai no modo IA gerencial


def handle_owner_message(phone: str, text: str) -> str:
    """Modo gerencial — comandos explícitos primeiro, IA com stats como fallback."""
    cmd_response = handle_owner_command(text)
    if cmd_response is not None:
        return cmd_response

    lead_id = f"owner_{phone}"
    messages = load_session(lead_id)
    messages.append({"role": "user", "content": text})
    add_message(lead_id, "user", text)
    stats = get_sales_stats()
    owner_prompt = OWNER_SYSTEM_PROMPT + f"\n\n[DADOS DE VENDAS ATUAIS]\n{stats}"
    response = call_ai(messages, system=owner_prompt)
    add_message(lead_id, "assistant", response)
    return response


def handle_message(phone: str, sender_name: str, text: str) -> str:
    if is_owner(phone):
        # Comandos têm precedência absoluta — /teste, /sair, /pausar, etc.
        if text.lstrip().startswith("/"):
            return handle_owner_message(phone, text)
        # Modo teste ON: dono virou "cliente" — fluxo do funil em memória, sem DB.
        if is_owner_test_mode():
            return _handle_owner_test_message(text)
        # Modo gerenciador padrão (mensagem livre)
        if is_trigger(text):
            return handle_owner_message(phone, text)
        return None

    if not is_trigger(text):
        return None

    lead_id  = create_lead(phone, name=sender_name)

    # Lead pausado (humano assumiu): registra a mensagem na história mas não responde.
    if is_lead_paused(lead_id):
        add_message(lead_id, "user", text)
        return None

    cancel_price_followup(lead_id)  # lead respondeu — cancela follow-up de preço pendente
    cancel_recovery(lead_id)        # lead respondeu — cancela recovery pendente
    messages = load_session(lead_id)

    # Fechamento de cortesia: se o bot já se despediu e o lead só retribui
    # ("gracias", "igualmente", "lindo día"), registramos a mensagem mas NÃO
    # respondemos. Evita o loop "obrigada / de nada / igual pra você / gracias…"
    last_assistant = next(
        (m["content"] for m in reversed(messages) if m["role"] == "assistant"),
        ""
    )
    if _is_courtesy_close(text, last_assistant):
        add_message(lead_id, "user", text)
        return None

    messages.append({"role": "user", "content": text})
    add_message(lead_id, "user", text)

    # Fase 2: classificador roda nas mensagens do gate (2ª msg do user, e a
    # cada 5 msgs novas). Se não há perfil ativo, é no-op sem custo.
    maybe_classify(lead_id, messages)

    # Fase 1: profile_slug entra como bloco PERFIL DETECTADO no system prompt
    # quando build_system_prompt o recebe.
    profile_slug = get_lead_profile(lead_id)
    response = call_ai(messages, profile_slug=profile_slug)

    # Camada defensiva: se o LLM vazou [tu nombre] etc, substitui pelo nome do
    # lead (do banco) ou apaga o placeholder.
    lead_name = _lookup_lead_name(lead_id) or sender_name
    response = strip_placeholders(response, lead_name=lead_name)

    # Markers de recovery: cliente pediu retorno em horário específico
    # ([RECOVERY_AT:Ns]) ou recusou contato ([RECOVERY_OFF]). Removidos da
    # mensagem antes de ir pro cliente. OFF prevalece sobre AT. Os markers
    # ficam stash em pending_recovery_actions (SQLite) pra commit_response
    # consumir após envio confirmado — sobrevive a restart do watcher.
    response, rec_kind, rec_value = _extract_recovery_marker(response)
    _set_pending_recovery_action(phone, rec_kind, rec_value)

    # IMPORTANTE: o histórico do assistant e os side-effects (mark_checkout,
    # schedule_followup, schedule_recovery) são gravados pelo watcher SÓ
    # depois do envio confirmado, via commit_response(). Isso evita histórico
    # fora de sincronia quando a Evolution API falha no envio.
    return response


# Stash durável dos markers de recovery extraídos em handle_message —
# consumido por commit_response após o envio. Persistido em SQLite (tabela
# pending_recovery_actions) pra sobreviver a restart do watcher na janela
# curta (~1-3s) entre handle_message e commit_response. Housekeeping em
# init_db() limpa entradas órfãs > 1h.

def _set_pending_recovery_action(phone: str, kind, value):
    """INSERT OR REPLACE da action pendente. `kind` é None|'off'|'at';
    `value` é int (segundos override pro stage 'at') ou None. Chave: phone."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO pending_recovery_actions "
        "(phone, kind, value, created_at) VALUES (?, ?, ?, ?)",
        (phone, kind, value, int(time.time()))
    )
    conn.commit()
    conn.close()


def _pop_pending_recovery_action(phone: str):
    """SELECT + DELETE atômico. Retorna (kind, value) ou (None, None) se
    não houver entrada. Usa transação implícita do sqlite3 (begin no SELECT
    via cursor compartilhado, commit no final) — mesma conexão pros dois
    statements garante isolamento contra outro pop concorrente."""
    conn = _db()
    c = conn.cursor()
    c.execute(
        "SELECT kind, value FROM pending_recovery_actions WHERE phone = ?",
        (phone,)
    )
    row = c.fetchone()
    if row is None:
        conn.close()
        return (None, None)
    c.execute("DELETE FROM pending_recovery_actions WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()
    return (row["kind"], row["value"])


def commit_response(phone: str, sender_name: str, response: str):
    """Chamado pelo watcher APÓS confirmação de envio. Persiste a resposta no
    histórico e faz side-effects mínimos (mark_checkout). No-op para owner
    ou leads pausados.

    NOTA: agendamento de followups (link/preço) e de recovery foi REMOVIDO
    em 12/05/2026 a pedido do owner — número da Sandra foi restrito pelo
    WhatsApp por volume e o owner decidiu que esse fluxo NÃO terá mais
    mensagens automáticas proativas. As funções schedule_followup,
    schedule_price_followup, schedule_recovery e os helpers de cancel
    seguem definidos no módulo (não foram apagados pra preservar histórico),
    mas NÃO são chamados daqui. O kill switch DISPATCH_ENABLED no watcher
    é defesa em camadas caso alguém reintroduza calls."""
    # Sempre limpa qualquer action pendente do pop pra não acumular lixo
    # mesmo agora que não agendamos recovery — markers do LLM seguem sendo
    # extraídos pra não vazar no texto enviado ao cliente.
    _pop_pending_recovery_action(phone)
    if not response:
        return
    if is_owner(phone):
        return  # owner não tem fluxo de followup; modo teste é em memória
    lead_id = f"wa_{phone}"
    if is_lead_paused(lead_id):
        return  # lead pausado: humano assumiu
    add_message(lead_id, "assistant", response)

    # Marca checkout enviado pra liberar triggers pós-pago (entrega de
    # PDFs etc) — não confunde com followup, que está desativado.
    if any(link in response for link in ALL_CHECKOUTS):
        if not _checkout_already_sent(lead_id):
            mark_checkout_sent(lead_id)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Agente Chef Sandra")
    parser.add_argument("--test", action="store_true", help="Testa triggers")
    parser.add_argument("--chat", type=str,            help="Chat interativo com phone")
    args = parser.parse_args()

    init_db()

    if args.test:
        print("Testando triggers:\n")
        for msg in [TRIGGER_EXACT, "recetas para diabéticos", "Olá tudo bem"]:
            r = "✅ Detectado" if is_trigger(msg) else "❌ Ignorado"
            print(f'  "{msg}" → {r}')

    elif args.chat:
        print(f"\n💬 Chat com {args.chat} (digite 'sair' para encerrar)\n")
        while True:
            msg = input("Você: ").strip()
            if msg.lower() == "sair":
                break
            resp = handle_message(args.chat, "Teste", msg)
            print(f"Chef Sandra: {resp}\n" if resp else "(não é trigger)\n")
    else:
        print("Use: python3 agent.py --test")
        print("     python3 agent.py --chat PHONE")


if __name__ == "__main__":
    main()
