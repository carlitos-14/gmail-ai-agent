# Email AI Agent 🤖

Un agente inteligente que revisa tu Gmail cada 5 minutos, analiza los emails con IA y decide automáticamente si responder, agendar una cita, cancelarla o escalarla para revisión manual. Corre gratis en GitHub Actions.

---

## Cómo funciona

```
Gmail (no leídos)
       ↓
  Lee el email
       ↓
  Groq / LLaMA 3.3 70B lo analiza
  (usando la documentación de tu empresa como contexto)
       ↓
 ┌─────────┬──────────┬──────────┬──────────┐
 │         │          │          │          │
RESPONDER AGENDAR  CANCELAR   ESCALAR
 │         │          │          │
Envía    Crea evento Borra     ⭐ Marca con
reply    en Calendar evento    estrella
         Guarda en   Elimina   (revisión
         Supabase    Supabase  manual)
 └─────────┴──────────┴──────────┘
                ↓
       Etiqueta: bot-processed
```

---

## Qué hace exactamente

- Lee solo emails no leídos que no haya procesado antes
- Usa tu documentación en PDF para responder preguntas sobre tus servicios
- Si la consulta es simple o está en la documentación → responde solo
- Si el cliente pide una cita con fecha y hora → la agenda en Google Calendar y guarda el registro en Supabase
- Si el cliente quiere cancelar una cita → busca el evento en Supabase, lo elimina de Calendar y confirma por email
- Si es una queja, tema complejo o hay dudas → lo escala y lo marca con estrella para revisión manual
- Si la IA falla por algún motivo → escala automáticamente, nunca responde a ciegas

---

## Stack

- **Python 3.11**
- **Gmail API** — lectura y envío de correos
- **Google Calendar API** — gestión de citas
- **Groq** — LLaMA 3.3 70B para análisis y redacción
- **Supabase** — base de datos de citas
- **PyPDF2** — lectura del PDF de documentación
- **GitHub Actions** — ejecución automática gratuita cada 5 minutos

---

## Estructura del proyecto

```
gmail-ai-agent/
├── email_agent.py          # Orquestador principal
├── pdf_context.py          # Carga el PDF de documentación
├── supabase_client.py      # CRUD de la tabla citas
├── calendar_client.py      # Gestión de Google Calendar
├── documentacion_empresa.pdf  # Documentación de tu empresa (RAG)
├── requirements.txt
└── .github/
    └── workflows/
        └── email_agent.yml
```

---

## Setup

### 1. Clona el repo

```bash
git clone https://github.com/tu-usuario/gmail-ai-agent.git
cd gmail-ai-agent
```

### 2. Consigue las credenciales

**Groq API Key**

Entra en [console.groq.com](https://console.groq.com) y crea una API key. Es gratis.

**Gmail + Google Calendar OAuth**

1. Ve a [Google Cloud Console](https://console.cloud.google.com)
2. Crea un proyecto y activa la **Gmail API** y la **Google Calendar API**
3. Crea credenciales OAuth 2.0 (tipo: aplicación de escritorio)
4. Ejecuta el script `get_token.py` para obtener el `refresh_token` con todos los scopes necesarios
5. El JSON resultante tiene esta forma:

```json
{
  "token": "ya29.xxx",
  "refresh_token": "1//xxx",
  "client_id": "xxx.apps.googleusercontent.com",
  "client_secret": "xxx"
}
```

**Supabase**

1. Crea un proyecto en [supabase.com](https://supabase.com)
2. Ejecuta el SQL del archivo `supabase_schema.sql` en el SQL Editor para crear la tabla `citas`
3. Copia la **Project URL** y la **anon public key** desde Project Settings → API

### 3. Añade los secrets en GitHub

Settings → Secrets and variables → Actions:

| Secret | Descripción |
|--------|-------------|
| `GROQ_API_KEY` | Tu API key de Groq |
| `GMAIL_CREDENTIALS_JSON` | JSON completo con token y refresh_token de Google |
| `COMPANY_NAME` | Nombre de tu empresa (aparece en las respuestas) |
| `SUPABASE_URL` | URL de tu proyecto Supabase |
| `SUPABASE_KEY` | Anon key de Supabase |
| `GOOGLE_CALENDAR_ID` | `primary` o el ID de tu calendario |

### 4. Sube la documentación de tu empresa

Añade un archivo llamado `documentacion_empresa.pdf` en la raíz del repo. El agente lo leerá automáticamente para responder preguntas sobre tus servicios. Si no existe, el agente sigue funcionando sin ese contexto.

### 5. Listo

Sube el código a `main` y el workflow empieza solo. Cada 5 minutos revisa el buzón.

Para lanzarlo manualmente: pestaña **Actions → Run workflow**.

---

## Personalización

El comportamiento del agente lo controla el `SYSTEM_PROMPT` dentro de `email_agent.py`. Desde ahí puedes cambiar:

- Cuándo responde y cuándo escala
- El tono de las respuestas
- La duración por defecto de las citas (variable `EVENT_DURATION_MINUTES` en `calendar_client.py`)
- La zona horaria de los eventos (por defecto `Europe/Madrid`)

---

## Base de datos

La tabla `citas` en Supabase almacena:

| Campo | Tipo | Descripción |
|-------|------|-------------|
| `email` | TEXT | Email del cliente |
| `event_id` | TEXT | ID del evento en Google Calendar |
| `fecha_cita` | TIMESTAMPTZ | Fecha y hora de la cita |
| `created_at` | TIMESTAMPTZ | Fecha de creación del registro |

---

## Nota importante

No subas nunca el JSON de credenciales al repo. Los secrets van en GitHub Secrets, no en el código.
