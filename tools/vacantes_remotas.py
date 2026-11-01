#!/usr/bin/env python3
"""Vacantes remotas junior abiertas desde tu país, de 8 fuentes públicas.

Local (no forma parte de Harvey upstream). Uso:
    ~/harvey/.venv/bin/python ~/harvey/tools/vacantes_remotas.py

Fuentes: Remotive, RemoteOK, Himalayas, Jobicy, Arbeitnow, Get on Board, We Work
Remotely (RSS) y el hilo mensual "Who is hiring?" de Hacker News. Cada lista enlaza
la oferta original y nombra su portal, como piden sus condiciones de uso.

Dos grupos: "junior" (dice junior, intern, entry-level...) y "estirarte" (no dice
nivel ni es senior/lead): las que puedes marcar como "aprender" o "interesante".
Remotive pide no consultar más de 4 veces al día: basta con una ejecución diaria.
Torre (API en beta privada) y Wellfound (sin API; su robots.txt cierra las
vacantes) no se consultan: úsalos en su web, o con sus alertas por correo.

Tu país y tus regiones salen de `job_search` en el config (country, regions).
Qué se busca sale de `job_search.vacancy_search` (search_terms, title_terms,
exclude_title_terms, remotive_categories, jobicy_industries). Lo que no pongas ahí
lo deduce el cerebro de tu perfil (job_search y product del config) y lo guarda
en data/vacancy_terms.json; si cambias el perfil, lo vuelve a deducir.
    --terminos    muestra los términos que usaría y termina
    --refrescar   vuelve a deducirlos aunque el perfil no haya cambiado
Salida en job_search.vacancies_dir (por defecto data/vacantes/):
    vacantes_remotas.md   la lista de hoy, las nuevas primero
    vacantes.csv          histórico de todo lo encontrado
Las ya vistas se recuerdan en ~/harvey/data/vacantes_vistas.json.
"""

import csv
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from harvey.job_search import tool_settings  # noqa: E402
from harvey.vacancy_terms import FIELDS, title_regex, vacancy_terms  # noqa: E402

_AJUSTES = tool_settings()
PAIS = _AJUSTES["country"]
OUT_DIR = _AJUSTES["vacancies_dir"]
SEEN_FILE = ROOT / "data" / "vacantes_vistas.json"
# Tu país y tus regiones, como alternativas de una regex.
_ZONAS = "|".join(re.escape(z) for z in ([PAIS.lower()] if PAIS else []) + _AJUSTES["regions"])
UA = {"User-Agent": "Mozilla/5.0 (personal job search script)"}
HIMALAYAS_PAGES = 8    # por término; 20 ofertas por página
GETONBRD_PAGES = 4     # por término; 100 ofertas por página
# Ids de https://www.getonbrd.com/api/v0/seniorities: su búsqueda no filtra por nivel.
GETONBRD_NIVELES = {1: "Entry-level (sin experiencia)", 2: "Junior"}

JUNIOR_RE = re.compile(
    r"\b(junior|jr\.?|entry[- ]level|intern(ship)?|trainee|graduate|apprentice(ship)?|"
    r"becari[oa]|pasant[ei]a?|practicante|early[- ]career|new grad)\b", re.I)
SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|lead|staff|principal|head|director|manager|architect|vp)\b", re.I)
# Qué buscar y qué títulos te sirven: del config, o deducido de tu perfil (cargar_terminos).
TERMINOS: dict = {}
TITULO_RE = None     # algún término de title_terms
EXCLUIR_RE = None    # algún término de exclude_title_terms
# Abierto desde tu país: sin restricción, mundial, o tu país/región.
OPEN_RE = re.compile(r"\b(worldwide|anywhere|global|remote" + (f"|{_ZONAS}" if _ZONAS else "") + r")\b", re.I)
# "Remote (US only)", "Remote - EU": remoto, pero cerrado a ti si no nombra también tu región.
CERRADO_RE = re.compile(
    r"\b(us|usa|u\.s\.?|united states|canada|eu|europe|uk|emea|apac|india|germany)\b", re.I)
