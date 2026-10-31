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

_AJUSTES = tool_settings()
PAIS = _AJUSTES["country"]
OUT_DIR = _AJUSTES["vacancies_dir"]
SEEN_FILE = ROOT / "data" / "vacantes_vistas.json"
# Tu país y tus regiones, como alternativas de una regex.
_ZONAS = "|".join(re.escape(z) for z in ([PAIS.lower()] if PAIS else []) + _AJUSTES["regions"])
UA = {"User-Agent": "Mozilla/5.0 (personal job search script)"}
HIMALAYAS_TERMS = ("developer", "engineer", "python", "data", "automation")
HIMALAYAS_PAGES = 8    # por término; 20 ofertas por página
WWR_CATEGORIAS = ("remote-programming-jobs", "remote-back-end-programming-jobs",
                  "remote-full-stack-programming-jobs", "remote-devops-sysadmin-jobs")
GETONBRD_TERMS =("junior", "python", "desarrollador", "developer", "datos", "automatización")
GETONBRD_PAGES = 4     # por término; 100 ofertas por página
# Ids de https://www.getonbrd.com/api/v0/seniorities: su búsqueda no filtra por nivel.
GETONBRD_NIVELES = {1: "Entry-level (sin experiencia)", 2: "Junior"}

JUNIOR_RE = re.compile(
    r"\b(junior|jr\.?|entry[- ]level|intern(ship)?|trainee|graduate|apprentice(ship)?|"
    r"becari[oa]|pasant[ei]a?|practicante|early[- ]career|new grad)\b", re.I)
SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|lead|staff|principal|head|director|manager|architect|vp|"
    r"annotators?|annotation)\b", re.I)   # el etiquetado de datos no es desarrollo
TECH_RE = re.compile(
    r"\b(developer|engineer|engineering|software|backend|back-end|frontend|front-end|"
    r"full[- ]?stack|python|fastapi|django|react|javascript|typescript|node|web|"
    r"data|analytics?|automation|ai|ml|machine learning|qa|devops|cloud|programador|"
    r"desarrollador(a)?|ingenier[oa]|datos|integrations?)\b", re.I)
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


def remotive():
    for j in get_json("https://remotive.com/api/remote-jobs?category=software-dev")["jobs"]:
        yield job("Remotive", j["id"], j["title"], j["company_name"],
                  j.get("candidate_required_location"), j["url"], j.get("publication_date"))


def remoteok():
    for j in get_json("https://remoteok.com/api")[1:]:   # el primero es el aviso legal
        yield job("RemoteOK", j["id"], j.get("position"), j.get("company"),
                  j.get("location") or "Remote", j.get("url"), j.get("date"))


def himalayas():
    # Su búsqueda filtra por nivel de entrada; la lista general solo trae lo más reciente de todo.
    for term in HIMALAYAS_TERMS:
        for page in range(1, HIMALAYAS_PAGES + 1):
            data = get_json("https://himalayas.app/jobs/api/search?seniority=Entry-level"
                            f"&q={term}&page={page}")
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
    for j in get_json("https://jobicy.com/api/v2/remote-jobs?count=50&industry=dev")["jobs"]:
        yield job("Jobicy", j["id"], j["jobTitle"], j["companyName"], j.get("jobGeo"),
                  j["url"], j.get("pubDate"), j.get("jobLevel"))


def arbeitnow():
    for j in get_json("https://www.arbeitnow.com/api/job-board-api")["data"]:
        if j.get("remote"):
            yield job("Arbeitnow", j["slug"], j["title"], j["company_name"], j.get("location"),
                      j["url"], datetime.fromtimestamp(j["created_at"], timezone.utc).isoformat())


def getonbrd():
    # API pública sin cuenta: https://www.getonbrd.com/api-doc.html
    for term in GETONBRD_TERMS:
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
    # RSS públicos por categoría; el título viene como "Empresa: Puesto".
    for cat in WWR_CATEGORIAS:
        req = urllib.request.Request(f"https://weworkremotely.com/categories/{cat}.rss", headers=UA)
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
    if not TECH_RE.search(j["titulo"]) or not is_open_to_you(j):
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


def main():
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
