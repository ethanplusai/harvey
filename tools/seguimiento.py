#!/usr/bin/env python3
"""Seguimiento de respuestas: qué canal y qué tipo de vacante te consigue entrevistas.

Local (no forma parte de Harvey upstream). Uso:
    ~/harvey/.venv/bin/python ~/harvey/tools/seguimiento.py                 # informe por canal y por peso
    ... lista [--pendientes]                    # postulaciones numeradas (--pendientes: sin respuesta)
    ... marcar N RESPUESTA [nota...]            # nada | rechazo | prueba | entrevista | oferta
    ... peso N pesado|aprender|interesante      # o p / a / i

Las postulaciones por formulario salen de data/aplicaciones.csv (las registra
aplicar.py; la respuesta la marcas tú aquí). Los correos en frío salen de
data/harvey.db: Harvey ya clasifica cada respuesta que llega a su hilo.
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

from aplicar import PESOS, REGISTRO, ROOT, guardar_registro, leer_registro

# De peor a mejor: una postulación cuenta en todas las etapas hasta la suya.
RESPUESTAS = ["nada", "rechazo", "prueba", "entrevista", "oferta"]
AVANCES = {"prueba", "entrevista", "oferta"}
# Días sin respuesta tras los que una postulación cuenta como "nada" en el informe.
DIAS_SIN_RESPUESTA = 21

# Cómo cuenta cada intención que el handler de Harvey le pone a una respuesta.
INTENCION_A_RESPUESTA = {
    "interested": "entrevista",   # quiere hablar: lo más cerca de una entrevista
    "question": "prueba",         # te preguntan algo: hay interés, aún sin entrevista
    "escalate": "prueba",
    "objection": "rechazo",
    "not_interested": "rechazo",
    "unsubscribe": "rechazo",
    # ooo / wrong_person: automáticas o de otra persona, no son respuesta
}


def plataforma(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    for ats in ("ashbyhq", "greenhouse", "lever", "workable"):
        if ats in host:
            return f"página de la empresa ({ats})"
    return host or "?"


def dias_desde(fecha: str) -> int:
    try:
        return (datetime.now() - datetime.fromisoformat(fecha)).days
    except ValueError:
        return 0


def enviadas() -> list[tuple[int, dict]]:
    """(número, fila) de las postulaciones enviadas; el número es estable entre comandos."""
    return [(n, f) for n, f in enumerate(leer_registro(), 1) if f["estado"] == "enviada"]


def correos_en_frio() -> list[dict]:
    """Un registro por persona a la que Harvey mandó el primer correo, con su mejor respuesta."""
    db = sqlite3.connect(ROOT / "data" / "harvey.db")
    personas = db.execute(
        "select prospect_id, min(sent_at) from outbox "
        "where kind = 'sequence' and step = 1 and status = 'sent' group by prospect_id").fetchall()
    intenciones = defaultdict(list)
    for pid, intent in db.execute("select prospect_id, intent from conversations"):
        intenciones[pid].append(intent or "")
    filas = []
    for pid, enviado in personas:
        mejor = ""
        for intent in intenciones.get(pid, []):
            r = INTENCION_A_RESPUESTA.get(intent, "")
            if r and (not mejor or RESPUESTAS.index(r) > RESPUESTAS.index(mejor)):
                mejor = r
        filas.append({"fecha": enviado or "", "respuesta": mejor})
    return filas


def resumir(filas: list[dict]) -> dict:
    c = {"total": len(filas), "esperando": 0, "respondieron": 0, "rechazo": 0, "avance": 0, "oferta": 0}
    for f in filas:
        r = f["respuesta"]
        if not r or r == "nada":
            if not r and dias_desde(f["fecha"]) < DIAS_SIN_RESPUESTA:
                c["esperando"] += 1
            continue
        c["respondieron"] += 1
        if r == "rechazo":
            c["rechazo"] += 1
        if r in AVANCES:
            c["avance"] += 1
        if r == "oferta":
            c["oferta"] += 1
    return c


def imprimir_tabla(titulo: str, grupos: dict[str, list[dict]]):
    print(f"\n{titulo}")
    print(f"  {'':34} {'Env.':>5} {'Esper.':>6} {'Resp.':>5} {'Rech.':>5} {'Avance':>6} {'Oferta':>6} {'% avance':>9}")
    for nombre, filas in sorted(grupos.items(), key=lambda kv: -len(kv[1])):
        c = resumir(filas)
        pct = f"{100 * c['avance'] / c['total']:.0f}%" if c["total"] else "-"
        print(f"  {nombre[:34]:34} {c['total']:>5} {c['esperando']:>6} {c['respondieron']:>5} "
              f"{c['rechazo']:>5} {c['avance']:>6} {c['oferta']:>6} {pct:>9}")


def informe():
    formularios = [f for _, f in enviadas()]
    por_canal = defaultdict(list)
    for f in formularios:
        por_canal[f"Formulario · {plataforma(f['url'])}"].append(f)
    try:
        por_canal["Correo en frío (Harvey)"] = correos_en_frio()
    except sqlite3.Error as e:
        print(f"(No pude leer los correos de Harvey: {e})")

    print(f"Seguimiento al {datetime.now():%Y-%m-%d}")
    imprimir_tabla("Por canal", por_canal)
    por_peso = defaultdict(list)
    for f in formularios:
        por_peso[f["peso"] or "(sin peso)"].append(f)
    imprimir_tabla("Formularios por peso", por_peso)

    print(f"\n  Esper. = sin respuesta y con menos de {DIAS_SIN_RESPUESTA} días: aún puede llegar.")
    print("  Avance = prueba técnica, entrevista u oferta.")
    sin_peso = sum(1 for f in formularios if not f["peso"])
    if sin_peso:
        print(f"\n  {sin_peso} postulación(es) sin peso: `seguimiento.py lista` y luego `peso N p|a|i`.")
    total = sum(len(v) for v in por_canal.values())
    if total and min(len(v) for v in por_canal.values()) < 30:
        print("  Ojo: con menos de ~30 por canal, una diferencia de porcentaje todavía puede ser suerte.")


def lista(solo_pendientes: bool):
    filas = enviadas()
    if solo_pendientes:
        filas = [(n, f) for n, f in filas if not f["respuesta"]]
    if not filas:
        print("Nada que mostrar.")
        return
    for n, f in filas:
        resp = f["respuesta"] or f"(sin respuesta, {dias_desde(f['fecha'])} días)"
        print(f"{n:>3}. {f['fecha'][:10]}  {f['titulo'][:45]} · {f['empresa'][:25]}")
        print(f"     peso: {f['peso'] or '-':12} respuesta: {resp}"
              + (f"  — {f['nota']}" if f["nota"] else ""))


def cambiar(numero: int, **cambios):
    filas = leer_registro()
    if not 1 <= numero <= len(filas) or filas[numero - 1]["estado"] != "enviada":
        sys.exit(f"No hay una postulación enviada con el número {numero}: mira `seguimiento.py lista`.")
    filas[numero - 1].update(cambios)
    guardar_registro(filas)
    f = filas[numero - 1]
    print(f"{numero}. {f['titulo']} · {f['empresa']}: " + ", ".join(f"{k} = {v}" for k, v in cambios.items() if v))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("lista")
    p.add_argument("--pendientes", action="store_true")
    p = sub.add_parser("marcar")
    p.add_argument("numero", type=int)
    p.add_argument("respuesta", choices=RESPUESTAS)
    p.add_argument("nota", nargs="*")
    p = sub.add_parser("peso")
    p.add_argument("numero", type=int)
    p.add_argument("peso", choices=list(PESOS) + list(PESOS.values()))
    args = ap.parse_args()

    if not REGISTRO.exists() and args.cmd:
        sys.exit(f"Aún no hay {REGISTRO}: se crea al registrar la primera postulación con aplicar.py.")
    if args.cmd == "lista":
        lista(args.pendientes)
    elif args.cmd == "marcar":
        cambiar(args.numero, respuesta=args.respuesta,
                fecha_respuesta=f"{datetime.now():%Y-%m-%d}", nota=" ".join(args.nota))
    elif args.cmd == "peso":
        cambiar(args.numero, peso=PESOS.get(args.peso, args.peso))
    else:
        informe()


if __name__ == "__main__":
    main()
