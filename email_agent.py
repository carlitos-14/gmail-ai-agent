import os, json, base64, logging, time
from email.header import Header
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from groq import Groq
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil import parser as dateparser

TZ_MADRID = ZoneInfo("Europe/Madrid")

# Módulos nuevos
from pdf_context import load_company_context
from supabase_client import guardar_cita, obtener_ultimo_event_id, eliminar_cita
from calendar_client import agendar_cita, cancelar_cita, buscar_slots_libres, slot_disponible

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# ── Clientes ───────────────────────────────────────────────────────────────────
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
COMPANY = os.environ.get("COMPANY_NAME", "Nuestra Empresa")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "")

# ── Contexto de empresa desde PDF ─────────────────────────────────────────────
COMPANY_CONTEXT = load_company_context()
CONTEXT_BLOCK = (
    f"\n\n---\nDOCUMENTACIÓN DE LA EMPRESA (usa esto para responder preguntas sobre servicios):\n{COMPANY_CONTEXT}\n---"
    if COMPANY_CONTEXT else ""
)

# Fecha de hoy para que el LLM pueda resolver expresiones relativas
_DIAS_ES = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_HOY_DT  = datetime.now(tz=TZ_MADRID)
_HOY     = f"{_DIAS_ES[_HOY_DT.weekday()]} {_HOY_DT.strftime('%d/%m/%Y')}"

def _calendario_proximos_dias(n=14):
    """Genera una tabla de los próximos n días para incluir en el prompt."""
    from datetime import timedelta
    lineas = []
    for i in range(n):
        d = _HOY_DT + timedelta(days=i)
        lineas.append(f"  - {_DIAS_ES[d.weekday()]} = {d.strftime('%Y-%m-%d')}")
    return "
".join(lineas)

_CALENDARIO = _calendario_proximos_dias()

SYSTEM_PROMPT = f"""Eres el asistente de atención al cliente de {COMPANY}.
{CONTEXT_BLOCK}

La fecha de hoy es {_HOY}. Calendario de los próximos días (úsalo para resolver
expresiones como "el próximo jueves", "mañana", "la semana que viene"):

{_CALENDARIO}

Siempre devuelve fecha_hora en formato YYYY-MM-DDTHH:MM:SS, nunca texto.

Analiza el email y responde SOLO con JSON válido (sin markdown, sin texto extra):
{{
  "accion": "AGENDAR" | "CANCELAR" | "CONSULTAR" | "RESPONDER" | "ESCALAR",
  "fecha_hora": "YYYY-MM-DDTHH:MM:SS" (solo si accion es AGENDAR o CONSULTAR, si no: null),
  "respuesta_texto": "Texto completo para el cuerpo del correo al cliente"
}}

Reglas de decisión:
- AGENDAR   → el cliente pide una cita concreta con fecha y hora
- CANCELAR  → el cliente quiere cancelar o anular una cita existente
- CONSULTAR → el cliente pregunta por disponibilidad de un día o una hora concreta sin confirmar cita
              (ej: "¿tenéis hueco el viernes?", "¿está libre el lunes a las 10?", "¿qué horas tenéis disponibles el martes?")
              En este caso fecha_hora debe ser el día consultado a las 09:30 si no indica hora, o a la hora indicada si la menciona.
- RESPONDER → preguntas simples, FAQs, info sobre servicios (usa la documentación)
- ESCALAR   → quejas, temas legales, situaciones complejas, dudas sin respuesta en la documentación,
              o cuando el cliente pide explícitamente hablar con una persona, el encargado, responsable o personal humano.
              En estos casos, respuesta_texto debe explicar claramente al cliente el motivo.

Si la fecha/hora no está clara para AGENDAR o CONSULTAR, usa ESCALAR en su lugar.

IMPORTANTE: En respuesta_texto para AGENDAR NO incluyas fecha ni hora. Escribe únicamente
el texto de confirmación sin mencionar ninguna fecha ni hora concreta, por ejemplo:
"Estimado [nombre], gracias por contactarnos. Le confirmamos que su cita ha sido agendada
en nuestra oficina. Si necesita realizar algún cambio o cancelación, no dude en hacérnoslo saber."
El sistema insertará automáticamente la fecha y hora correctas."""

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


