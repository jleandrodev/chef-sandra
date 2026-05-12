#!/usr/bin/env python3
"""
watcher.py — Monitora WhatsApp e ativa agente Chef Sandra
Suporta mensagens de texto e áudio (transcrição via Whisper)
Execução: python3 watcher.py
"""

import json
import time
import logging
import signal
import traceback
import urllib.request
import urllib.error
import tempfile
import os
import base64
from pathlib import Path
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path.home() / "chef-sandra" / "watcher.log")
    ]
)
logger = logging.getLogger(__name__)

import sys
sys.path.insert(0, str(Path.home() / "chef-sandra"))
from agent import (handle_message, commit_response, init_db, AI_API_KEY,
                   get_pending_followups, mark_followup_sent,
                   get_pending_price_followups, mark_price_followup_sent,
                   is_payment_confirmation, is_awaiting_files,
                   _checkout_already_sent, books_already_delivered,
                   mark_books_delivered, add_message, mark_checkout_sent,
                   has_image_alert_been_sent, mark_image_alert_sent,
                   is_lead_paused,
                   get_pending_recoveries, advance_recovery,
                   generate_recovery_message, record_assistant_message,
                   _is_quiet_hour, _now_dt_in_tz,
                   OWNER_PHONE,
                   _safe_first_name, _extract_name_from_history)
from book_presentation import MEDIA_DISPATCH, CONTENT_DIR

# ── Configuração ──────────────────────────────────────────────────────────────
EVOLUTION_URL     = "http://localhost:8080"
EVOLUTION_API_KEY = "aqYCBaeh-k_UL6-nbj0kKaKQxDSKkoPEi6rbBvtFsFY"
INSTANCE_NAME     = "meu-agente"
POLL_INTERVAL     = 3

STATE_FILE = Path.home() / "chef-sandra" / "watcher_state.json"


# ── Evolution API ─────────────────────────────────────────────────────────────

def evolution_request(endpoint: str, method: str = "GET", data: dict = None) -> dict:
    url = f"{EVOLUTION_URL}{endpoint}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data else None,
        headers=headers,
        method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.error(f"Evolution API erro: {e}")
        return {}


def fetch_messages(count: int = 20) -> list:
    result = evolution_request(
        f"/chat/findMessages/{INSTANCE_NAME}",
        method="POST",
        data={"count": count}
    )
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and "messages" in result:
        m = result["messages"]
        if isinstance(m, dict):
            return m.get("records", [])
        if isinstance(m, list):
            return m
    return []


def notify_owner_sale(client_name: str, client_phone: str, trigger: str):
    """Comemora uma venda realizada — os 5 PDFs já foram entregues automaticamente.
    `trigger` indica o sinal que disparou a entrega (pra log/auditoria)."""
    msg = (
        f"🎉🎊 *VENDA REALIZADA!* 🎊🎉\n\n"
        f"Cliente: {client_name or 'sem nome'}\n"
        f"Telefone: +{client_phone}\n\n"
        f"Os 5 livros foram entregues automaticamente aqui no WhatsApp. "
        f"Mais um cliente feliz! 💚📚"
    )
    send_whatsapp(OWNER_PHONE, msg)
    logger.info(f"🎉 VENDA REALIZADA — {client_phone} ({trigger})")


def notify_owner_sale_partial_failure(client_name: str, client_phone: str,
                                      trigger: str, failed_books: list):
    """Venda registrada mas algum PDF falhou no envio depois dos retries.
    Avisa o dono com a lista de arquivos que precisam ir manualmente."""
    failed_list = ", ".join(failed_books)
    msg = (
        f"⚠️ *VENDA REALIZADA — com falha parcial no envio*\n\n"
        f"Cliente: {client_name or 'sem nome'}\n"
        f"Telefone: +{client_phone}\n\n"
        f"Os seguintes PDFs FALHARAM mesmo após retry e precisam ser enviados "
        f"manualmente: {failed_list}"
    )
    send_whatsapp(OWNER_PHONE, msg)
    logger.warning(f"⚠️  VENDA com falha parcial — {client_phone}: {failed_list}")


# ── Prex / PIX ────────────────────────────────────────────────────────────────
# Quando o lead confirma que paga por Prex, a Sandra emite o marker abaixo.
# O watcher envia a chave PIX sozinha numa mensagem isolada (fácil de copiar)
# e marca sent_checkout=1 — assim os triggers pós-pago (awaiting_files /
# image_proof) passam a funcionar mesmo sem link de Kiwify.

CLAVE_PIX_MARKER = "[[ENVIAR_CLAVE_PIX]]"
# Duas chaves Pix do mesmo dono (Jhonatan Leandro Ozório). O cliente, de
# dentro do app Prex (Uruguai/região), entra em "Transferir a Brasil" /
# "Pago con Pix" e usa qualquer uma delas. PIX_KEY é a chave aleatória
# (UUID); CPF_PREX é o CPF brasileiro do dono cadastrado como chave Pix.
PIX_KEY          = "94ba8d14-4299-419e-b14a-8d969da44136"
CPF_PREX         = "07994518940"


