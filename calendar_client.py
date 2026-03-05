"""
calendar_client.py
Gestión de citas en Google Calendar usando la misma autenticación OAuth2 que Gmail.
"""

import json
import logging
import os
from datetime import datetime, timedelta

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]

CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")
EVENT_DURATION_MINUTES = int(os.environ.get("EVENT_DURATION_MINUTES", "60"))


def get_calendar_service():
    creds_data = json.loads(os.environ["GMAIL_CREDENTIALS_JSON"])
    creds = Credentials(
        token=creds_data.get("token"),
        refresh_token=creds_data.get("refresh_token"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=creds_data.get("client_id"),
        client_secret=creds_data.get("client_secret"),
        scopes=CALENDAR_SCOPES,
    )
    if not creds.valid:
        if creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError("Credenciales de Google inválidas y sin refresh_token.")
    return build("calendar", "v3", credentials=creds)


def slot_disponible(fecha_hora: datetime) -> bool:
    try:
        svc = get_calendar_service()

        # Comprobar solo el rango exacto que ocuparía el nuevo evento
        evento_inicio = fecha_hora.isoformat()
        evento_fin    = (fecha_hora + timedelta(minutes=EVENT_DURATION_MINUTES)).isoformat()

        eventos = svc.events().list(
            calendarId=CALENDAR_ID,
            timeMin=evento_inicio,
            timeMax=evento_fin,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        conflictos = eventos.get("items", [])

        if conflictos:
            logger.warning(f"⛔ Slot ocupado: {len(conflictos)} evento(s) solapan con {fecha_hora}")
            return False

        logger.info(f"✅ Slot disponible para {fecha_hora}")
        return True

    except Exception as e:
        logger.error(f"❌ Error comprobando disponibilidad: {e}")
        return False


def buscar_slots_libres(fecha_hora_referencia: datetime, num_slots: int = 3) -> list[datetime]:
    """
    Busca `num_slots` franjas horarias libres a partir de ahora (nunca en el pasado),
    empezando desde fecha_hora_referencia si es futura, o desde ahora si ya pasó.
    Respeta horario de atención 09:30–17:00 todos los días de la semana.
    """
    HORA_INICIO = 9
    MIN_INICIO  = 30
    HORA_FIN    = 17

    ahora = datetime.now(tz=fecha_hora_referencia.tzinfo)
    punto_inicio = max(ahora, fecha_hora_referencia)
    punto_inicio = punto_inicio.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    candidatos: list[datetime] = []
    candidato = punto_inicio
    max_dias = 14

    while len(candidatos) < num_slots:
        if candidato > punto_inicio + timedelta(days=max_dias):
            logger.warning("⚠️ Se alcanzó el límite de búsqueda de slots libres.")
            break

        # Saltar fuera de horario
        en_horario = (
            (candidato.hour > HORA_INICIO or (candidato.hour == HORA_INICIO and candidato.minute >= MIN_INICIO))
            and candidato.hour < HORA_FIN
        )
        if not en_horario:
            candidato = (candidato + timedelta(days=1)).replace(
                hour=HORA_INICIO, minute=MIN_INICIO, second=0, microsecond=0
            )
            continue

        if slot_disponible(candidato):
            candidatos.append(candidato)
            logger.info(f"🟢 Slot libre encontrado: {candidato}")

        candidato += timedelta(hours=1)

    return candidatos[:num_slots]


def agendar_cita(fecha_hora: datetime, email_cliente: str, asunto_email: str) -> str | None:
    if not slot_disponible(fecha_hora):
        logger.warning(f"⛔ No se puede agendar: slot ocupado en {fecha_hora}")
        return None

    try:
        svc = get_calendar_service()

        inicio = fecha_hora
        fin = fecha_hora + timedelta(minutes=EVENT_DURATION_MINUTES)

        evento = {
            "summary": f"Cita: {asunto_email[:80]}",
            "description": f"Cita agendada automáticamente para {email_cliente}.",
            "start": {
                "dateTime": inicio.isoformat(),
                "timeZone": "Europe/Madrid",
            },
            "end": {
                "dateTime": fin.isoformat(),
                "timeZone": "Europe/Madrid",
            },
            "attendees": [{"email": email_cliente}],
            "reminders": {
                "useDefault": False,
                "overrides": [
                    {"method": "email", "minutes": 24 * 60},
                    {"method": "popup", "minutes": 30},
                ],
            },
        }

        resultado = svc.events().insert(
            calendarId=CALENDAR_ID,
            body=evento,
            sendUpdates="all",
        ).execute()

        event_id = resultado.get("id")
        logger.info(f"📅 Cita agendada | event_id: {event_id} | {inicio}")
        return event_id

    except Exception as e:
        logger.error(f"❌ Error agendando cita: {e}")
        return None


def cancelar_cita(event_id: str) -> bool:
    try:
        svc = get_calendar_service()
        svc.events().delete(
            calendarId=CALENDAR_ID,
            eventId=event_id,
            sendUpdates="all",
        ).execute()
        logger.info(f"🗑️ Evento eliminado | event_id: {event_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error cancelando cita: {e}")
        return False
