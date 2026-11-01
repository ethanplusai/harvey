# Morthy en 10 pasos

La versión corta. Para los detalles: [busqueda-de-empleo.md](busqueda-de-empleo.md).

## Antes de empezar, consigue esto

- **Linux o macOS** con Python 3.11 o más nuevo.
- **Un Gmail** para enviar y leer respuestas.
- **Una API key de DeepSeek** ([platform.deepseek.com](https://platform.deepseek.com), con unos dólares de saldo; dura semanas).
- **Una llave gratis de Reoon** ([reoon.com](https://www.reoon.com/email-verifier/), 600 verificaciones al mes), para no escribir a correos que no existen.

## Instalar (una sola vez)

**1. Descarga e instala:**
```bash
git clone https://github.com/JJhiru/morthy.git && cd morthy
python3 -m venv .venv && source .venv/bin/activate
pip install -e . && python -m playwright install chromium
```

**2. Tus llaves:** `cp .env.example .env`, abre `.env` y llena:
```
DEEPSEEK_API_KEY=sk-...
REOON_API_KEY=...
GMAIL_CLIENT_ID=...        # paso 5
GMAIL_CLIENT_SECRET=...
```

**3. Tu configuración:** `cp harvey.job.example.yaml harvey.local.yaml` y cambia todo lo de "Ana Ejemplo" por lo tuyo. Lo que más importa:
- `persona` → tu nombre, tu Gmail y tu **firma** con links a GitHub, LinkedIn y CV.
- `product` → tu CV en 4-5 líneas. **Morthy solo dice de ti lo que está aquí.**
- `job_search.projects` → tus proyectos con sus datos reales, y para qué sectores sirve cada uno.
- `job_search.country` → tu país.

**4. Tu perfil de formularios:** `cp perfil_aplicacion.example.yaml perfil_aplicacion.yaml`, cambia cada `"REVISAR"` y al final pon `revisado: true`. Contesta lo legal (edad, permiso de trabajo, visa) con la verdad.

**5. Conecta Gmail:** en [Google Cloud Console](https://console.cloud.google.com/) crea un proyecto, activa la **Gmail API**, crea credenciales **OAuth "Desktop app"** y pega el id y el secret en `.env`. En la pantalla de consentimiento, **publica la app**: si la dejas en "Testing", Google la desconecta cada 7 días. Después:
```bash
morthy gmail auth     # se abre el navegador
morthy gmail test     # debe decir "Connected as ..."
```

**6. Comprueba:**
```bash
morthy status --check           # cerebro y correo listos
morthy signals --confirm free   # activa las señales gratuitas
```

## Usarlo (cada día)

Siempre primero: `cd morthy && source .venv/bin/activate`

**7. Prende Morthy** y déjalo corriendo en una terminal:
```bash
morthy run
```
Cada 15 minutos busca empresas, escribe correos, lee respuestas y prepara notas de seguimiento. **Nada sale sin tu aprobación.** Solo trabaja con la computadora despierta.

**8. Abre el dashboard** en otra terminal (`morthy dashboard`, abre el link que imprime) y revisa dos pestañas:
- **Outbox** → aprueba, edita o rechaza los correos que preparó.
- **Other mail** → respuestas de empresas a tus postulaciones. Pulsa **Record** para anotarlas.

**9. Busca vacantes y postula** (una vez al día):
```bash
python tools/vacantes_remotas.py    # lista en data/vacantes/vacantes_remotas.md
python tools/aplicar.py --csv       # llena los formularios; los envías tú
```

**10. LinkedIn**, después de postular:
```bash
python tools/linkedin_mensajes.py preparar   # redacta notas para quien decide
python tools/linkedin_mensajes.py abrir 1    # abre el perfil y copia la nota
```

Cada semana mira qué funciona: `python tools/seguimiento.py`

## Reglas de oro

- **Nunca compartas** `.env`, `harvey.local.yaml`, `perfil_aplicacion.yaml` ni la carpeta `data/`.
- **Revisa cada correo** antes de aprobarlo, sobre todo a quién va.
- **Pocos y buenos:** 15 correos al día como máximo, y personalizados.
- ¿Algo falla? `morthy status --check` dice qué. Y la [guía completa](busqueda-de-empleo.md#problemas-comunes) tiene una tabla de problemas comunes.
