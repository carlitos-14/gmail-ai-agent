"""
supabase_client.py
Gestión de la tabla `citas` en Supabase.

Esquema esperado de la tabla `citas`:
  - email       TEXT
  - event_id    TEXT
  - fecha_cita  TIMESTAMP WITH TIME ZONE
  - created_at  TIMESTAMP WITH TIME ZONE (default: now())
"""

import logging
import os
import re
from datetime import datetime

logger = logging.getLogger(__name__)


def clean_email(raw: str) -> str:
    """Extrae solo el email de cadenas como 'Nombre <email@x.com>'."""
    m = re.search(r'<(.+?)>', raw)
    return m.group(1).strip() if m else raw.strip()


def get_supabase():
    """Devuelve el cliente de Supabase inicializado."""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not url or not key:
        raise RuntimeError("Faltan variables de entorno SUPABASE_URL o SUPABASE_KEY.")

    return create_client(url, key)


def guardar_cita(email: str, event_id: str, fecha_cita: datetime) -> bool:
    try:
        db = get_supabase()
        db.table("citas").insert({
            "email": clean_email(email),
            "event_id": event_id,
            "fecha_cita": fecha_cita.isoformat(),
        }).execute()
        logger.info(f"💾 Cita guardada en Supabase para {clean_email(email)} | event_id: {event_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error guardando cita en Supabase: {e}")
        return False


def obtener_ultimo_event_id(email: str) -> str | None:
    """
    Busca el event_id más reciente asociado al email del cliente.
    Devuelve el event_id o None si no encuentra ninguno.
    """
    try:
        db = get_supabase()
        result = (
            db.table("citas")
            .select("event_id, fecha_cita")
            .eq("email", clean_email(email))
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            event_id = result.data[0]["event_id"]
            logger.info(f"🔍 event_id encontrado para {clean_email(email)}: {event_id}")
            return event_id
        else:
            logger.warning(f"⚠️ No se encontró ninguna cita para {clean_email(email)}.")
            return None
    except Exception as e:
        logger.error(f"❌ Error consultando Supabase para {email}: {e}")
        return None


def obtener_todas_citas_cliente(email: str) -> list[dict]:
    try:
        db = get_supabase()
        ahora = datetime.now().astimezone().isoformat()
        result = (
            db.table("citas")
            .select("event_id, fecha_cita")
            .eq("email", clean_email(email))
            .gt("fecha_cita", ahora)
            .order("fecha_cita")
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"❌ Error obteniendo citas de {email}: {e}")
        return []


MAX_CITAS_ACTIVAS = int(os.environ.get("MAX_CITAS_ACTIVAS", "2"))


def contar_citas_futuras(email: str) -> int:
    try:
        db = get_supabase()
        ahora = datetime.now().astimezone().isoformat()
        result = (
            db.table("citas")
            .select("event_id", count="exact")
            .eq("email", clean_email(email))
            .gt("fecha_cita", ahora)
            .execute()
        )
        total = result.count if result.count is not None else len(result.data)
        logger.info(f"📊 Citas futuras de {clean_email(email)}: {total}")
        return total
    except Exception as e:
        logger.error(f"❌ Error contando citas futuras para {email}: {e}")
        return 0


def eliminar_cita(email: str, event_id: str) -> bool:
    try:
        db = get_supabase()
        db.table("citas").delete().eq("email", clean_email(email)).eq("event_id", event_id).execute()
        logger.info(f"🗑️ Registro eliminado de Supabase: {clean_email(email)} | {event_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error eliminando cita de Supabase: {e}")
        return False


def obtener_event_id_por_fecha(email: str, fecha: datetime) -> str | None:
    """
    Busca el event_id de una cita por email y fecha aproximada.
    Si fecha_hora es medianoche (00:00), busca en todo el día.
    Si tiene hora concreta, busca ±2 horas.
    """
    try:
        from datetime import timedelta
        db = get_supabase()
        email_limpio = clean_email(email)

        # Si la hora es medianoche asumimos que solo saben el día → buscar todo el día
        if fecha.hour == 0 and fecha.minute == 0:
            desde = fecha.replace(hour=0, minute=0, second=0).isoformat()
            hasta = fecha.replace(hour=23, minute=59, second=59).isoformat()
        else:
            desde = (fecha - timedelta(hours=2)).isoformat()
            hasta = (fecha + timedelta(hours=2)).isoformat()

        result = (
            db.table("citas")
            .select("event_id, fecha_cita")
            .eq("email", email_limpio)
            .gte("fecha_cita", desde)
            .lte("fecha_cita", hasta)
            .order("fecha_cita")
            .limit(1)
            .execute()
        )
        if result.data:
            event_id = result.data[0]["event_id"]
            logger.info(f"🔍 event_id por fecha encontrado para {email_limpio}: {event_id}")
            return event_id
        logger.warning(f"⚠️ No se encontró cita para {email_limpio} en torno a {fecha}.")
        return None
    except Exception as e:
        logger.error(f"❌ Error buscando cita por fecha para {email}: {e}")
        return None


def obtener_citas_futuras_todas() -> list[dict]:
    """
    Devuelve todas las citas futuras de Supabase (de cualquier cliente).
    Usado para detectar citas huérfanas (borradas de Calendar pero no de Supabase).
    """
    try:
        db = get_supabase()
        ahora = datetime.now().astimezone().isoformat()
        result = (
            db.table("citas")
            .select("email, event_id, fecha_cita")
            .gt("fecha_cita", ahora)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"❌ Error obteniendo citas futuras de Supabase: {e}")
        return []