def notify_owner_pix_sent(client_name: str, client_phone: str):
    """Lead confirmou Prex: chave PIX foi enviada. Owner precisa conferir
    chegada do pagamento manualmente (Prex não tem webhook como Kiwify)."""
    msg = (
        f"💸 *CHAVE PIX (Prex) ENVIADA*\n\n"
        f"Cliente: {client_name or 'sem nome'}\n"
        f"Telefone: +{client_phone}\n\n"
        f"O lead confirmou que paga por Prex e recebeu a chave PIX. "
        f"Confira o recebimento na conta — quando o lead disser que pagou, "
        f"os 5 PDFs são entregues automaticamente."
    )
    send_whatsapp(OWNER_PHONE, msg)
    logger.info(f"💸 PIX/Prex chave enviada — {client_phone} ({client_name or 'sem nome'})")


# ── Wise USD ──────────────────────────────────────────────────────────────────
# Conta USD na Wise (Wise US Inc, Wilmington DE). Dois caminhos:
#   - Wise app (lead tem Wise) → Wise→Wise via routing+account, conversão
#     UYU/MXN/etc → USD acontece dentro do app dele, tarifa de centavos.
#   - Wire bancário (lead sem Wise) → banco comum cobra $20-50 de tarifa,
#     bem mais caro que o livro. Avisamos no pre-text e ainda assim mandamos
#     se ele insistir, pra não travar a venda.
# Sistema espera USD; cliente paga em moeda local dentro do app (Wise faz
# a conversão). Se mandar moeda local pros dados USA o banco pode rejeitar.

WISE_APP_MARKER   = "[[ENVIAR_WISE_APP]]"
WISE_EMAIL_MARKER = "[[ENVIAR_WISE_EMAIL]]"
WISE_WIRE_MARKER  = "[[ENVIAR_WISE_WIRE]]"

WISE_HOLDER_NAME = "Jonathan Leandro Ozório"
WISE_BANK_NAME   = "Wise US Inc"
WISE_BANK_ADDR   = "108 W 13th St, Wilmington, DE, 19801, United States"
WISE_ROUTING     = "101019628"
WISE_ACCOUNT     = "217059817543"
WISE_SWIFT       = "TRWIUS35XXX"
WISE_ACCT_TYPE   = "Checking"
# Email cadastrado na Wise (no titular Jonathan, conta do filho da Sandra).
# Dentro do app Wise o lead pode mandar pra um contato pelo email — mais
# rápido que copiar routing+account.
WISE_EMAIL       = "jleandro.dev@gmail.com"

# Wire bancário: TUDO num bloco rotulado (campos múltiplos = formulário do
# banco). Lead lê tudo junto e seleciona/copia campo a campo. 6 mensagens
# soltas seria muito barulho pra um caminho que já vai cobrar tarifa cara.
WISE_WIRE_BLOCK = (
    "📋 *Datos para transferencia internacional (USD):*\n\n"
    f"*Beneficiario:* {WISE_HOLDER_NAME}\n"
    f"*Tipo de cuenta:* {WISE_ACCT_TYPE}\n"
    f"*Banco:* {WISE_BANK_NAME}\n"
    f"*Dirección del banco:* {WISE_BANK_ADDR}\n"
    f"*Routing/ABA:* {WISE_ROUTING}\n"
    f"*Número de cuenta:* {WISE_ACCOUNT}\n"
    f"*Swift/BIC:* {WISE_SWIFT}"
)


def notify_owner_wise_sent(client_name: str, client_phone: str, kind: str):
    """Lead recebeu dados de pagamento Wise (app, email ou wire). Owner
    reconcilia manualmente quando o crédito chegar (Wise não tem webhook
    de venda)."""
    label = {
        "app": "Wise app",
        "email": "Wise email",
        "wire": "wire bancário",
    }.get(kind, kind)
    msg = (
        f"💸 *DADOS WISE ENVIADOS — {label}*\n\n"
        f"Cliente: {client_name or 'sem nome'}\n"
        f"Telefone: +{client_phone}\n\n"
        f"O lead recebeu os dados de pagamento via {label}. "
        f"Confira o recebimento na conta — quando o lead disser que pagou, "
        f"os 5 PDFs são entregues automaticamente."
    )
    send_whatsapp(OWNER_PHONE, msg)
    logger.info(f"💸 Wise ({label}) — {client_phone} ({client_name or 'sem nome'})")