# ── Helpers ────────────────────────────────────────────────────────────────────
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]

def fecha_legible(dt: datetime) -> str:
    return f"{DIAS[dt.weekday()]} {dt.strftime('%d/%m/%Y')} a las {dt.strftime('%H:%M')}"


def slots_del_dia(fecha_referencia: datetime, hora_concreta: bool = False, num_slots: int = 3) -> list[datetime]:
    """
    Si hora_concreta=True, comprueba solo esa hora exacta.
    Si hora_concreta=False, busca hasta num_slots huecos libres en ese día (9:30-17:00).
    """
    HORA_INICIO = 9
    MIN_INICIO  = 30
    HORA_FIN    = 17

    if hora_concreta:
        return [fecha_referencia] if slot_disponible(fecha_referencia) else []

    # Generar todas las horas en punto del día laboral
    candidatos = []
    hora = fecha_referencia.replace(hour=HORA_INICIO, minute=MIN_INICIO, second=0, microsecond=0)
    while hora.hour < HORA_FIN:
        if slot_disponible(hora):
            candidatos.append(hora)
            if len(candidatos) >= num_slots:
                break
        hora += timedelta(hours=1)

    return candidatos


# ── Manejadores de cada acción ─────────────────────────────────────────────────
def handle_agendar(svc, mid, tid, sender, subject, decision):
    fecha_str = decision.get("fecha_hora")
    respuesta = decision.get("respuesta_texto", "")

    if not fecha_str:
        logger.warning("⚠️ AGENDAR sin fecha_hora. Escalando.")
        mark_starred(svc, mid)
        return

    try:
        fecha_dt = dateparser.parse(fecha_str)
        if fecha_dt is None:
            raise ValueError("dateparser devolvió None")
        fecha_dt = fecha_dt.replace(tzinfo=None)
        fecha_dt = fecha_dt.replace(tzinfo=TZ_MADRID)
    except Exception as e:
        logger.error(f"❌ No se pudo parsear la fecha '{fecha_str}': {e}. Escalando.")
        mark_starred(svc, mid)
        return

    hoy = datetime.now(tz=fecha_dt.tzinfo)
    if fecha_dt < hoy:
        fecha_dt = fecha_dt.replace(year=hoy.year)
        if fecha_dt < hoy:
            fecha_dt = fecha_dt.replace(year=hoy.year + 1)
        logger.warning(f"⚠️ Fecha en el pasado corregida a: {fecha_dt}")

    event_id = agendar_cita(fecha_dt, sender, subject)

    if event_id:
        guardar_cita(email=sender, event_id=event_id, fecha_cita=fecha_dt)
        confirmacion = (
            f"{respuesta.rstrip()}\n\n"
            f"📅 Fecha confirmada: {fecha_legible(fecha_dt)}."
        )
        send_reply(svc, mid, tid, sender, subject, confirmacion)
        mark_read(svc, mid)
        logger.info(f"📅 Cita agendada y confirmada: {subject[:60]}")
    else:
        logger.warning("⛔ Slot ocupado. Buscando alternativas y notificando al cliente.")
        slots_libres = buscar_slots_libres(fecha_dt, num_slots=3)

        if slots_libres:
            opciones_texto = "\n".join(f"  • {fecha_legible(s)}" for s in slots_libres)
            mensaje_ocupado = (
                f"Hola,\n\n"
                f"Gracias por contactarnos. Lamentablemente el horario solicitado "
                f"({fecha_legible(fecha_dt)}) no está disponible "
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
                f"({fecha_legible(fecha_dt)}) no está disponible "
                f"ya que tenemos otra cita en ese rango horario.\n\n"
                f"En este momento no encontramos huecos libres en los próximos días. "
                f"Por favor, indícanos otro horario de tu preferencia y lo revisamos "
                f"sin problema.\n\nRecuerda que atendemos de lunes a viernes de 9:30 a 17:00.\n\n"
                f"Un saludo,\n{COMPANY}"
            )

        send_reply(svc, mid, tid, sender, subject, mensaje_ocupado)
        mark_read(svc, mid)


