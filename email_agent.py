import os, json, base64, logging, time
from email.header import Header
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from groq import Groq
from datetime import datetime
from dateutil import parser as dateparser

# Módulos nuevos
from pdf_context import load_company_context
from supabase_client import guardar_cita, obtener_ultimo_event_id, eliminar_cita
from calendar_client import agendar_cita, cancelar_cita, buscar_slots_libres

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# ── Clientes ───────────────────────────────────────────────────────────────────
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
COMPANY = os.environ.get("COMPANY_NAME", "Nuestra Empresa")

# ── Contexto de empresa desde PDF ─────────────────────────────────────────────
COMPANY_CONTEXT = load_company_context()
CONTEXT_BLOCK = (
    f"\n\n---\nDOCUMENTACIÓN DE LA EMPRESA (usa esto para responder preguntas sobre servicios):\n{COMPANY_CONTEXT}\n---"
    if COMPANY_CONTEXT else ""
)

SYSTEM_PROMPT = f"""Eres el asistente de atención al cliente de {COMPANY}.
{CONTEXT_BLOCK}

Analiza el email y responde SOLO con JSON válido (sin markdown, sin texto extra):
{{
  "accion": "AGENDAR" | "CANCELAR" | "RESPONDER" | "ESCALAR",
  "fecha_hora": "YYYY-MM-DDTHH:MM:SS" (solo si accion es AGENDAR, si no: null),
  "respuesta_texto": "Texto completo para el cuerpo del correo al cliente"
}}

Reglas de decisión:
- AGENDAR  → el cliente pide una cita, reunión o llamada con fecha/hora concreta
- CANCELAR → el cliente quiere cancelar o anular una cita existente
- RESPONDER → preguntas simples, FAQs, info sobre servicios (usa la documentación)
- ESCALAR  → quejas, temas legales, situaciones complejas o dudas sin respuesta en la documentación

Si la fecha/hora no está clara para AGENDAR, usa ESCALAR en su lugar.

IMPORTANTE: En respuesta_texto NUNCA escribas el día de la semana (lunes, martes, etc.).
Usa solo el formato de fecha DD/MM/YYYY. El sistema insertará el día correcto automáticamente."""

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

BOT_LABEL = "bot-processed"


# ── Autenticación Gmail ────────────────────────────────────────────────────────
def get_gmail_service():
    creds_data = json.loads(os.environ["GMAIL_CREDENTIALS_JSON"])
    creds = Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=creds_data.get("client_id"),
        client_secret=creds_data.get("client_secret"),
        scopes=GMAIL_SCOPES,
    )
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("Credenciales de Gmail inválidas y sin refresh_token.")
    return build("gmail", "v1", credentials=creds)


# ── Label bot-processed ────────────────────────────────────────────────────────
def get_or_create_label(svc):
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for l in labels:
        if l["name"] == BOT_LABEL:
            return l["id"]
    created = svc.users().labels().create(
        userId="me", body={"name": BOT_LABEL}
    ).execute()
    logger.info(f"🏷️ Label '{BOT_LABEL}' creado en Gmail.")
    return created["id"]


# ── Extracción de cuerpo del email ─────────────────────────────────────────────
def get_body(payload):
    """Extrae texto plano de forma recursiva para soportar estructuras MIME anidadas."""
    mime = payload.get("mimeType", "")

    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", "ignore")[:3000]

    for part in payload.get("parts", []):
        result = get_body(part)
        if result:
            return result

    return ""


# ── Envío de respuesta ─────────────────────────────────────────────────────────
def send_reply(svc, mid, tid, to, subject, text):
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    encoded_subject = Header(reply_subject, "utf-8").encode()

    msg = (
        f"To: {to}\r\n"
        f"Subject: {encoded_subject}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n"
        f"In-Reply-To: {mid}\r\n"
        f"References: {mid}\r\n\r\n"
        f"{text}"
    )
    svc.users().messages().send(
        userId="me",
        body={
            "raw": base64.urlsafe_b64encode(msg.encode("utf-8")).decode(),
            "threadId": tid,
        }
    ).execute()