def notify_owner_unexpected_image(client_name: str, client_phone: str):
    """Lead enviou imagem mas nunca recebeu link de checkout — pode ser
    comprovante de outro canal (compra direta no site, etc.). Alertamos
    o dono UMA vez por lead pra ele verificar manualmente."""
    msg = (
        f"🖼️ *Imagem recebida — sem checkout enviado*\n\n"
        f"Cliente: {client_name or 'sem nome'}\n"
        f"Telefone: +{client_phone}\n\n"
        f"O lead enviou uma imagem mas ainda não recebeu o link de pagamento "
        f"por aqui. Pode ser comprovante de outro canal — verifique manualmente."
    )
    send_whatsapp(OWNER_PHONE, msg)
    logger.info(f"🖼️  Dono alertado: imagem inesperada de {client_phone}")


def _typing_delay_ms(text: str) -> int:
    """Calcula delay de digitação proporcional ao tamanho da mensagem.
    Simula uma pessoa digitando: mín 1.5s, máx 6s."""
    ms = len(text) * 50
    return max(1500, min(ms, 6000))


def send_whatsapp(phone: str, message: str, attempts: int = 3) -> bool:
    """Envia mensagem de texto com retry + backoff exponencial (2s, 4s).
    Falhas pontuais da Evolution API (timeout, 502) são absorvidas. Retorna
    True se algum attempt teve sucesso. Use attempts=1 para evitar recursão
    em alertas de falha (ver notify_owner_send_failure)."""
    last_result = None
    for attempt in range(attempts):
        result = evolution_request(
            f"/message/sendText/{INSTANCE_NAME}",
            method="POST",
            data={
                "number": phone,
                "text": message,
                "delay": _typing_delay_ms(message),
            }
        )
        last_result = result
        if result.get("key") or result.get("id"):
            logger.info(f"📤 Enviado para {phone}")
            return True
        if attempt < attempts - 1:
            wait = 2 * (attempt + 1)
            logger.warning(f"↻ retry send_whatsapp({phone}) em {wait}s "
                           f"(tentativa {attempt + 2}/{attempts})")
            time.sleep(wait)
    logger.error(f"❌ Falha ao enviar para {phone} após {attempts} tentativas: {last_result}")
    return False


def notify_owner_send_failure(client_name: str, client_phone: str, preview: str):
    """Avisa o dono quando uma resposta da Sandra falhou ao chegar no lead
    mesmo após retries. Usa attempts=1 pra evitar loop caso a Evolution
    API esteja totalmente fora do ar."""
    msg = (
        f"⚠️ *Falha ao responder lead*\n\n"
        f"Cliente: {client_name or 'sem nome'}\n"
        f"Telefone: +{client_phone}\n\n"
        f"A Sandra gerou a resposta mas a Evolution API recusou todas as "
        f"tentativas. O lead está sem resposta no momento.\n\n"
        f"Mensagem que ficou pendente:\n_{preview[:280]}_\n\n"
        f"Tente enviar manualmente pelo WhatsApp."
    )
    send_whatsapp(OWNER_PHONE, msg, attempts=1)


def notify_owner_processing_failure(client_name: str, client_phone: str,
                                    incoming_text: str, error: str):
    """Avisa o dono quando handle_message levantou exceção (OpenAI esgotou
    retry, exception inesperada, etc.) — o lead mandou mensagem e não vai
    receber resposta automática. Owner precisa responder manual."""
    msg = (
        f"⚠️ *Mensagem do lead sem resposta automática*\n\n"
        f"Cliente: {client_name or 'sem nome'}\n"
        f"Telefone: +{client_phone}\n\n"
        f"O lead escreveu mas a Sandra não conseguiu gerar resposta "
        f"(provável falha na OpenAI após retries).\n\n"
        f"Mensagem do lead:\n_{incoming_text[:280]}_\n\n"
        f"Erro: {error[:160]}\n\n"
        f"Responda manualmente pelo WhatsApp."
    )
    send_whatsapp(OWNER_PHONE, msg, attempts=1)


def send_whatsapp_media(phone: str, image_path: Path, caption: str) -> bool:
    """Envia imagem com caption via Evolution API. Se o arquivo não existir,
    cai pra send_whatsapp (texto puro com o caption) — útil enquanto as
    imagens em conteudo/ ainda não foram subidas."""
    if not image_path.exists():
        if caption:
            logger.warning(f"⚠️  Imagem ausente ({image_path.name}) — enviando caption como texto")
            return send_whatsapp(phone, caption)
        logger.warning(f"⚠️  Imagem ausente sem caption fallback — pulando ({image_path.name})")
        return False

    try:
        media_b64 = base64.b64encode(image_path.read_bytes()).decode()
    except Exception as e:
        logger.error(f"Erro lendo {image_path}: {e}")
        return send_whatsapp(phone, caption)

    result = evolution_request(
        f"/message/sendMedia/{INSTANCE_NAME}",
        method="POST",
        data={
            "number": phone,
            "mediatype": "image",
            "mimetype": "image/png",
            "caption": caption,
            "media": media_b64,
            "fileName": image_path.name,
            "delay": _typing_delay_ms(caption),
        }
    )
    success = bool(result.get("key") or result.get("id"))
    if success:
        logger.info(f"📷 Imagem {image_path.name} enviada para {phone}")
    else:
        logger.error(f"❌ Falha ao enviar imagem {image_path.name} para {phone}: {result}")
    return success


