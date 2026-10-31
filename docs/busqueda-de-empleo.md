# Harvey para buscar empleo

Esta versión de Harvey está adaptada para conseguir trabajo, no para vender.
Tu CV hace de "producto", y Harvey trabaja por ti en cinco frentes:

| Qué hace | Cómo |
|---|---|
| **Busca vacantes remotas** para tu nivel en 8 fuentes públicas | `tools/vacantes_remotas.py` |
| **Llena formularios de postulación** con tu perfil, y tú los envías | `tools/aplicar.py` |
| **Escribe a quien decide** (CTO, founder) en empresas que te interesan, en frío | el ciclo de Harvey (`harvey run`) |
| **Manda una nota corta** a una persona de la empresa 2-21 días después de que postulas | el ciclo de Harvey |
| **Lee tus respuestas** (sistemas de postulación, reclutadores) y te dice qué dicen | pestaña **Otros correos** del dashboard |

**Nada sale sin tu aprobación.** Cada correo espera en el **Outbox** del
dashboard hasta que lo apruebes, y `aplicar.py` nunca pulsa "enviar".

---

## Lo que necesitas

- Linux o macOS con Python 3.11 o más nuevo.
- Una cuenta de **Gmail** para enviar y leer respuestas. Puede ser tu correo
  personal: el volumen es bajo (15 correos al día como máximo).