LATAM_RE = re.compile(r"\b(worldwide|anywhere|global" + (f"|{_ZONAS}" if _ZONAS else "") + r")\b", re.I)
# Nivel que sí dice el aviso pero no es para estirarte.
NIVEL_ALTO_RE = re.compile(r"senior|lead|expert|mid|semi|staff|principal", re.I)


def get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.load(r)


def strip_tags(text):
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def job(source, id_, title, company, location, url, date, level=""):
    return {"fuente": source, "id": f"{source}:{id_}", "titulo": strip_tags(title),
            "empresa": strip_tags(company), "ubicacion": strip_tags(location) or "",
            "url": url, "fecha": (date or "")[:10], "nivel": level or ""}


def categorias_portales():
    """Las categorías que cada portal acepta hoy, para que el cerebro elija entre ellas."""
    out = {}
    try:
        out["remotive_categories"] = [c["slug"] for c in
                                      get_json("https://remotive.com/api/remote-jobs/categories")["jobs"]]
    except Exception:
        pass
    try:
        out["jobicy_industries"] = [i["industrySlug"] for i in
                                    get_json("https://jobicy.com/api/v2/remote-jobs?get=industries")["industries"]]
    except Exception:
        pass
    return out


def remotive():
    # Remotive pide no consultar más de 4 veces al día: una ejecución diaria, pocas categorías.
    for cat in TERMINOS["remotive_categories"]:
        for j in get_json("https://remotive.com/api/remote-jobs?category=" + urllib.parse.quote(cat))["jobs"]:
            yield job("Remotive", j["id"], j["title"], j["company_name"],
                      j.get("candidate_required_location"), j["url"], j.get("publication_date"))


def remoteok():
    for j in get_json("https://remoteok.com/api")[1:]:   # el primero es el aviso legal
        yield job("RemoteOK", j["id"], j.get("position"), j.get("company"),
                  j.get("location") or "Remote", j.get("url"), j.get("date"))


def himalayas():
    # Su búsqueda filtra por nivel de entrada; la lista general solo trae lo más reciente de todo.
    for term in TERMINOS["search_terms"]:
        for page in range(1, HIMALAYAS_PAGES + 1):
            data = get_json("https://himalayas.app/jobs/api/search?seniority=Entry-level"
                            f"&q={urllib.parse.quote(term)}&page={page}")
            jobs = data.get("jobs") or []
            for j in jobs:
                where = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
                yield job("Himalayas", j.get("guid"), j["title"], j.get("companyName"), where,
                          j.get("applicationLink") or j.get("guid"),
                          datetime.fromtimestamp(j["pubDate"], timezone.utc).isoformat()
                          if isinstance(j.get("pubDate"), (int, float)) else str(j.get("pubDate", "")),
                          ", ".join(j.get("seniority") or []))
            if len(jobs) < 20 or page * 20 >= (data.get("totalCount") or 0):
                break


def jobicy():
    for ind in TERMINOS["jobicy_industries"]:
        for j in get_json("https://jobicy.com/api/v2/remote-jobs?count=50&industry="
                          + urllib.parse.quote(ind)).get("jobs") or []:
            yield job("Jobicy", j["id"], j["jobTitle"], j["companyName"], j.get("jobGeo"),
                      j["url"], j.get("pubDate"), j.get("jobLevel"))


def arbeitnow():
    for j in get_json("https://www.arbeitnow.com/api/job-board-api")["data"]:
        if j.get("remote"):
            yield job("Arbeitnow", j["slug"], j["title"], j["company_name"], j.get("location"),
                      j["url"], datetime.fromtimestamp(j["created_at"], timezone.utc).isoformat())