def send_whatsapp_document(phone: str, pdf_path: Path, caption: str = "") -> bool:
    """Envia um PDF (ou outro documento) via Evolution API. Uma única tentativa.
    Para retry com backoff use _send_pdf_with_retry."""
    if not pdf_path.exists():
        logger.error(f"❌ PDF ausente: {pdf_path}")
        return False
    try:
        media_b64 = base64.b64encode(pdf_path.read_bytes()).decode()
    except Exception as e:
        logger.error(f"Erro lendo {pdf_path}: {e}")
        return False
    result = evolution_request(
        f"/message/sendMedia/{INSTANCE_NAME}",
        method="POST",
        data={
            "number": phone,
            "mediatype": "document",
            "mimetype": "application/pdf",
            "caption": caption,
            "media": media_b64,
            "fileName": pdf_path.name,
            "delay": 1500,
        }
    )
    success = bool(result.get("key") or result.get("id"))
    if success:
        logger.info(f"📄 PDF {pdf_path.name} enviado para {phone}")
    else:
        logger.error(f"❌ Falha ao enviar PDF {pdf_path.name} para {phone}: {result}")
    return success


def _send_pdf_with_retry(phone: str, pdf_path: Path, attempts: int = 3) -> bool:
    """Tenta enviar o PDF até `attempts` vezes com backoff exponencial (2s, 4s).
    Retorna True se algum attempt teve sucesso."""
    for attempt in range(attempts):
        if send_whatsapp_document(phone, pdf_path):
            return True
        if attempt < attempts - 1:
            wait = 2 * (attempt + 1)
            logger.warning(f"↻ retry de {pdf_path.name} em {wait}s "
                           f"(tentativa {attempt + 2}/{attempts})")
            time.sleep(wait)
    return False


def deliver_purchase(phone: str, name: str, lead_id: str, trigger: str):
    """Fluxo determinístico pós-pagamento:
       1) registra a mensagem do lead na história (já feito antes da chamada),
       2) manda mensagem de agradecimento calorosa,
       3) envia os 5 PDFs (libro1.pdf … libro5.pdf) com retry,
       4) marca books_delivered no banco,
       5) notifica o dono — celebração se tudo OK, alerta se algum PDF falhou.
    `trigger` é só pra log: 'payment_text' | 'awaiting_files' | 'image_proof'."""
    # Prioriza o nome do histórico (resposta à pergunta de PASO 1) sobre o
    # `pushName` do WhatsApp — pushName pode ser etiqueta/apelido/configuração
    # de tradução do app (ex: "Traducir Al Español") e nunca deve aparecer em
    # saudações. Sem nome → saudação neutra.
    safe = _extract_name_from_history(lead_id)
    nombre = safe if safe else ""
    saludo = f"¡Qué alegría, {nombre}!" if nombre else "¡Qué alegría!"
    thank_msg = (
        f"{saludo} 🎉 ¡Mil gracias por tu compra! Aquí van los 5 libros — "
        "espero que los disfrutes muchísimo y que tu cocina se llene de "
        "recetas ricas, sanas y seguras 💚\n\n"
        "Cualquier duda con las recetas, aquí estoy para ayudarte 😊"
    )
    send_whatsapp(phone, thank_msg)
    add_message(lead_id, "assistant", thank_msg)

    failed_books = []
    for i in range(1, 6):
        pdf_path = CONTENT_DIR / f"libro{i}.pdf"
        if not _send_pdf_with_retry(phone, pdf_path, attempts=3):
            failed_books.append(pdf_path.name)

    mark_books_delivered(lead_id)
    if failed_books:
        notify_owner_sale_partial_failure(name, phone, trigger, failed_books)
    else:
        notify_owner_sale(name, phone, trigger=trigger)


_ALL_MARKERS = (
    CLAVE_PIX_MARKER,
    WISE_APP_MARKER,
    WISE_EMAIL_MARKER,
    WISE_WIRE_MARKER,
)


