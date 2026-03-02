import os, json, base64, logging, time
from email.header import Header
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from groq import Groq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger(__name__)

# ── Clientes ───────────────────────────────────────────────────────────────────
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
COMPANY = os.environ.get("COMPANY_NAME", "Nuestra Empresa")

SYSTEM_PROMPT = f"""Eres el asistente de atención al cliente de {COMPANY}.
Analiza el email y responde SOLO con JSON válido (sin markdown):
{{
  "action": "respond" o "escalate",
  "reason": "motivo en una frase",
  "reply_message": "respuesta al cliente si respond, vacío si escalate"
}}

respond → preguntas simples, FAQs, info general
escalate → quejas, temas complejos, cliente lo pide, incertidumbre"""

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

BOT_LABEL = "bot-processed"


# ── FIX #1: Credenciales robustas ─────────────────────────────────────────────
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
    # FIX: usar creds.valid en lugar de solo creds.expired
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("Credenciales de Gmail inválidas y sin refresh_token.")
    return build("gmail", "v1", credentials=creds)


# ── Obtener o crear el label en Gmail ─────────────────────────────────────────
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


# ── FIX #2: get_body recursivo para emails anidados ───────────────────────────
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


# ── FIX #3: send_reply con subject codificado correctamente ───────────────────
def send_reply(svc, mid, tid, to, subject, text):
    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    # Codificar subject para soportar tildes, emojis, etc.
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


# ── FIX #4: Análisis con Groq con retry y backoff ─────────────────────────────
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


# ── Procesador principal ───────────────────────────────────────────────────────
def process_new_emails():
    logger.info("🔍 Revisando emails...")

    svc = get_gmail_service()
    label_id = get_or_create_label(svc)

    # Solo trae emails UNREAD que NO tengan el label bot-processed
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
            d = analyze(subject, sender, body)
        except Exception as e:
            logger.error(f"Error Groq tras reintentos: {e}")
            d = {"action": "escalate", "reason": f"Error IA: {e}", "reply_message": ""}

        action = d.get("action", "escalate")
        reply  = d.get("reply_message", "")

        if action == "respond" and reply:
            send_reply(svc, mid, tid, sender, subject, reply)
            mark_read(svc, mid)
            logger.info(f"✅ Respondido: {subject[:60]}")
        else:
            mark_starred(svc, mid)
            logger.info(f"⭐ Escalado: {subject[:60]} | Motivo: {d.get('reason', '?')}")

        mark_bot_processed(svc, mid, label_id)
        new_count += 1

    logger.info(f"✔ Listo. {new_count} emails nuevos procesados.")


if __name__ == "__main__":
    process_new_emails()