def mark_read(svc, mid):
    svc.users().messages().modify(
        userId="me", id=mid, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def mark_starred(svc, mid):
    svc.users().messages().modify(
        userId="me", id=mid, body={"addLabelIds": ["STARRED"]}
    ).execute()


def mark_bot_processed(svc, mid, label_id):
    svc.users().messages().modify(
        userId="me", id=mid, body={"addLabelIds": [label_id]}
    ).execute()


# ── Análisis con Groq/LLaMA ────────────────────────────────────────────────────
def analyze(subject, sender, body, retries=3, backoff=5):
    last_error = None
    for attempt in range(retries):
        try:
            response = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Asunto: {subject}\nDe: {sender}\nContenido: {body}"}
                ],
                temperature=0.2,
                max_tokens=500,
            )
            text = response.choices[0].message.content.strip()
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            return json.loads(text.strip())
        except Exception as e:
            last_error = e
            wait = backoff * (attempt + 1)
            logger.warning(f"⚠️ Intento {attempt + 1}/{retries} fallido: {e}. Reintentando en {wait}s...")
            time.sleep(wait)
    raise last_error


# ── Manejadores de cada acción ─────────────────────────────────────────────────
def handle_agendar(svc, mid, tid, sender, subject, decision):
    """Agenda la cita en Calendar, guarda en Supabase y responde al cliente."""
    fecha_str = decision.get("fecha_hora")
    respuesta = decision.get("respuesta_texto", "")

    if not fecha_str:
        logger.warning("⚠️ AGENDAR sin fecha_hora. Escalando.")
        mark_starred(svc, mid)
        return

    try:
        fecha_dt = dateparser.parse(fecha_str)
    except Exception as e:
        logger.error(f"❌ No se pudo parsear la fecha '{fecha_str}': {e}. Escalando.")
        mark_starred(svc, mid)
        return

    # Si la fecha ya pasó, corregir al año actual o al siguiente
    hoy = datetime.now(tz=fecha_dt.tzinfo)
    if fecha_dt < hoy:
        fecha_dt = fecha_dt.replace(year=hoy.year)
        if fecha_dt < hoy:
            fecha_dt = fecha_dt.replace(year=hoy.year + 1)
        logger.warning(f"⚠️ Fecha en el pasado corregida a: {fecha_dt}")

    event_id = agendar_cita(fecha_dt, sender, subject)

    if event_id:
        guardar_cita(email=sender, event_id=event_id, fecha_cita=fecha_dt)
        # Sustituir cualquier fecha en respuesta_texto por la versión con día correcto
        DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        fecha_correcta = f"{DIAS[fecha_dt.weekday()]} {fecha_dt.strftime('%d/%m/%Y')} a las {fecha_dt.strftime('%H:%M')}"
        respuesta = respuesta.replace(fecha_dt.strftime("%d/%m/%Y"), fecha_correcta)
        send_reply(svc, mid, tid, sender, subject, respuesta)
        mark_read(svc, mid)
        logger.info(f"📅 Cita agendada y confirmada: {subject[:60]}")
    else:
        logger.warning("⛔ Slot ocupado. Buscando alternativas y notificando al cliente.")

        slots_libres = buscar_slots_libres(fecha_dt, num_slots=3)

        if slots_libres:
            opciones_texto = "\n".join(
                f"  • {s.strftime('%A %d/%m/%Y a las %H:%M')}"
                for s in slots_libres
            )
            mensaje_ocupado = (
                f"Hola,\n\n"
                f"Gracias por contactarnos. Lamentablemente el horario solicitado "
                f"({fecha_dt.strftime('%d/%m/%Y a las %H:%M')}) no está disponible "
                f"ya que tenemos otra cita en ese rango horario.\n\n"
                f"Te proponemos las siguientes fechas libres próximas:\n\n"
                f"{opciones_texto}\n\n"
                f"Confírmanos cuál de estas opciones te viene mejor y lo agendamos "
                f"de inmediato.\n\nRecuerda que atendemos de lunes a viernes de 9:30 a 17:00.\n\n"
                f"Un saludo,\n{COMPANY}"
            )
        else:
            mensaje_ocupado = (
                f"Hola,\n\n"
                f"Gracias por contactarnos. Lamentablemente el horario solicitado "
                f"({fecha_dt.strftime('%d/%m/%Y a las %H:%M')}) no está disponible "
                f"ya que tenemos otra cita en ese rango horario.\n\n"
                f"En este momento no encontramos huecos libres en los próximos días. "
                f"Por favor, indícanos otro horario de tu preferencia y lo revisamos "
                f"sin problema.\n\nRecuerda que atendemos de lunes a viernes de 9:30 a 17:00.\n\n"
                f"Un saludo,\n{COMPANY}"
            )

        send_reply(svc, mid, tid, sender, subject, mensaje_ocupado)
        mark_read(svc, mid)