def _scrub_markers(text: str) -> str:
    """Remove qualquer marcador de controle remanescente do texto antes de
    enviar pro lead. Defesa contra a IA emitir o mesmo marcador duas vezes
    na resposta — partition() só pega a primeira ocorrência, então a
    segunda iria literal pro WhatsApp. Também limpa marcadores de MEDIA
    (livros, prova social) que tenham sobrado em pre/post."""
    markers = list(_ALL_MARKERS) + list(MEDIA_DISPATCH.keys())
    for m in markers:
        text = text.replace(m, "")
    # Colapsa quebras de linha múltiplas que sobraram do scrub.
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def dispatch_response(phone: str, response: str, name: str = "") -> bool:
    """Envia a resposta da IA. Se contiver algum marcador de mídia
    (definido em MEDIA_DISPATCH), separa o texto antes/depois do marcador
    e intercala as mensagens de mídia entre eles. Sem marcador, envia
    como mensagem de texto única. Retorna False se QUALQUER sub-envio
    (texto ou mídia) falhar — assim o watcher sabe que precisa alertar."""

    # Prex / PIX: lead confirmou que paga por Prex. Enviamos a explicação
    # (texto antes do marker), depois a chave PIX (UUID) e o CPF — cada um
    # SOZINHO em mensagem isolada — assim o lead toca e copia cada valor
    # sem arrastar texto extra. O cliente usa qualquer um dos dois dentro
    # do app Prex em "Transferir a Brasil" / "Pago con Pix".
    # Marcamos sent_checkout=1 pra liberar triggers pós-pago.
    if CLAVE_PIX_MARKER in response:
        pre, _, post = response.partition(CLAVE_PIX_MARKER)
        pre, post = _scrub_markers(pre), _scrub_markers(post)
        ok = True
        if pre and not send_whatsapp(phone, pre):
            ok = False
        if not send_whatsapp(phone, PIX_KEY):
            ok = False
        if not send_whatsapp(phone, CPF_PREX):
            ok = False
        if post and not send_whatsapp(phone, post):
            ok = False
        if ok:
            lead_id = f"wa_{phone}"
            if not _checkout_already_sent(lead_id):
                mark_checkout_sent(lead_id)
            notify_owner_pix_sent(name, phone)
        return ok

    # Wise EMAIL — lead tem o app de Wise e prefere mandar pelo email do
    # contato em vez de routing+account. Manda só o email sozinho pra
    # cópia de um toque.
    if WISE_EMAIL_MARKER in response:
        pre, _, post = response.partition(WISE_EMAIL_MARKER)
        pre, post = _scrub_markers(pre), _scrub_markers(post)
        ok = True
        if pre and not send_whatsapp(phone, pre):
            ok = False
        if not send_whatsapp(phone, WISE_EMAIL):
            ok = False
        if post and not send_whatsapp(phone, post):
            ok = False
        if ok:
            lead_id = f"wa_{phone}"
            if not _checkout_already_sent(lead_id):
                mark_checkout_sent(lead_id)
            notify_owner_wise_sent(name, phone, kind="email")
        return ok

    # Wise APP — lead tem o app de Wise. Manda 3 valores SOZINHOS pra
    # cópia (holder, routing, account). Wise auto-detecta o banco a partir
    # do routing — não precisa mandar nome/endereço do banco aqui.
    if WISE_APP_MARKER in response:
        pre, _, post = response.partition(WISE_APP_MARKER)
        pre, post = _scrub_markers(pre), _scrub_markers(post)
        ok = True
        if pre and not send_whatsapp(phone, pre):
            ok = False
        if not send_whatsapp(phone, WISE_HOLDER_NAME):
            ok = False
        if not send_whatsapp(phone, WISE_ROUTING):
            ok = False
        if not send_whatsapp(phone, WISE_ACCOUNT):
            ok = False
        if post and not send_whatsapp(phone, post):
            ok = False
        if ok:
            lead_id = f"wa_{phone}"
            if not _checkout_already_sent(lead_id):
                mark_checkout_sent(lead_id)
            notify_owner_wise_sent(name, phone, kind="app")
        return ok

    # Wise WIRE — lead vai usar banco tradicional. Manda 1 mensagem rotulada
    # com TODOS os campos. Lead lê tudo junto e seleciona/copia campo a
    # campo no formulário do banco. 6 mensagens soltas seria barulho demais
    # num caminho que já vai cobrar tarifa cara ($20-50 do banco do lead).
    if WISE_WIRE_MARKER in response:
        pre, _, post = response.partition(WISE_WIRE_MARKER)
        pre, post = _scrub_markers(pre), _scrub_markers(post)
        ok = True
        if pre and not send_whatsapp(phone, pre):
            ok = False
        if not send_whatsapp(phone, WISE_WIRE_BLOCK):
            ok = False
        if post and not send_whatsapp(phone, post):
            ok = False
        if ok:
            lead_id = f"wa_{phone}"
            if not _checkout_already_sent(lead_id):
                mark_checkout_sent(lead_id)
            notify_owner_wise_sent(name, phone, kind="wire")
        return ok

    for marker, items in MEDIA_DISPATCH.items():
        if marker in response:
            pre, _, post = response.partition(marker)
            pre, post = _scrub_markers(pre), _scrub_markers(post)
            ok = True
            if pre and not send_whatsapp(phone, pre):
                ok = False
            for item in items:
                if not send_whatsapp_media(phone, item["image"], item.get("caption", "")):
                    ok = False
            if post and not send_whatsapp(phone, post):
                ok = False
            return ok

    return send_whatsapp(phone, _scrub_markers(response))


# ── Transcrição de áudio ──────────────────────────────────────────────────────