def handle_consultar(svc, mid, tid, sender, subject, decision):
    """Responde con los huecos disponibles del día/hora consultado."""
    fecha_str = decision.get("fecha_hora")

    if not fecha_str:
        logger.warning("⚠️ CONSULTAR sin fecha_hora. Escalando.")
        mark_starred(svc, mid)
        return

    try:
        fecha_dt = dateparser.parse(fecha_str)
        if fecha_dt is None:
            raise ValueError("dateparser devolvió None")
        fecha_dt = fecha_dt.replace(tzinfo=None)
        fecha_dt = fecha_dt.replace(tzinfo=TZ_MADRID)
    except Exception as e:
        logger.error(f"❌ No se pudo parsear la fecha '{fecha_str}': {e}. Escalando.")
        mark_starred(svc, mid)
        return

    hoy = datetime.now(tz=fecha_dt.tzinfo)
    if fecha_dt < hoy:
        fecha_dt = fecha_dt.replace(year=hoy.year)
        if fecha_dt < hoy:
            fecha_dt = fecha_dt.replace(year=hoy.year + 1)

    # Si el LLM puso una hora concreta (distinta de 09:30), consultamos esa hora exacta
    hora_concreta = not (fecha_dt.hour == 9 and fecha_dt.minute == 30)
    slots = slots_del_dia(fecha_dt, hora_concreta=hora_concreta, num_slots=3)

    dia_str = f"{DIAS[fecha_dt.weekday()]} {fecha_dt.strftime('%d/%m/%Y')}"

    if hora_concreta:
        # El cliente preguntó por una hora concreta
        if slots:
            mensaje = (
                f"Hola,\n\n"
                f"Sí, el {dia_str} a las {fecha_dt.strftime('%H:%M')} está disponible.\n\n"
                f"Si deseas confirmar la cita, responde a este correo y lo agendamos de inmediato.\n\n"
                f"Recuerda que atendemos de lunes a viernes de 9:30 a 17:00.\n\n"
                f"Un saludo,\n{COMPANY}"
            )
        else:
            # Hora ocupada — ofrecemos alternativas ese mismo día, y si no hay, los próximos slots
            alternativas = slots_del_dia(fecha_dt, hora_concreta=False, num_slots=3)
            if alternativas:
                opciones_texto = "\n".join(f"  • {fecha_legible(s)}" for s in alternativas)
                mensaje = (
                    f"Hola,\n\n"
                    f"Lamentablemente el {dia_str} a las {fecha_dt.strftime('%H:%M')} no está disponible.\n\n"
                    f"Sin embargo, sí tenemos los siguientes huecos libres ese día:\n\n"
                    f"{opciones_texto}\n\n"
                    f"¿Te viene bien alguno? Responde confirmando y lo agendamos.\n\n"
                    f"Un saludo,\n{COMPANY}"
                )
            else:
                proximos = buscar_slots_libres(fecha_dt, num_slots=3)
                if proximos:
                    opciones_texto = "\n".join(f"  • {fecha_legible(s)}" for s in proximos)
                    mensaje = (
                        f"Hola,\n\n"
                        f"Lamentablemente el {dia_str} no tenemos huecos disponibles.\n\n"
                        f"Las próximas fechas libres más cercanas son:\n\n"
                        f"{opciones_texto}\n\n"
                        f"¿Te viene bien alguna? Responde confirmando y lo agendamos.\n\n"
                        f"Un saludo,\n{COMPANY}"
                    )
                else:
                    mensaje = (
                        f"Hola,\n\n"
                        f"Lamentablemente el {dia_str} no tenemos huecos disponibles "
                        f"ni encontramos fechas libres en los próximos días.\n\n"
                        f"Por favor, contáctanos de nuevo más adelante.\n\n"
                        f"Recuerda que atendemos de lunes a viernes de 9:30 a 17:00.\n\n"
                        f"Un saludo,\n{COMPANY}"
                    )
    else:
        # El cliente preguntó por disponibilidad general del día
        if slots:
            opciones_texto = "\n".join(f"  • {fecha_legible(s)}" for s in slots)
            mensaje = (
                f"Hola,\n\n"
                f"Para el {dia_str} tenemos los siguientes huecos disponibles:\n\n"
                f"{opciones_texto}\n\n"
                f"Si deseas confirmar alguno, responde a este correo indicando tu preferencia.\n\n"
                f"Un saludo,\n{COMPANY}"
            )
        else:
            proximos = buscar_slots_libres(fecha_dt, num_slots=3)
            if proximos:
                opciones_texto = "\n".join(f"  • {fecha_legible(s)}" for s in proximos)
                mensaje = (
                    f"Hola,\n\n"
                    f"Lamentablemente el {dia_str} no tenemos huecos disponibles.\n\n"
                    f"Las próximas fechas libres más cercanas son:\n\n"
                    f"{opciones_texto}\n\n"
                    f"¿Te viene bien alguna? Responde confirmando y lo agendamos.\n\n"
                    f"Un saludo,\n{COMPANY}"
                )
            else:
                mensaje = (
                    f"Hola,\n\n"
                    f"Lamentablemente el {dia_str} no tenemos huecos disponibles "
                    f"ni encontramos fechas libres en los próximos días.\n\n"
                    f"Por favor, contáctanos de nuevo más adelante.\n\n"
                    f"Recuerda que atendemos de lunes a viernes de 9:30 a 17:00.\n\n"
                    f"Un saludo,\n{COMPANY}"
                )

    send_reply(svc, mid, tid, sender, subject, mensaje)
    mark_read(svc, mid)
    logger.info(f"🔍 Consulta de disponibilidad respondida: {subject[:60]}")


