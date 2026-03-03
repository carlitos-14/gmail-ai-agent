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
from datetime import datetime

logger = logging.getLogger(__name__)


def get_supabase():
    """Devuelve el cliente de Supabase inicializado."""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not url or not key:
        raise RuntimeError("Faltan variables de entorno SUPABASE_URL o SUPABASE_KEY.")

    return create_client(url, key)


def guardar_cita(email: str, event_id: str, fecha_cita: datetime) -> bool:
    """
    Inserta un registro nuevo en la tabla `citas`.
    Devuelve True si tuvo éxito, False si falló.
    """
    try:
        db = get_supabase()
        db.table("citas").insert({
            "email": email,
            "event_id": event_id,
            "fecha_cita": fecha_cita.isoformat(),
        }).execute()
        logger.info(f"💾 Cita guardada en Supabase para {email} | event_id: {event_id}")
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
            .eq("email", email)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            event_id = result.data[0]["event_id"]
            logger.info(f"🔍 event_id encontrado para {email}: {event_id}")
            return event_id
        else:
            logger.warning(f"⚠️ No se encontró ninguna cita para {email}.")
            return None
    except Exception as e:
        logger.error(f"❌ Error consultando Supabase para {email}: {e}")
        return None


def eliminar_cita(email: str, event_id: str) -> bool:
    """
    Elimina el registro de la cita en Supabase una vez cancelada en Calendar.
    """
    try:
        db = get_supabase()
        db.table("citas").delete().eq("email", email).eq("event_id", event_id).execute()
        logger.info(f"🗑️ Registro eliminado de Supabase: {email} | {event_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error eliminando cita de Supabase: {e}")
        return False