def download_audio_base64(raw_msg: dict):
    """Baixa áudio da Evolution API e retorna bytes do arquivo."""
    result = evolution_request(
        f"/chat/getBase64FromMediaMessage/{INSTANCE_NAME}",
        method="POST",
        data={"message": raw_msg, "convertToMp4": False}
    )
    b64 = result.get("base64") or result.get("data")
    if not b64:
        return None
    try:
        return base64.b64decode(b64)
    except Exception as e:
        logger.error(f"Erro ao decodificar base64: {e}")
        return None


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg"):
    """Envia áudio para OpenAI Whisper e retorna transcrição."""
    # Determina extensão pelo mime type
    ext_map = {
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".mp4",
        "audio/webm": ".webm",
        "audio/wav": ".wav",
    }
    ext = ext_map.get(mime_type, ".ogg")

    # Salva em arquivo temporário
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name

    try:
        # Multipart form-data manualmente
        boundary = "----WhisperBoundary"
        filename = f"audio{ext}"

        body_parts = []
        body_parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1".encode())
        body_parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: {mime_type}\r\n\r\n".encode()
            + audio_bytes
        )
        body_parts.append(f"--{boundary}--".encode())
        body = b"\r\n".join(body_parts)

        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {AI_API_KEY}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return result.get("text", "").strip()
    except urllib.error.HTTPError as e:
        logger.error(f"Whisper erro {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        logger.error(f"Whisper erro: {e}")
        return None
    finally:
        os.unlink(tmp_path)


# ── Extração de mensagens ─────────────────────────────────────────────────────

def extract_message_data(msg) -> dict:
    """Extrai dados da mensagem. Retorna text=None para áudios (processar separado)."""
    if not isinstance(msg, dict):
        return {}
    key = msg.get("key", {})
    if not isinstance(key, dict):
        return {}
    if key.get("fromMe", False):
        return {}
    remote_jid = key.get("remoteJid", "")
    if "@g.us" in remote_jid:
        return {}

    if key.get("addressingMode") == "lid" and key.get("remoteJidAlt"):
        phone = key["remoteJidAlt"].replace("@s.whatsapp.net", "")
    else:
        phone = remote_jid.replace("@s.whatsapp.net", "").replace("@lid", "")

    push_name = msg.get("pushName", "Lead")
    message_content = msg.get("message", {})
    if not isinstance(message_content, dict):
        return {}

    # Texto normal
    text = (
        message_content.get("conversation") or
        (message_content.get("extendedTextMessage") or {}).get("text") or
        ""
    )

    # Áudio (audioMessage ou pttMessage = push-to-talk)
    audio_info = message_content.get("audioMessage") or message_content.get("pttMessage")
    is_audio = bool(audio_info) and not text

    # Imagem: NÃO baixamos nem lemos — só sinalizamos pra que o loop
    # principal possa interpretar como provável comprovante de pagamento
    # (quando combinado com checkout já enviado).
    image_info = message_content.get("imageMessage")
    is_image = bool(image_info) and not text and not is_audio
    image_caption = (image_info or {}).get("caption", "") if is_image else ""

    return {
        "id":            key.get("id", ""),
        "phone":         phone,
        "name":          push_name,
        "text":          text.strip() or image_caption.strip(),
        "is_audio":      is_audio,
        "is_image":      is_image,
        "mime":          (audio_info or {}).get("mimetype", "audio/ogg") if is_audio else None,
        "raw_msg":       msg if is_audio else None,
    }


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": [], "last_run": None}


def save_state(state: dict):
    state["last_run"] = datetime.now().isoformat()
    if len(state["seen_ids"]) > 500:
        state["seen_ids"] = state["seen_ids"][-500:]
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Loop principal ────────────────────────────────────────────────────────────

# Flag de parada cooperativa. PM2 manda SIGINT/SIGTERM no `pm2 restart` e dá
# ~1.6s antes de SIGKILL. Em vez de deixar o sinal interromper Python no meio
# de uma chamada de OpenAI ou de um send_whatsapp (o que pode deixar o lead
# recebendo mensagem sem histórico persistido, ou vice-versa), apenas sinalizamos
# a flag e checamos ENTRE iterações — qualquer turno em andamento termina antes
# do exit. Usamos lista de 1 elemento pra ser mutável dentro do handler sem
# precisar de `nonlocal`/`global`.
_SHOULD_STOP = [False]


def _request_stop(signum, frame):
    """Handler de SIGTERM/SIGINT. Apenas seta a flag — o loop principal
    detecta no topo da próxima iteração e sai limpo. NÃO faz nada que possa
    falhar (sem I/O, sem logging.error) pra não levantar dentro do handler."""
    _SHOULD_STOP[0] = True


def watch():
    init_db()
    logger.info("🔍 Watcher iniciado — Chef Sandra ativa (texto + áudio)")
    state = load_state()

    # Signal handlers só rodam na main thread. O watcher não usa threads
    # explícitas (todo o trabalho é serial neste loop), então o handler vai
    # ser invocado de forma segura entre opcodes — não no meio de uma syscall
    # de rede longa, mas garantidamente antes da próxima iteração do while.
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    while True:
        if _SHOULD_STOP[0]:
            logger.info("🛑 Recebido sinal de parada, finalizando após iteração atual")
            break
        try:
            messages = fetch_messages(count=20)
            for msg in messages:
                msg_data = extract_message_data(msg)
                if not msg_data or not msg_data.get("phone"):
                    continue

                msg_id = msg_data["id"]
                if msg_id in state["seen_ids"]:
                    continue
                state["seen_ids"].append(msg_id)

                phone = msg_data["phone"]
                name  = msg_data["name"]
                text  = msg_data["text"]
                is_image = msg_data.get("is_image", False)

                # Transcrever áudio se necessário
                if msg_data["is_audio"]:
                    logger.info(f"🎙️  Áudio recebido de {name} ({phone}) — transcrevendo...")
                    audio_bytes = download_audio_base64(msg_data["raw_msg"])
                    if audio_bytes:
                        text = transcribe_audio(audio_bytes, msg_data["mime"])
                        if text:
                            logger.info(f"📝 Transcrição: {text[:80]}")
                        else:
                            logger.warning("⚠️  Transcrição falhou — ignorando mensagem")
                            continue
                    else:
                        logger.warning("⚠️  Não foi possível baixar áudio — ignorando")
                        continue

                if not text and not is_image:
                    continue

                lead_id = f"wa_{phone}"

                # ── Sinal de pós-pagamento (entrega determinística, ANTES da IA) ──
                # Trust model: se o lead diz que pagou, ou pede os livros após o
                # checkout, ou manda uma imagem (provável comprovante) após o
                # checkout — entregamos os 5 PDFs e comemoramos a venda. O LLM é
                # totalmente bypassado nesse turno pra evitar tentativas de revenda.
                #
                # IMPORTANTE: se o lead está pausado, NADA dispara automaticamente —
                # o humano assumiu a conversa e pode estar fechando manualmente
                # (ex: enviando chave PIX por privado). O cliente que disser
                # "paguei" enquanto pausado vai cair no fluxo de paused (registra
                # mensagem mas não responde). Quando despausar, mensagem nova de
                # pagamento dispara a entrega normalmente.
                purchase_trigger = None
                if not is_lead_paused(lead_id):
                    if text and is_payment_confirmation(text):
                        purchase_trigger = "payment_text"
                    elif text and is_awaiting_files(text) and _checkout_already_sent(lead_id):
                        purchase_trigger = "awaiting_files"
                    elif is_image and _checkout_already_sent(lead_id):
                        purchase_trigger = "image_proof"

                if purchase_trigger and not books_already_delivered(lead_id):
                    user_msg = text if text else "(imagen recibida — comprobante de pago)"
                    logger.info(f"💰 Sinal de venda ({purchase_trigger}) de {name} ({phone})")
                    try:
                        add_message(lead_id, "user", user_msg)
                        deliver_purchase(phone, name, lead_id, trigger=purchase_trigger)
                    except Exception as e:
                        logger.error(f"Erro na entrega de livros: {e}\n{traceback.format_exc()}")
                    continue

                # Pagamento já confirmado antes (books_delivered=1) e o lead
                # voltou a falar de pagamento/arquivos? Não chamamos a IA —
                # ela tende a emitir [[ENVIAR_LIBROS]] (PASO 4: capas dos
                # livros, não os PDFs) e confundir o cliente. Mandamos uma
                # tranquilização fixa apontando que os PDFs já foram enviados.
                if purchase_trigger and books_already_delivered(lead_id):
                    safe = _extract_name_from_history(lead_id)
                    nombre = f", {safe}" if safe else ""
                    user_msg = text if text else "(imagen recibida — comprobante de pago)"
                    msg = (
                        f"Ya te envié los 5 libros aquí mismo{nombre} 😊 Son archivos "
                        "PDF — revisa más arriba en este chat. Si no los ves o tuviste "
                        "algún problema en abrirlos, me avisas que reenvío 💚"
                    )
                    logger.info(f"💬 Pós-entrega: {purchase_trigger} reincidente de {name} ({phone}) — mensagem fixa")
                    try:
                        add_message(lead_id, "user", user_msg)
                        if send_whatsapp(phone, msg):
                            add_message(lead_id, "assistant", msg)
                    except Exception as e:
                        logger.error(f"Erro na resposta pós-entrega: {e}\n{traceback.format_exc()}")
                    continue

                # Imagem sem trigger de venda (ou já entregue) → o agente não lê
                # imagens. Se o lead nunca recebeu link de checkout, alertamos
                # o dono UMA vez (pode ser comprovante de outro canal). Caso
                # contrário só ignora.
                if is_image and not text:
                    if (not _checkout_already_sent(lead_id)
                            and not has_image_alert_been_sent(lead_id)):
                        notify_owner_unexpected_image(name, phone)
                        mark_image_alert_sent(lead_id)
                    else:
                        logger.info(f"🖼️  Imagem de {name} ({phone}) — sem trigger de venda, ignorando")
                    continue

                logger.info(f"📩 {name} ({phone}): {text[:60]}")
                try:
                    response = handle_message(phone, name, text)
                    if response:
                        sent_ok = dispatch_response(phone, response, name=name)
                        if sent_ok:
                            # Só persiste assistant + agenda followups APÓS envio
                            # confirmado — evita histórico fantasma quando a
                            # Evolution API derruba a mensagem.
                            commit_response(phone, name, response)
                        else:
                            # Lead ficou sem resposta. Alerta o dono pra ele
                            # responder manualmente enquanto a API não volta.
                            notify_owner_send_failure(name, phone, response)
                except Exception as e:
                    # handle_message levantou (OpenAI esgotou retry, falha
                    # inesperada, etc.). Lead sem resposta — avisa o dono
                    # pra ele assumir manual em vez de só sumir.
                    logger.error(f"Erro ao processar: {e}\n{traceback.format_exc()}")
                    try:
                        notify_owner_processing_failure(name, phone, text or "(sem texto)", str(e))
                    except Exception as notify_err:
                        logger.error(f"Falha também ao notificar owner: {notify_err}")

            save_state(state)

            # ── Quiet hours: bot não dispara nenhuma mensagem automática
            # (followup link/preço, recovery) entre 22h e 8h do fuso ops.
            # Mensagens de cliente entrando seguem sendo processadas (acima);
            # o silêncio é só para envios proativos. Quando a janela acaba,
            # os pendentes acumulados disparam no próximo poll naturalmente.
            in_quiet = _is_quiet_hour(_now_dt_in_tz())
            if in_quiet:
                time.sleep(POLL_INTERVAL)
                continue

            # ── Follow-ups de checkout ──────────────────────────────────────
            for fu in get_pending_followups():
                # Nome vem do histórico (resposta à pergunta de PASO 1), NÃO
                # do pushName — pushName pode ser etiqueta/apelido/configuração
                # do app (ex: "Traducir Al Español"). Sem nome no histórico →
                # saudação neutra.
                safe = _extract_name_from_history(fu["lead_id"])
                greeting = f"Hola {safe}" if safe else "Hola"
                msg = (
                    f"{greeting} 😊 ¿Pudiste completar el proceso de compra?\n"
                    "Si tuviste algún problema en algún paso, aquí estoy para ayudarte 💙"
                )
                if send_whatsapp(fu["phone"], msg):
                    mark_followup_sent(fu["lead_id"])
                    logger.info(f"📲 Follow-up enviado para {fu['phone']}")

            # ── Follow-ups de preço (30 min sem resposta após PASO 5) ───────
            for fu in get_pending_price_followups():
                safe = _extract_name_from_history(fu["lead_id"])
                greeting = f"Hola {safe}" if safe else "Hola"
                msg = (
                    f"{greeting} 😊 ¿Pudiste decidir con cuál de los valores te quedas? "
                    "Si tienes alguna duda o algo no quedó claro, aquí estoy para ayudarte 💚"
                )
                if send_whatsapp(fu["phone"], msg):
                    mark_price_followup_sent(fu["lead_id"])
                    logger.info(f"📲 Follow-up de preço enviado para {fu['phone']}")

            # ── Recoveries (cadência 30min → 4h → 1d, gerada via LLM) ──────
            for r in get_pending_recoveries():
                lead_id = r["lead_id"]
                stage   = r["stage"]
                msg     = generate_recovery_message(lead_id, r.get("name"), stage)
                if not msg:
                    logger.warning(f"⚠️  Recovery stage {stage} sem mensagem gerada — lead={lead_id}")
                    advance_recovery(lead_id)
                    continue
                # Defesa em camada: o prompt do LLM proíbe markers em
                # recovery, mas se desobedecer (visto em produção 11/05 com
                # [[ENVIAR_LIBROS]] indo literal pro cliente) limpamos aqui
                # — recovery NUNCA roda através do dispatch_response que
                # expandiria os markers.
                msg = _scrub_markers(msg)
                if not msg:
                    logger.warning(f"⚠️  Recovery stage {stage} ficou vazia após scrub — lead={lead_id}")
                    advance_recovery(lead_id)
                    continue
                if send_whatsapp(r["phone"], msg):
                    record_assistant_message(lead_id, msg)
                    advance_recovery(lead_id)
                    logger.info(f"♻️  Recovery stage {stage} enviado pra {r['phone']}")

            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            logger.info("⏹️  Watcher encerrado")
            break
        except Exception as e:
            logger.error(f"Erro no loop: {e}\n{traceback.format_exc()}")
            time.sleep(5)


if __name__ == "__main__":
    watch()
