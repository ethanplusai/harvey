#!/usr/bin/env python3
"""Borradores de mensajes de LinkedIn después de postular: Morthy prepara, tú envías.

Uso:
    .venv/bin/python tools/linkedin_mensajes.py preparar [--max 5]   # redacta para tus postulaciones
    .venv/bin/python tools/linkedin_mensajes.py                      # los pendientes, numerados
    .venv/bin/python tools/linkedin_mensajes.py abrir N              # abre el perfil y copia la nota
    .venv/bin/python tools/linkedin_mensajes.py mensaje N            # copia el mensaje (ya conectados)
    .venv/bin/python tools/linkedin_mensajes.py marcar N nota|mensaje|saltar
                                       # nota: mandaste la invitación; mensaje: ya le escribiste

Para cada postulación (hasta 21 días, sin rechazo ni entrevista) busca a quien
decide en la empresa: un nombre basta, no hace falta su correo. Redacta una nota
de conexión (máximo 300 caracteres, el límite de LinkedIn) y un mensaje para
cuando acepte. Si no encuentra a nadie, el link es una búsqueda de personas en
LinkedIn para que elijas tú.

Nunca inicia sesión en LinkedIn ni toca tu cuenta: abres el link, pegas el texto
y lo envías tú. Ojo: en cuentas gratuitas LinkedIn limita las notas
personalizadas al mes; si se te acaban, conecta sin nota y manda el mensaje
cuando acepte.
"""

import argparse
import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def copiar(texto: str) -> bool:
    """Al portapapeles con wl-copy (Wayland), xclip o xsel, si hay alguno."""
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "-ib"], ["pbcopy"]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=texto.encode(), timeout=5, check=True)
                return True
            except (OSError, subprocess.SubprocessError):
                continue
    return False


def abrir_link(url: str):
    try:
        subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        print(f"  Ábrelo en tu navegador: {url}")


def mostrar(n: int, d: dict, completo: bool = False):
    quien = f"{d['person']} ({d['person_title']})" if d["person"] else "(sin nombre: búsqueda de personas)"
    paso = " — invitación enviada, falta el mensaje" if d["status"] == "noted" else ""
    print(f"{n:>3}. {d['job_title'][:50]} · {d['company']}{paso}")
    print(f"     Para: {quien}")
    print(f"     Link: {d['link']}")
    print(f"     Nota ({len(d['note'])}/300): {d['note']}")
    if completo:
        print("     Mensaje:\n       " + d["message"].replace("\n", "\n       "))


async def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("preparar")
    p.add_argument("--max", type=int, default=5)
    for nombre in ("abrir", "mensaje"):
        sub.add_parser(nombre).add_argument("numero", type=int)
    p = sub.add_parser("marcar")
    p.add_argument("numero", type=int)
    p.add_argument("estado", choices=["nota", "mensaje", "saltar"])
    args = ap.parse_args()

    from harvey.brain import Brain
    from harvey.config import load_config, load_env
    from harvey.linkedin_drafts import LinkedInDrafter
    from harvey.state import StateManager
    config, env = load_config(), load_env()
    state = StateManager()
    await state.init_db()
    drafter = LinkedInDrafter(Brain(state, config.brain), state, config, env)

    if args.cmd == "preparar":
        pendientes = await drafter.pending()
        print(f"{len(pendientes)} postulación(es) sin borrador de LinkedIn. Preparando hasta {args.max}...")
        hechos = await drafter.prepare(limit=args.max)
        print(f"{hechos} borrador(es) listos. Míralos con: tools/linkedin_mensajes.py")
        return

    borradores = await drafter.drafts()
    if args.cmd is None:
        if not borradores:
            print("No hay borradores pendientes. Prepara con: tools/linkedin_mensajes.py preparar")
        for n, d in enumerate(borradores, 1):
            mostrar(n, d)
            print()
        return

    if not 1 <= args.numero <= len(borradores):
        sys.exit(f"No hay un borrador pendiente con el número {args.numero}.")
    d = borradores[args.numero - 1]
    if args.cmd == "abrir":
        mostrar(args.numero, d, completo=True)
        abrir_link(d["link"])
        print("\n  Nota copiada al portapapeles: en LinkedIn, Conectar → Añadir una nota → pegar."
              if copiar(d["note"]) else "\n  Copia la nota de arriba.")
    elif args.cmd == "mensaje":
        print(d["message"])
        print("\n  Mensaje copiado al portapapeles." if copiar(d["message"]) else "")
    elif args.cmd == "marcar":
        estado = {"nota": "noted", "mensaje": "done", "saltar": "skipped"}[args.estado]
        await drafter.set_status(d["app_url"], estado)
        print(f"{args.numero}. {d['company']}: " + {
            "noted": "invitación enviada. Cuando acepte: tools/linkedin_mensajes.py mensaje N",
            "done": "mensaje enviado. Listo.", "skipped": "saltada."}[estado])


if __name__ == "__main__":
    asyncio.run(main())