def handle_cancelar(svc, mid, tid, sender, subject, decision):
    """Busca el event_id en Supabase, cancela en Calendar y responde al cliente."""
    respuesta = decision.get("respuesta_texto", "")

    event_id = obtener_ultimo_event_id(sender)

    if not event_id:
        logger.warning(f"⚠️ No se encontró cita para cancelar: {sender}. Escalando.")
        mark_starred(svc, mid)
        return

    cancelado = cancelar_cita(event_id)

    if cancelado:
        eliminar_cita(email=sender, event_id=event_id)
        send_reply(svc, mid, tid, sender, subject, respuesta)
        mark_read(svc, mid)
        logger.info(f"🗑️ Cita cancelada y confirmada: {subject[:60]}")
    else:
        logger.error("❌ Fallo al cancelar evento en Calendar. Escalando.")
        mark_starred(svc, mid)


def handle_responder(svc, mid, tid, sender, subject, decision):
    """Envía la respuesta directa al cliente."""
    respuesta = decision.get("respuesta_texto", "")
    if respuesta:
        send_reply(svc, mid, tid, sender, subject, respuesta)
        mark_read(svc, mid)
        logger.info(f"✅ Respondido: {subject[:60]}")
    else:
        logger.warning("⚠️ RESPONDER sin respuesta_texto. Escalando.")
        mark_starred(svc, mid)


def handle_escalar(svc, mid, subject, decision):
    """Marca con estrella para revisión manual."""
    mark_starred(svc, mid)
    logger.info(f"⭐ Escalado: {subject[:60]} | Motivo: {decision.get('respuesta_texto', '?')[:80]}")


# ── Procesador principal ───────────────────────────────────────────────────────
def process_new_emails():
    logger.info("🔍 Revisando emails...")

    svc = get_gmail_service()
    label_id = get_or_create_label(svc)

    msgs = svc.users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],
        maxResults=10,
        q=f"-label:{BOT_LABEL}"
    ).execute().get("messages", [])

    new_count = 0
    for ref in msgs:
        mid = ref["id"]

        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
        subject = h.get("Subject", "(sin asunto)")
        sender  = h.get("From", "?")
        body    = get_body(msg["payload"])
        tid     = msg["threadId"]

        try:
            decision = analyze(subject, sender, body)
        except Exception as e:
            logger.error(f"Error Groq tras reintentos: {e}")
            decision = {
                "accion": "ESCALAR",
                "fecha_hora": None,
                "respuesta_texto": f"Error IA: {e}"
            }

        accion = decision.get("accion", "ESCALAR").upper()

        if accion == "AGENDAR":
            handle_agendar(svc, mid, tid, sender, subject, decision)
        elif accion == "CANCELAR":
            handle_cancelar(svc, mid, tid, sender, subject, decision)
        elif accion == "RESPONDER":
            handle_responder(svc, mid, tid, sender, subject, decision)
        else:
            handle_escalar(svc, mid, subject, decision)

        mark_bot_processed(svc, mid, label_id)
        new_count += 1

    logger.info(f"✔ Listo. {new_count} emails nuevos procesados.")


if __name__ == "__main__":
    process_new_emails()
