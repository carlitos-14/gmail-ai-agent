import os, json, base64, logging
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

PROCESSED_FILE = "processed_ids.json"

# ── Cargar/guardar IDs procesados ──────────────────────────────────────────────
def load_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    return set()

def save_processed(ids):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(ids), f)

# ── Gmail ──────────────────────────────────────────────────────────────────────
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
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("gmail", "v1", credentials=creds)

def get_body(payload):
    if "parts" in payload:
        for p in payload["parts"]:
            if p["mimeType"] == "text/plain":
                return base64.urlsafe_b64decode(p["body"].get("data", "")).decode("utf-8", "ignore")[:3000]
    elif payload["body"].get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")[:3000]
    return ""

def send_reply(svc, mid, tid, to, text):
    msg = f"To: {to}\r\nIn-Reply-To: {mid}\r\nReferences: {mid}\r\n\r\n{text}"
    svc.users().messages().send(
        userId="me",
        body={"raw": base64.urlsafe_b64encode(msg.encode()).decode(), "threadId": tid}
    ).execute()

def mark_read(svc, mid):
    svc.users().messages().modify(
        userId="me", id=mid, body={"removeLabelIds": ["UNREAD"]}
    ).execute()

def mark_starred(svc, mid):
    svc.users().messages().modify(
        userId="me", id=mid, body={"addLabelIds": ["STARRED"]}
    ).execute()

# ── Análisis con Groq ──────────────────────────────────────────────────────────
def analyze(subject, sender, body):
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

# ── Procesador principal ───────────────────────────────────────────────────────
def process_new_emails():
    logger.info("🔍 Revisando emails...")
    processed = load_processed()

    svc = get_gmail_service()
    msgs = svc.users().messages().list(
        userId="me", labelIds=["INBOX", "UNREAD"], maxResults=10
    ).execute().get("messages", [])

    new_count = 0
    for ref in msgs:
        mid = ref["id"]
        if mid in processed:
            continue

        msg = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        h = {x["name"]: x["value"] for x in msg["payload"]["headers"]}
        subject = h.get("Subject", "(sin asunto)")
        sender  = h.get("From", "?")
        body    = get_body(msg["payload"])
        tid     = msg["threadId"]

        try:
            d = analyze(subject, sender, body)
        except Exception as e:
            logger.error(f"Error Groq: {e}")
            d = {"action": "escalate", "reason": f"Error IA: {e}", "reply_message": ""}

        action = d.get("action", "escalate")
        reply  = d.get("reply_message", "")

        if action == "respond" and reply:
            send_reply(svc, mid, tid, sender, reply)
            mark_read(svc, mid)
            logger.info(f"✅ Respondido: {subject[:60]}")
        else:
            mark_starred(svc, mid)
            logger.info(f"⭐ Escalado: {subject[:60]}")

        processed.add(mid)
        new_count += 1

    save_processed(processed)
    logger.info(f"✔ Listo. {new_count} emails nuevos procesados.")

if __name__ == "__main__":
    process_new_emails()