def getonbrd():
    # API pública sin cuenta: https://www.getonbrd.com/api-doc.html
    for term in TERMINOS["search_terms"]:
        for page in range(1, GETONBRD_PAGES + 1):
            data = get_json("https://www.getonbrd.com/api/v0/search/jobs?per_page=100&lang=en"
                            f"&expand[]=company&query={urllib.parse.quote(term)}&page={page}")
            for j in data["data"]:
                a = j["attributes"]
                nivel = GETONBRD_NIVELES.get((a.get("seniority") or {}).get("data", {}).get("id"))
                if not nivel:
                    continue
                # fully_remote trae countries = ["Remote"]; el resto, el país de la oficina.
                paises = a.get("countries") or []
                if a.get("remote_modality") == "fully_remote":
                    donde = "Remote"
                elif PAIS and PAIS in paises:
                    donde = f"{PAIS} · " + a.get("remote_modality", "").replace("_", " ")
                else:
                    continue
                empresa = (((a.get("company") or {}).get("data") or {}).get("attributes") or {}).get("name", "")
                yield job("Get on Board", j["id"], a["title"], empresa, donde,
                          (j.get("links") or {}).get("public_url") or f"https://www.getonbrd.com/jobs/{j['id']}",
                          datetime.fromtimestamp(a["published_at"], timezone.utc).isoformat()
                          if isinstance(a.get("published_at"), (int, float)) else "", nivel)
            if page >= (data.get("meta") or {}).get("total_pages", 0):
                break


def weworkremotely():
    # El RSS general trae lo más reciente de todas las categorías; el filtro de
    # títulos decide cuáles te sirven. El título viene como "Empresa: Puesto".
    req = urllib.request.Request("https://weworkremotely.com/remote-jobs.rss", headers=UA)
    with urllib.request.urlopen(req, timeout=40) as r:
        root = ET.fromstring(r.read())
    for item in root.iter("item"):
        empresa, _, puesto = (item.findtext("title") or "").partition(": ")
        fecha = item.findtext("pubDate") or ""
        try:
            fecha = datetime.strptime(fecha, "%a, %d %b %Y %H:%M:%S %z").isoformat()
        except ValueError:
            pass
        yield job("We Work Remotely", item.findtext("guid") or item.findtext("link"),
                  puesto or empresa, empresa if puesto else "", item.findtext("region") or "",
                  item.findtext("link"), fecha)


def hackernews():
    # El hilo "Ask HN: Who is hiring?" del mes, por la API pública de Algolia.
    hilos = get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring"
                     "&query=" + urllib.parse.quote("Who is hiring?"))["hits"]
    hilo = next(h for h in hilos if h["title"].startswith("Ask HN: Who is hiring?"))
    for c in get_json(f"https://hn.algolia.com/api/v1/items/{hilo['objectID']}")["children"]:
        texto = strip_tags((c.get("text") or "").replace("<p>", "\n"))
        if not texto:
            continue
        # Primera línea: "Empresa | Puesto(s) | REMOTE (...) | ..."
        partes = [p.strip() for p in texto.split("\n", 1)[0].split("|")]
        donde = next((p for p in partes if re.search(r"\bremote\b", p, re.I)), "")
        if not donde:
            continue
        nivel = "junior" if JUNIOR_RE.search(texto) else ""
        yield job("Hacker News", c["id"], " | ".join(partes[1:])[:140] or partes[0], partes[0],
                  donde, f"https://news.ycombinator.com/item?id={c['id']}", c.get("created_at"), nivel)


def is_junior(j):
    if SENIOR_RE.search(j["titulo"]):
        return False
    return bool(JUNIOR_RE.search(j["titulo"])
                or re.search(r"entry|junior|intern", j["nivel"], re.I))


def grupo(j):
    """'junior', 'estirarte' (no dice nivel ni es senior) o None si no es para ti."""
    if not TITULO_RE or not TITULO_RE.search(j["titulo"]) or not is_open_to_you(j):
        return None
    if EXCLUIR_RE and EXCLUIR_RE.search(j["titulo"]):
        return None
    if is_junior(j):
        return "junior"
    if SENIOR_RE.search(j["titulo"]) or NIVEL_ALTO_RE.search(j["nivel"]):
        return None
    return "estirarte"


def is_open_to_you(j):
    u = j["ubicacion"]
    if not u:
        return True
    if CERRADO_RE.search(u) and not LATAM_RE.search(u):
        return False
    return bool(OPEN_RE.search(u))