- Un **"cerebro"**, que puede ser uno de estos tres:
  - **DeepSeek**: una API key de [platform.deepseek.com](https://platform.deepseek.com),
    con saldo prepagado. Es el más barato; una semana normal cuesta centavos.
  - **Claude**: una suscripción Claude Pro/Max con Claude Code instalado.
  - **Codex**: una suscripción de ChatGPT (ver el README principal).
- Opcional pero recomendado: una llave gratis de un **verificador de correos**
  (Reoon da 600 al mes). Sin ella, Harvey no envía a direcciones que no pudo verificar.

## Instalación (unos 15 minutos)

```bash
git clone <este repo> harvey && cd harvey
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m playwright install chromium     # para aplicar.py
```

### 1. Tus llaves (`.env`)

```bash
cp .env.example .env
```

Abre `.env` y pon como mínimo estas:

```
DEEPSEEK_API_KEY=sk-...        # si usas DeepSeek
GMAIL_CLIENT_ID=...            # ver "Conectar Gmail" abajo
GMAIL_CLIENT_SECRET=...
REOON_API_KEY=...              # verificador de correos (recomendado)
```

Para buscar en la web, Harvey usa DuckDuckGo y Bing sin llave. Una llave de
**Serper** (2.500 búsquedas gratis) lo hace más confiable: `SERPER_API_KEY=`.

### 2. Tu configuración (`harvey.local.yaml`)

```bash
cp harvey.job.example.yaml harvey.local.yaml
```

Edítala de arriba abajo, cambiando todo lo de "Ana Ejemplo" por tus datos:

- **`persona`**: tu nombre, tu Gmail y tu **firma**, con los links a tu GitHub,
  LinkedIn y CV. Va al final de cada correo.
- **`product`**: tu CV en pocas líneas. Harvey **solo dice de ti lo que está aquí**
  y en `job_search.projects`; nunca inventa experiencia ni números.
- **`icp`**: el tipo de empresas a las que quieres escribir y los cargos de quien decide.
- **`brain`**: `provider: deepseek` (o `claude`). `max_daily_usd` es el tope de gasto diario.
- **`job_search`**:
  - `intro` y `role_sought`: cómo te presentas y qué buscas.
  - `country`: tu país; el buscador también acepta vacantes abiertas para él.
  - `projects`: **la parte más importante.** Cada proyecto lleva los sectores
    para los que sirve y la frase con sus datos reales. Harvey elige uno por
    empresa según su sector. Un proyecto con `sectors: []` es el que usa por defecto.
  - `signature_es`: tu firma en español, para empresas hispanohablantes.
- **`channels.email.link_domains`**: los dominios de los links de tu firma.

`harvey.local.yaml` está en `.gitignore`: nunca se sube al repo.

### 3. Tu perfil de formularios (`perfil_aplicacion.yaml`)

```bash
cp perfil_aplicacion.example.yaml perfil_aplicacion.yaml
```

Es lo que `aplicar.py` escribe en los formularios: datos de contacto,
elegibilidad, idiomas, salario esperado, etc. Cambia cada `"REVISAR"` y al
final pon `revisado: true`. **Las preguntas legales (edad, permiso de trabajo,
visa) contéstalas con la verdad**: una respuesta falsa puede descalificarte.
También está en `.gitignore`.

### 4. Conectar Gmail

1. En [Google Cloud Console](https://console.cloud.google.com/), crea un proyecto,
   activa la **Gmail API** y crea credenciales **OAuth** de tipo "Desktop app".
2. Pon el client id y el secret en `.env`.
3. En la pantalla de consentimiento OAuth, agrega tu Gmail como usuario de prueba,
   o publica la app. **Si la dejas en "Testing", Google desconecta a Harvey cada 7 días**
   y tienes que repetir el paso 4.
4. Corre `harvey gmail auth` (abre el navegador) y después `harvey gmail test`.

### 5. Comprobar y arrancar

```bash
harvey status --check      # dice si el cerebro y el correo están listos
harvey signals --confirm free
harvey run                 # el ciclo: cada 15 minutos revisa qué hay que hacer
harvey dashboard           # en otra terminal; abre el link que imprime
```

Harvey **solo trabaja mientras tu computadora está despierta.** Si se
suspende, Harvey se detiene con ella y sigue al despertar.

---

## El día a día

**Una vez al día, buscar vacantes:**
```bash
python tools/vacantes_remotas.py
```
Deja la lista en `data/vacantes/vacantes_remotas.md`, separada en dos grupos:
**para tu nivel** (junior, intern, entry-level) y **para estirarte** (no dicen
nivel: las que te parecen "puedo aprender" o "interesante").

**Postular:**
```bash
python tools/aplicar.py --csv          # las vacantes del buscador
python tools/aplicar.py URL [URL...]   # links que encontraste tú
```
Abre cada formulario, lo llena, te muestra la carta y las respuestas que
redactó, y **tú revisas y envías**. Al marcarla como enviada, te pregunta el
**peso**: pesado (calza con tu CV), aprender o interesante.

**En el dashboard:**
- **Outbox:** aprueba, edita o rechaza los correos que Harvey preparó: correos
  en frío y notas de seguimiento después de postular.
- **Otros correos:** las respuestas de empresas. Harvey sugiere de qué postulación
  son y si es rechazo, prueba técnica, entrevista o acuse. Con **Anotar** queda
  registrado. El botón "Importar últimos 30 días" trae lo que llegó antes.
- **Conversations:** cuando alguien responde a un correo de Harvey, aquí está
  el hilo y el borrador de respuesta, que también espera tu aprobación.

**Ver qué funciona:**
```bash
python tools/seguimiento.py            # respuestas y entrevistas por canal y por peso
python tools/seguimiento.py lista      # tus postulaciones numeradas
python tools/seguimiento.py marcar 3 entrevista "llamada el martes"
```

## Costos con DeepSeek

Harvey usa `deepseek-flash` sin "modo pensamiento". Un correo cuesta una
fracción de centavo. `brain.max_daily_usd` (0,50 USD por defecto) detiene a
Harvey ese día si lo alcanza; el gasto es una estimación con tarifas de hora
pico, así que lo real suele ser menos. Mira el consumo con `harvey usage`.
Si te quedas sin saldo, Harvey se pausa solo y te lo dice en `harvey status`.

## Seguridad y privacidad

- **Nunca compartas** `.env`, `harvey.local.yaml`, `perfil_aplicacion.yaml`
  ni la carpeta `data/`: tienen tus llaves, tu token de Gmail y tu historial.
  Las cuatro están en `.gitignore`.
- El cerebro (DeepSeek, Claude o Codex) **no tiene herramientas**: solo
  devuelve texto. No puede leer archivos ni ejecutar nada, aunque una página
  web o un correo le pida hacerlo.
- Harvey tiene límites de envío diario, horas de silencio, y frena solo si
  rebotan demasiados correos o alguien pide no ser contactado.

## Problemas comunes

| Síntoma | Qué hacer |
|---|---|
| `command not found: harvey` | `source .venv/bin/activate` |
| "Gmail token refresh rejected" | `harvey gmail auth` (y publica la app OAuth; ver paso 4) |
| `DEEPSEEK_API_KEY is not set` | Falta la llave en `.env` |
| "DeepSeek balance is empty" | Recarga saldo en platform.deepseek.com |
| `aplicar.py` dice "El perfil aún no está revisado" | Quedan `"REVISAR"` en `perfil_aplicacion.yaml` |
| No llegan notas de seguimiento | Solo salen 2-21 días después de postular, y solo si Harvey encuentra a una persona con un correo verificable |