def handle_cancelar(svc, mid, tid, sender, subject, decision):
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
    respuesta = decision.get("respuesta_texto", "")
    if respuesta:
        send_reply(svc, mid, tid, sender, subject, respuesta)
        mark_read(svc, mid)
        logger.info(f"✅ Respondido: {subject[:60]}")
    else:
        logger.warning("⚠️ RESPONDER sin respuesta_texto. Escalando.")
        mark_starred(svc, mid)


def handle_escalar(svc, mid, tid, sender, subject, decision):
    mark_starred(svc, mid)
    motivo = decision.get("respuesta_texto", "Sin motivo especificado")
    logger.info(f"⭐ Escalado: {subject[:60]} | Motivo: {motivo[:80]}")

    mensaje_cliente = motivo if motivo else (
        f"Hola,\n\n"
        f"Hemos recibido tu mensaje y nuestro equipo lo revisará a la mayor brevedad posible.\n\n"
        f"En breve nos pondremos en contacto contigo.\n\n"
        f"Un saludo,\n{COMPANY}"
    )
    send_reply(svc, mid, tid, sender, subject, mensaje_cliente)

    if CONTACT_EMAIL:
        aviso_interno = (
            f"Se ha escalado un email para revisión manual.\n\n"
            f"De: {sender}\n"
            f"Asunto: {subject}\n"
            f"Motivo: {motivo}\n\n"
            f"Revísalo en Gmail (estará marcado con ⭐)."
        )
        try:
            msg = (
                f"To: {CONTACT_EMAIL}\r\n"
                f"Subject: [ESCALADO] {subject}\r\n"
                f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                f"{aviso_interno}"
            )
            svc.users().messages().send(
                userId="me",
                body={"raw": base64.urlsafe_b64encode(msg.encode("utf-8")).decode()},
            ).execute()
            logger.info(f"📨 Aviso de escalado enviado a {CONTACT_EMAIL}")
        except Exception as e:
            logger.error(f"❌ Error enviando aviso de escalado: {e}")


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
        elif accion == "CONSULTAR":
            handle_consultar(svc, mid, tid, sender, subject, decision)
        elif accion == "CANCELAR":
            handle_cancelar(svc, mid, tid, sender, subject, decision)
        elif accion == "RESPONDER":
            handle_responder(svc, mid, tid, sender, subject, decision)
        else:
            handle_escalar(svc, mid, tid, sender, subject, decision)

        mark_bot_processed(svc, mid, label_id)
        new_count += 1

    logger.info(f"✔ Listo. {new_count} emails nuevos procesados.")


if __name__ == "__main__":
    process_new_emails()