def anexar_csv(path, fields, filas):
    """Agrega filas al histórico; si es de antes de una columna nueva, lo reescribe con ella."""
    viejas = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            r = csv.DictReader(f)
            if r.fieldnames != fields:
                viejas = list(r)
    if viejas or not path.exists():
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows({**v, "grupo": v.get("grupo") or "junior"} for v in viejas)
    with path.open("a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=fields, extrasaction="ignore").writerows(filas)


def cargar_terminos(refrescar=False):
    global TERMINOS, TITULO_RE, EXCLUIR_RE
    TERMINOS = vacancy_terms(categorias_portales, refresh=refrescar)
    TITULO_RE = title_regex(TERMINOS["title_terms"])
    EXCLUIR_RE = title_regex(TERMINOS["exclude_title_terms"])
    origen = {"config": "de tu config", "brain": "deducido de tu perfil ahora",
              "cache": "deducido de tu perfil", "default": "por defecto, de desarrollo"}
    print("Buscando con:")
    for campo in FIELDS:
        print(f"  {campo} ({origen[TERMINOS['sources'][campo]]}): "
              + (", ".join(TERMINOS[campo]) or "(ninguno)"))
    if TERMINOS["warning"]:
        print(f"Aviso: {TERMINOS['warning']}; donde falta, uso los términos por defecto.",
              file=sys.stderr)


def main():
    cargar_terminos(refrescar="--refrescar" in sys.argv)
    if "--terminos" in sys.argv:
        return
    found, errors = [], []
    for fetch in (remotive, remoteok, himalayas, jobicy, arbeitnow, getonbrd,
                  weworkremotely, hackernews):
        try:
            found.extend(fetch())
        except Exception as e:   # un portal caído no debe tumbar los demás
            errors.append(f"{fetch.__name__}: {str(e)[:80]}")

    hits = []
    for j in found:
        j["grupo"] = grupo(j)
        if j["grupo"]:
            hits.append(j)
    hits = list({j["id"]: j for j in hits}.values())

    try:
        seen = set(json.loads(SEEN_FILE.read_text()))
    except (OSError, ValueError):
        seen = set()
    for j in hits:
        j["nueva"] = j["id"] not in seen
    hits.sort(key=lambda j: j["fecha"], reverse=True)
    hits.sort(key=lambda j: not j["nueva"])

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d %H:%M")
    new = sum(j["nueva"] for j in hits)
    lines = [f"# Vacantes remotas abiertas desde {PAIS or 'tu país'} — {today}", "",
             f"Revisadas: {len(found)} ofertas en 8 fuentes · coinciden: {len(hits)} · nuevas: {new}", ""]
    if errors:
        lines += ["Portales que fallaron hoy: " + "; ".join(errors), ""]
    for g, titulo in (("junior", "Para tu nivel (junior, intern, entry-level)"),
                      ("estirarte", "Para estirarte (no dicen nivel: ¿puedo aprender? ¿interesante?)")):
        del_grupo = [j for j in hits if j["grupo"] == g]
        lines += [f"## {titulo} — {len(del_grupo)}", "",
                  "| | Puesto | Empresa | Ubicación | Fecha | Portal |", "|---|---|---|---|---|---|"]
        for j in del_grupo:
            mark = "🆕" if j["nueva"] else ""
            lines.append(f"| {mark} | [{j['titulo']}]({j['url']}) | {j['empresa']} | "
                         f"{j['ubicacion'] or 'sin restricción'} | {j['fecha']} | {j['fuente']} |")
        if not del_grupo:
            lines.append("| | (ninguna hoy) | | | | |")
        lines.append("")
    (OUT_DIR / "vacantes_remotas.md").write_text("\n".join(lines), encoding="utf-8")

    fields = ["encontrada", "fuente", "titulo", "empresa", "ubicacion", "fecha", "nivel", "url", "id", "grupo"]
    anexar_csv(OUT_DIR / "vacantes.csv", fields,
               [{**j, "encontrada": today} for j in hits if j["nueva"]])

    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.write_text(json.dumps(sorted(seen | {j["id"] for j in hits})))
    print(f"{len(found)} revisadas, {len(hits)} coinciden, {new} nuevas -> {OUT_DIR / 'vacantes_remotas.md'}")
    for e in errors:
        print("Aviso:", e, file=sys.stderr)


if __name__ == "__main__":
    main()
