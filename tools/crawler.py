#!/usr/bin/env python3
"""
Crawler fuer den Mammal Atlas (nur Standardbibliothek, Python 3.9+).

Holt aktuelle Daten aus offenen Quellen, um die Artendateien zu verbessern, neue Arten zu
ergaenzen und einen Datensatz ausgestorbener Arten aufzubauen:

  Wikidata (SPARQL)   Artenliste, IUCN-Status, Masse, Lebensdauer, Familie/Ordnung, Bilddatei, GBIF-ID
  Wikipedia (API)     Titelbild (nur freie Lizenzen), Einleitungstext, Kategorie ausgestorbener Saeuger
  Wikimedia Commons   Lizenz und Urheber je Bild
  GBIF (API)          Fundpunkte: grobe Gebietsnaeherung (konvexe Huelle) und letztes Fundjahr
  AnAge / PanTHERIA / COMBINE (lokale Dateien)   Lebensdauer und Koerpermasse

Schritte (jeder schreibt eine JSON-Datei, alle sind wiederaufnehmbar dank Cache):

  taxa      Arten aus Wikidata                    -> crawl/taxa.json
  enrich    Bilder, Lizenzen, Texte               -> crawl/taxa.json (ergaenzt)
  traits    Masse/Lebensdauer aus lokalen Tabellen -> crawl/taxa.json (ergaenzt)
  gbif      Gebietsnaeherung fuer Arten ohne Polygon -> crawl/gbif.json
  extinct   Datensatz ausgestorbener Arten         -> data/extinct_species.json
  merge     fuehrt alles in species_part_*.txt zusammen (Standard: nur Vorschau, --apply schreibt)

Hoeflichkeit: Wikimedia und GBIF verlangen einen aussagekraeftigen User-Agent mit Kontakt.
Bitte --contact (E-Mail oder Repo-URL) angeben. Anfragen laufen nacheinander mit Pause.
Lizenzen: Wikipedia-Texte CC BY-SA, Bilder je nach Commons-Lizenz (hier gespeichert), GBIF-Daten
CC0/CC BY je Datensatz. Die Verbreitungsgebiete aus GBIF sind grobe Naeherungen (rangeQuality=low).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

VERSION = "0.1.0"
WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
GBIF_API = "https://api.gbif.org/v1"

Q_MAMMALIA, Q_SPECIES, Q_SUBSPECIES, Q_ORDER, Q_FAMILY = "Q7377", "Q7432", "Q68947", "Q36602", "Q35409"

STATUS_BY_LABEL = {
    "least concern": "LC", "near threatened": "NT", "vulnerable species": "VU", "vulnerable": "VU",
    "endangered species": "EN", "endangered": "EN", "critically endangered": "CR",
    "extinct in the wild": "EW", "extinct species": "EX", "extinct": "EX", "data deficient": "DD",
}
STATUS_BY_QID = {  # Rueckfall, falls das Label fehlt
    "Q211005": "LC", "Q719675": "NT", "Q278113": "VU", "Q11394": "EN", "Q219127": "CR",
    "Q239509": "EW", "Q237350": "EX", "Q3245245": "DD",
}
SEVERITY = {"EX": 9, "EW": 8, "CR": 7, "EN": 6, "VU": 5, "NT": 4, "LC": 3, "DD": 2, "": 0}


class CrawlError(RuntimeError):
    pass


# --------------------------------------------------------------------------- HTTP mit Cache
class Http:
    """Einfacher GET-Client: Cache auf der Platte, Pause zwischen Anfragen, Wiederholung bei 429/5xx."""

    def __init__(self, cache_dir=".crawl_cache", contact="", delay=0.3, retries=4, timeout=90):
        self.cache = Path(cache_dir)
        self.contact = contact
        self.delay, self.retries, self.timeout = delay, retries, timeout
        self.requests_made = 0

    @property
    def user_agent(self):
        return f"MammalAtlasCrawler/{VERSION} ({self.contact or 'kein Kontakt angegeben'}) python-urllib"

    def get_json(self, url, params=None, headers=None):
        full = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        key = self.cache / (hashlib.sha1(full.encode("utf-8")).hexdigest() + ".json")
        if key.exists():
            return json.loads(key.read_text(encoding="utf-8"))
        hdr = {"User-Agent": self.user_agent, "Accept": "application/json"}
        hdr.update(headers or {})
        for attempt in range(self.retries + 1):
            time.sleep(self.delay)
            try:
                req = urllib.request.Request(full, headers=hdr)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self.requests_made += 1
                self.cache.mkdir(parents=True, exist_ok=True)
                key.write_text(json.dumps(data), encoding="utf-8")
                return data
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    wait = int(e.headers.get("Retry-After", 0) or 0) or 2 ** (attempt + 1) * 2
                    print(f"  HTTP {e.code}, warte {wait}s ...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise CrawlError(f"HTTP {e.code} fuer {full[:140]}") from e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                if attempt < self.retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise CrawlError(f"{type(e).__name__} fuer {full[:140]}: {e}") from e
        raise CrawlError("unerreichbar")


# --------------------------------------------------------------------------- Hilfen
def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def qid_of(uri):
    return uri.rsplit("/", 1)[-1] if uri else None


def bval(b, key):
    return (b.get(key) or {}).get("value")


def sparql(http, query):
    data = http.get_json(WIKIDATA_SPARQL, {"query": query, "format": "json"},
                         {"Accept": "application/sparql-results+json"})
    return data["results"]["bindings"]


def title_case_common(name):
    """'red fox' -> 'Red Fox'; vorhandene Grossschreibung bleibt erhalten."""
    def word(w):
        return "-".join(p[:1].upper() + p[1:] if p.islower() else p for p in w.split("-"))
    return " ".join(word(w) for w in name.split())


def status_code(label, qid=None):
    return STATUS_BY_LABEL.get((label or "").strip().lower()) or STATUS_BY_QID.get(qid or "", "")


def fmt_weight(grams):
    if grams is None or grams <= 0:
        return None
    return f"{grams / 1000:.3g} kg" if grams >= 1000 else f"{grams:.3g} g"


def fmt_lifespan(years):
    return f"{years:.3g} J." if years and years > 0 else None


def image_file_from_uri(uri):
    """http://commons.wikimedia.org/wiki/Special:FilePath/Name.jpg -> 'Name.jpg'"""
    if not uri:
        return None
    return urllib.parse.unquote(uri.rsplit("/", 1)[-1]).replace(" ", "_")


# --------------------------------------------------------------------------- Wikidata: Arten
def fetch_orders(http, only=None):
    q = (f"SELECT ?o ?name WHERE {{ ?o wdt:P105 wd:{Q_ORDER}; wdt:P171* wd:{Q_MAMMALIA}; wdt:P225 ?name . }}")
    orders = {qid_of(bval(b, "o")): bval(b, "name") for b in sparql(http, q)}
    if only:
        wanted = {o.strip().lower() for o in only}
        orders = {k: v for k, v in orders.items() if v.lower() in wanted}
    return orders


SPECIES_QUERY = """
SELECT ?item ?name ?label ?status ?statusLabel ?iucn ?gbif ?image ?mass ?life ?famName ?title WHERE {{
  {{ SELECT DISTINCT ?item WHERE {{
       ?item wdt:P105 wd:{rank}; wdt:P171+ wd:{order}; wdt:P225 ?n .
       ?item wdt:P141|wdt:P627 ?x .
     }} ORDER BY ?item LIMIT {limit} OFFSET {offset} }}
  ?item wdt:P225 ?name .
  OPTIONAL {{ ?item wdt:P141 ?status . }}
  OPTIONAL {{ ?item wdt:P627 ?iucn . }}
  OPTIONAL {{ ?item wdt:P846 ?gbif . }}
  OPTIONAL {{ ?item wdt:P18 ?image . }}
  OPTIONAL {{ ?item p:P2067/psn:P2067/wikibase:quantityAmount ?mass . }}
  OPTIONAL {{ ?item p:P2250/psn:P2250/wikibase:quantityAmount ?life . }}
  OPTIONAL {{ ?item wdt:P171+ ?fam . ?fam wdt:P105 wd:{family} ; wdt:P225 ?famName . }}
  OPTIONAL {{ ?art schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?title . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . ?item rdfs:label ?label . ?status rdfs:label ?statusLabel . }}
}}
"""


def collapse_rows(rows, order_name):
    """Mehrfachzeilen (mehrere Bilder/Status ...) je Art zu einem Datensatz zusammenfassen."""
    items = {}
    for b in rows:
        q = qid_of(bval(b, "item"))
        it = items.setdefault(q, {"qid": q, "sciName": bval(b, "name"), "order": order_name.upper(),
                                  "statuses": set(), "images": [], "label": bval(b, "label"),
                                  "family": "", "iucnId": None, "gbifId": None, "wikiTitle": None,
                                  "massKg": None, "lifespanSeconds": None})
        st = status_code(bval(b, "statusLabel"), qid_of(bval(b, "status")))
        if st:
            it["statuses"].add(st)
        img = image_file_from_uri(bval(b, "image"))
        if img and img not in it["images"]:
            it["images"].append(img)
        for src, dst in (("famName", "family"), ("iucn", "iucnId"), ("gbif", "gbifId"), ("title", "wikiTitle")):
            if bval(b, src) and not it[dst]:
                it[dst] = bval(b, src).upper() if dst == "family" else bval(b, src)
        if bval(b, "mass") and it["massKg"] is None:
            it["massKg"] = float(bval(b, "mass"))
        if bval(b, "life") and it["lifespanSeconds"] is None:
            it["lifespanSeconds"] = float(bval(b, "life"))
    out = []
    for it in items.values():
        sts = sorted(it.pop("statuses"), key=lambda s: -SEVERITY.get(s, 0))
        label, images = it.pop("label"), it.pop("images")
        mass, life = it.pop("massKg"), it.pop("lifespanSeconds")
        common = label if label and label.lower() != it["sciName"].lower() else (it["wikiTitle"] or "")
        if common.lower() == it["sciName"].lower():
            common = ""
        years = None if life is None else (life / 31556952.0 if life > 1e5 else life)   # Sekunden oder schon Jahre
        out.append({**it, "common": title_case_common(common) if common else it["sciName"],
                    "status": sts[0] if sts else "", "statusConflict": len(sts) > 1,
                    "imageFile": images[0] if images else None,
                    "massG": None if mass is None else mass * 1000.0, "lifespanYears": years})
    return out


def crawl_taxa(http, only_orders=None, limit_per_order=None, page=1500, rank=Q_SPECIES, log=print):
    orders = fetch_orders(http, only_orders)
    log(f"{len(orders)} Ordnungen")
    result = []
    for oq, oname in sorted(orders.items(), key=lambda kv: kv[1]):
        offset, got = 0, []
        while True:
            lim = min(page, (limit_per_order - len(got)) if limit_per_order else page)
            if lim <= 0:
                break
            rows = sparql(http, SPECIES_QUERY.format(rank=rank, order=oq, family=Q_FAMILY, limit=lim, offset=offset))
            part = collapse_rows(rows, oname)
            got.extend(part)
            if len(part) < lim:
                break
            offset += lim
        if got:
            log(f"  {oname}: {len(got)} Arten")
        result.extend(got)
    return result


# --------------------------------------------------------------------------- Wikipedia und Commons
def _shorten(text, limit=600):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[:end + 1] if end > 200 else cut.rstrip() + "…"


def _strip_html(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def wikipedia_pages(http, titles):
    """Titelbild (nur freie Lizenzen) und Einleitung fuer bis zu 20 Titel. Rueckgabe: {qid: Daten}."""
    data = http.get_json(WIKIPEDIA_API, {
        "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
        "titles": "|".join(titles), "prop": "pageimages|extracts|pageprops",
        "piprop": "thumbnail|name", "pithumbsize": "250", "pilicense": "free",
        "exintro": "1", "explaintext": "1", "exlimit": "20", "ppprop": "wikibase_item"})
    out = {}
    for page in data.get("query", {}).get("pages", []):
        if page.get("missing"):
            continue
        q = (page.get("pageprops") or {}).get("wikibase_item")
        if not q:
            continue
        out[q] = {"title": page.get("title"), "img": (page.get("thumbnail") or {}).get("source"),
                  "imageFile": page.get("pageimage"), "summary": _shorten(page.get("extract"))}
    return out


def commons_licenses(http, files):
    """Lizenz, Urheber und Seite je Commons-Datei (bis zu 50 pro Anfrage). Rueckgabe: {Dateiname_mit_Unterstrich: Daten}."""
    data = http.get_json(COMMONS_API, {
        "action": "query", "format": "json", "formatversion": "2", "prop": "imageinfo",
        "iiprop": "extmetadata", "iiextmetadatafilter": "LicenseShortName|Artist|LicenseUrl",
        "titles": "|".join("File:" + f for f in files)})
    out = {}
    for page in data.get("query", {}).get("pages", []):
        info = (page.get("imageinfo") or [{}])[0].get("extmetadata") or {}
        name = (page.get("title") or "")[5:].replace(" ", "_")
        if name:
            out[name] = {"license": (info.get("LicenseShortName") or {}).get("value", ""),
                         "artist": _strip_html((info.get("Artist") or {}).get("value", ""))[:120],
                         "licenseUrl": (info.get("LicenseUrl") or {}).get("value", ""),
                         "page": "https://commons.wikimedia.org/wiki/File:" + urllib.parse.quote(name)}
    return out


def enrich(http, species, log=print):
    """Ergaenzt wikiTitle-Arten um img, imageFile, summary und Lizenzangaben (veraendert die Liste)."""
    with_title = [s for s in species if s.get("wikiTitle")]
    by_qid = {s["qid"]: s for s in species}
    for i, batch in enumerate(chunks(with_title, 20), 1):
        for q, d in wikipedia_pages(http, [s["wikiTitle"] for s in batch]).items():
            s = by_qid.get(q)
            if not s:
                continue
            if d["img"]:
                s["img"], s["imageFile"] = d["img"], d["imageFile"] or s.get("imageFile")
            if d["summary"]:
                s["summary"] = d["summary"]
        if i % 10 == 0:
            log(f"  Wikipedia: {i * 20}/{len(with_title)}")
    files = sorted({s["imageFile"] for s in species if s.get("img") and s.get("imageFile")})
    meta = {}
    for batch in chunks(files, 50):
        meta.update(commons_licenses(http, batch))
    for s in species:
        m = meta.get(s.get("imageFile") or "")
        if m:
            s["imgLicense"], s["imgArtist"], s["imgPage"] = m["license"], m["artist"], m["page"]
    return species


# --------------------------------------------------------------------------- Eigenschaften aus lokalen Tabellen
def read_table(path):
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    lines = [l for l in text.splitlines() if l.strip()]
    delim = "\t" if lines[0].count("\t") >= lines[0].count(",") else ","
    import csv
    return list(csv.DictReader(lines, delimiter=delim))


def _num(v):
    try:
        x = float(str(v).strip())
    except ValueError:
        return None
    return None if x <= -900 or math.isnan(x) else x


def _col(row, *needles):
    for k in row:
        if all(n in k.lower() for n in needles):
            return row[k]
    return None


def load_traits(path, fmt):
    """Liest AnAge, PanTHERIA oder COMBINE. Rueckgabe: {'genus species': {'massG':..., 'lifespanYears':...}}."""
    out = {}
    for row in read_table(path):
        if fmt == "anage":
            if (row.get("Class") or "").strip().lower() != "mammalia":
                continue
            name = f"{(row.get('Genus') or '').strip()} {(row.get('Species') or '').strip()}"
            mass, life = _num(_col(row, "adult weight")), _num(_col(row, "maximum longevity"))
        elif fmt == "pantheria":
            name = (row.get("MSW05_Binomial") or "").strip()
            mass, life = _num(_col(row, "adultbodymass_g")), _num(_col(row, "maxlongevity_m"))
            life = None if life is None else life / 12.0
        elif fmt == "combine":
            name = (row.get("iucn2020_binomial") or row.get("phylacine_binomial") or "").strip().replace("_", " ")
            mass, life = _num(row.get("adult_mass_g")), _num(row.get("max_longevity_d"))
            life = None if life is None else life / 365.25
        else:
            raise CrawlError(f"unbekanntes Format {fmt}")
        if name and (mass or life):
            out[name.lower()] = {"massG": mass, "lifespanYears": life}
    return out


def apply_traits(species, tables):
    """tables: [(quelle, {name: werte}), ...] in Prioritaetsreihenfolge; fuellt nur fehlende Werte."""
    filled = 0
    for s in species:
        key = s["sciName"].lower()
        for src, table in tables:
            t = table.get(key)
            if not t:
                continue
            for field in ("massG", "lifespanYears"):
                if not s.get(field) and t.get(field):
                    s[field] = t[field]
                    s.setdefault("traitSource", {})[field] = src
                    filled += 1
    return filled


# --------------------------------------------------------------------------- GBIF
def gbif_key(http, sci_name):
    r = http.get_json(f"{GBIF_API}/species/match", {"name": sci_name, "kingdom": "Animalia", "class": "Mammalia", "strict": "true"})
    return r.get("usageKey") if r.get("matchType") == "EXACT" and r.get("rank") == "SPECIES" else None


def gbif_occurrences(http, key, max_records=300):
    out, offset = [], 0
    while len(out) < max_records:
        r = http.get_json(f"{GBIF_API}/occurrence/search", {
            "taxonKey": key, "hasCoordinate": "true", "hasGeospatialIssue": "false",
            "limit": min(300, max_records - len(out)), "offset": offset})
        res = r.get("results", [])
        out.extend({"lat": x.get("decimalLatitude"), "lng": x.get("decimalLongitude"), "year": x.get("year"),
                    "basis": x.get("basisOfRecord")} for x in res
                   if x.get("decimalLatitude") is not None and x.get("decimalLongitude") is not None)
        if r.get("endOfRecords") or not res:
            break
        offset += len(res)
    return out


def convex_hull(points):
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def hull_range(points, trim=0.05, min_points=5):
    """Grobe Gebietsnaeherung: Ausreisser je Achse kappen, konvexe Huelle. Format wie in den Artendateien ('lng|lat;...')."""
    if len(points) < min_points:
        return None
    lngs = sorted(p[0] for p in points)
    lats = sorted(p[1] for p in points)
    k = int(len(points) * trim)
    lo_x, hi_x, lo_y, hi_y = lngs[k], lngs[len(lngs) - 1 - k], lats[k], lats[len(lats) - 1 - k]
    keep = [p for p in points if lo_x <= p[0] <= hi_x and lo_y <= p[1] <= hi_y]
    if len(keep) < min_points or (hi_x - lo_x) > 180:
        return None
    hull = convex_hull([(round(x, 3), round(y, 3)) for x, y in keep])
    if len(hull) < 3:
        return None
    return ";".join(f"{x}|{y}" for x, y in hull)


def occurrence_summary(records):
    pts = [(r["lng"], r["lat"]) for r in records]
    living = [r for r in records if r["year"] and r["basis"] != "FOSSIL_SPECIMEN"]
    return {"n": len(records), "range": hull_range(pts),
            "lat": median_(sorted(r["lat"] for r in records)) if records else None,
            "lng": median_(sorted(r["lng"] for r in records)) if records else None,
            "lastYear": max((r["year"] for r in living), default=None)}


def median_(xs):
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def crawl_gbif(http, names, max_species=200, max_records=300, log=print):
    out = {}
    for i, name in enumerate(names[:max_species], 1):
        try:
            key = gbif_key(http, name)
            if not key:
                continue
            summary = occurrence_summary(gbif_occurrences(http, key, max_records))
            summary["gbifKey"] = key
            out[name] = summary
        except CrawlError as e:
            log(f"  GBIF {name}: {e}")
        if i % 25 == 0:
            log(f"  GBIF: {i}/{min(len(names), max_species)}")
    return out


# --------------------------------------------------------------------------- Ausgestorbene Arten
_Y = r"(1[5-9]\d\d|20[0-2]\d)"
YEAR_PATTERNS = [
    r"\blast\s+(?:known|confirmed|reliable|recorded|documented|verified|certain|wild|captive)?[^.]{0,90}?\b" + _Y + r"\b",
    r"\b(?:went|became|declared|considered|presumed|believed|thought|listed|classified)\s+(?:as\s+)?extinct[^.]{0,60}?\b" + _Y + r"\b",
    r"\bextinct\s+(?:in|by|since|around|circa|c\.)\s*(?:the\s+)?(?:year\s+)?" + _Y + r"\b",
    r"\bdied\s+(?:out\s+)?(?:in\s+)?" + _Y + r"\b",
]
CAUSE_PATTERNS = {
    "Jagd": r"\bhunt|\bbount|\bshot\b|\bkilled\b|\bculled|trapp|fur trade|sealing",
    "Wilderei": r"poach",
    "Lebensraumverlust": r"habitat (?:loss|destruction|degradation|fragmentation)|deforestation|land clearing|logging|agricultur|human settlement",
    "Eingeschleppte Arten": r"\bintroduced\b|invasive|\bferal\b|crazy ant|black rats?\b",
    "Krankheit": r"disease|epidemic|trypanos|rabies|distemper|virus|parasite|infect",
    "Klima / Meeresspiegel": r"climate change|sea[- ]level|inundat|storm surge|drought",
    "Fischerei": r"overfishing|bycatch|fishing net|entangle",
}


def extinction_hint(text):
    """Sucht im Text ein Aussterbejahr. Rueckgabe (jahr, ungefaehr, ausschnitt) oder None. Heuristik, bitte pruefen."""
    for pat in YEAR_PATTERNS:
        m = re.search(pat, text or "", flags=re.I)
        if m:
            before = text[max(0, m.start(1) - 14):m.start(1)].lower()
            approx = bool(re.search(r"(?:circa|c\.|around|about|approximately|probably|likely|roughly)\s*$", before))
            return int(m.group(1)), approx, text[max(0, m.start() - 20):m.end() + 20]
    return None


def cause_hints(text):
    return [label for label, pat in CAUSE_PATTERNS.items() if re.search(pat, text or "", flags=re.I)]


def category_titles(http, category, depth=1):
    """Seitentitel einer Wikipedia-Kategorie, optional mit Unterkategorien (depth)."""
    titles, queue, seen = [], [(category, 0)], set()
    while queue:
        cat, d = queue.pop(0)
        if cat in seen:
            continue
        seen.add(cat)
        cont = None
        while True:
            params = {"action": "query", "format": "json", "formatversion": "2", "list": "categorymembers",
                      "cmtitle": "Category:" + cat, "cmlimit": "500", "cmtype": "page|subcat"}
            if cont:
                params["cmcontinue"] = cont
            data = http.get_json(WIKIPEDIA_API, params)
            for m in data.get("query", {}).get("categorymembers", []):
                if m.get("ns") == 14:
                    if d < depth:
                        queue.append((m["title"][len("Category:"):], d + 1))
                else:
                    titles.append(m["title"])
            cont = (data.get("continue") or {}).get("cmcontinue")
            if not cont:
                break
    return titles


def titles_to_qids(http, titles):
    out = {}
    for batch in chunks(titles, 50):
        data = http.get_json(WIKIPEDIA_API, {"action": "query", "format": "json", "formatversion": "2", "redirects": "1",
                                              "titles": "|".join(batch), "prop": "pageprops", "ppprop": "wikibase_item"})
        for p in data.get("query", {}).get("pages", []):
            q = (p.get("pageprops") or {}).get("wikibase_item")
            if q:
                out[q] = p.get("title")
    return out


ITEM_QUERY = """
SELECT ?item ?name ?label ?status ?statusLabel ?iucn ?gbif ?image ?mass ?life ?famName ?orderName ?title WHERE {{
  VALUES ?item {{ {values} }}
  ?item wdt:P105 ?rank . FILTER(?rank IN (wd:{sp}, wd:{ssp}))
  ?item wdt:P171+ wd:{mammalia} ; wdt:P225 ?name .
  OPTIONAL {{ ?item wdt:P141 ?status . }}
  OPTIONAL {{ ?item wdt:P627 ?iucn . }}
  OPTIONAL {{ ?item wdt:P846 ?gbif . }}
  OPTIONAL {{ ?item wdt:P18 ?image . }}
  OPTIONAL {{ ?item p:P2067/psn:P2067/wikibase:quantityAmount ?mass . }}
  OPTIONAL {{ ?item p:P2250/psn:P2250/wikibase:quantityAmount ?life . }}
  OPTIONAL {{ ?item wdt:P171+ ?fam . ?fam wdt:P105 wd:{family} ; wdt:P225 ?famName . }}
  OPTIONAL {{ ?item wdt:P171+ ?ord . ?ord wdt:P105 wd:{order} ; wdt:P225 ?orderName . }}
  OPTIONAL {{ ?art schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?title . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en" . ?item rdfs:label ?label . ?status rdfs:label ?statusLabel . }}
}}
"""


def fetch_items(http, qids):
    """Details zu bekannten Wikidata-Items (Art oder Unterart, Saeugetier)."""
    out = []
    for batch in chunks(sorted(qids), 80):
        rows = sparql(http, ITEM_QUERY.format(values=" ".join("wd:" + q for q in batch), sp=Q_SPECIES, ssp=Q_SUBSPECIES,
                                              mammalia=Q_MAMMALIA, family=Q_FAMILY, order=Q_ORDER))
        by_order = {}
        for b in rows:
            by_order.setdefault(bval(b, "orderName") or "", []).append(b)
        for oname, part in by_order.items():
            out.extend(collapse_rows(part, oname))
    return out


def fetch_places(http, qids):
    """'Endemisch in' (P183) samt Koordinaten: {qid: {'label','lat','lng'}}."""
    out = {}
    for batch in chunks(sorted(qids), 80):
        q = ("SELECT ?item ?place ?placeLabel ?coord WHERE { VALUES ?item { " + " ".join("wd:" + x for x in batch) + " } "
             "?item wdt:P183 ?place . OPTIONAL { ?place wdt:P625 ?coord } "
             'SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . } }')
        for b in sparql(http, q):
            m = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", bval(b, "coord") or "")
            cur = out.get(qid_of(bval(b, "item")))
            if m and not (cur and cur.get("lat") is not None):
                out[qid_of(bval(b, "item"))] = {"label": bval(b, "placeLabel"), "lng": float(m.group(1)), "lat": float(m.group(2))}
    return out


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def build_extinct(http, taxa, category=None, overrides=None, gbif_max=150, depth=1, log=print):
    overrides = overrides or {}
    cands = {s["qid"]: dict(s, source="wikidata-iucn") for s in taxa if s.get("status") in ("EX", "EW")}
    if category:
        qids = titles_to_qids(http, category_titles(http, category, depth))
        extra = [q for q in qids if q not in cands]
        log(f"  Kategorie '{category}': {len(qids)} Seiten, {len(extra)} neue Kandidaten")
        for s in fetch_items(http, extra):
            s["status"] = s["status"] or "EX"
            cands[s["qid"]] = dict(s, source="wikipedia-category")
    species = list(cands.values())
    enrich(http, species, log)
    places = fetch_places(http, [s["qid"] for s in species])
    gb = crawl_gbif(http, [s["sciName"] for s in species], gbif_max, 300, log)
    out = []
    for s in species:
        ov = overrides.get(s["sciName"], {})
        g, place = gb.get(s["sciName"], {}), places.get(s["qid"], {})
        hint = extinction_hint(s.get("summary", ""))
        year, approx, ysrc = None, False, ""
        if "year" in ov:
            year, approx, ysrc = int(ov["year"]), bool(ov.get("yearApprox")), "override"
        elif hint:
            year, approx, ysrc = hint[0], hint[1], "wikipedia-text"
        elif g.get("lastYear"):
            year, ysrc = int(g["lastYear"]), "gbif-last-record"
        lat = ov.get("lat", place.get("lat", g.get("lat")))
        lng = ov.get("lng", place.get("lng", g.get("lng")))
        out.append({
            "id": slug(s["sciName"]), "sciName": s["sciName"], "common": s["common"], "order": s.get("order", ""),
            "family": s.get("family", ""), "status": s["status"], "year": year, "yearApprox": approx, "yearSource": ysrc,
            "causes": ov.get("causes", cause_hints(s.get("summary", ""))), "region": ov.get("region", place.get("label", "")),
            "lat": lat, "lng": lng, "range": g.get("range") or "", "rangeQuality": "low" if g.get("range") else "",
            "img": s.get("img", ""), "imgLicense": s.get("imgLicense", ""), "imgArtist": s.get("imgArtist", ""),
            "imgPage": s.get("imgPage", ""), "summary": ov.get("summary", s.get("summary", "")),
            "wikipedia": "https://en.wikipedia.org/wiki/" + urllib.parse.quote((s.get("wikiTitle") or s["sciName"]).replace(" ", "_")),
            "source": s["source"], "verified": bool(ov)})
    out.sort(key=lambda r: (r["year"] is None, r["year"] or 0))
    return out


# --------------------------------------------------------------------------- Zusammenfuehren
HEADER = ["common", "sciName", "order", "family", "status", "img", "w", "l", "range"]


def read_species(data_dir):
    rows = []
    for name in ("species_part_1.txt", "species_part_2.txt"):
        p = Path(data_dir) / name
        if p.exists():
            for line in p.read_text(encoding="utf-8").split("\n")[1:]:
                if line.strip():
                    parts = line.rstrip("\r").split("\t")
                    rows.append((parts + [""] * 9)[:9])
    return rows


def write_species(data_dir, rows):
    rows = sorted(rows, key=lambda r: r[1].lower())
    half = (len(rows) + 1) // 2
    for name, part in (("species_part_1.txt", rows[:half]), ("species_part_2.txt", rows[half:])):
        text = "\t".join(HEADER) + "\n" + "\n".join("\t".join(r) for r in part) + "\n"
        (Path(data_dir) / name).write_text(text, encoding="utf-8")


def needs_image(img):
    return not img.startswith("http") or "Status_iucn" in img


def merge_species(rows, taxa, gbif=None, update_status=False, add_new=True):
    """Fuehrt Crawler-Daten in die Zeilen ein. Rueckgabe: (neue Zeilen, Bericht, Herkunft). Loescht nie Zeilen."""
    gbif = gbif or {}
    rows = [list(r) for r in rows]
    idx = {r[1].lower().replace("_", " "): i for i, r in enumerate(rows)}
    unknown_w = next((r[6] for r in rows if r[6].startswith("?")), "? kg")
    unknown_l = next((r[7] for r in rows if r[7].startswith("?")), "? J.")
    rep = {"bilder": 0, "gewichte": 0, "lebensdauern": 0, "gebiete": 0, "status_geaendert": [], "neu": 0,
           "neu_ausgestorben": 0, "uebersprungen_ohne_status": 0}
    prov = {}
    for s in taxa:
        name = s["sciName"]
        g = gbif.get(name, {})
        if name.lower() in idx:
            row, p = rows[idx[name.lower()]], {}
            if needs_image(row[5]) and s.get("img") and s.get("imgLicense"):
                row[5] = s["img"]; rep["bilder"] += 1
                p["img"] = {"source": "wikipedia", "license": s["imgLicense"], "page": s.get("imgPage", "")}
            if row[6].startswith("?") and fmt_weight(s.get("massG")):
                row[6] = fmt_weight(s["massG"]); rep["gewichte"] += 1
                p["weight"] = (s.get("traitSource") or {}).get("massG", "wikidata")
            if row[7].startswith("?") and fmt_lifespan(s.get("lifespanYears")):
                row[7] = fmt_lifespan(s["lifespanYears"]); rep["lebensdauern"] += 1
                p["lifespan"] = (s.get("traitSource") or {}).get("lifespanYears", "wikidata")
            if update_status and s.get("status") and s["status"] != row[4]:
                rep["status_geaendert"].append((name, row[4], s["status"])); row[4] = s["status"]
                p["status"] = "wikidata"
            if not row[8].strip() and g.get("range"):
                row[8] = g["range"]; rep["gebiete"] += 1
                p["range"] = "gbif-hull (grobe Naeherung)"
            if p:
                prov[name.replace(" ", "_")] = p
        elif add_new:
            if s.get("status") in ("EX", "EW"):
                rep["neu_ausgestorben"] += 1
                continue
            if not s.get("status"):
                rep["uebersprungen_ohne_status"] += 1
                continue
            img = s["img"] if s.get("img") and s.get("imgLicense") else ""
            rows.append([s["common"], name.replace(" ", "_"), s.get("order", ""), s.get("family", ""), s["status"], img,
                         fmt_weight(s.get("massG")) or unknown_w, fmt_lifespan(s.get("lifespanYears")) or unknown_l,
                         g.get("range") or ""])
            rep["neu"] += 1
            prov[name.replace(" ", "_")] = {"added": date.today().isoformat(), "source": "wikidata",
                                            **({"range": "gbif-hull (grobe Naeherung)"} if g.get("range") else {}),
                                            **({"img": {"source": "wikipedia", "license": s["imgLicense"], "page": s.get("imgPage", "")}} if img else {})}
    return rows, rep, prov


def print_report(rep):
    print("\n=== Bericht ===")
    print(f"Neue lebende Arten:       {rep['neu']}  (ohne Status uebersprungen: {rep['uebersprungen_ohne_status']})")
    print(f"Bilder ersetzt/ergaenzt:  {rep['bilder']}")
    print(f"Gewichte ergaenzt:        {rep['gewichte']}")
    print(f"Lebensdauern ergaenzt:    {rep['lebensdauern']}")
    print(f"Gebiete ergaenzt (GBIF):  {rep['gebiete']}")
    print(f"Neu als ausgestorben:     {rep['neu_ausgestorben']}  (gehoeren in den Ausgestorbenen-Atlas)")
    ch = rep["status_geaendert"]
    print(f"Status abweichend:        {len(ch)}")
    for n, a, b in ch[:15]:
        print(f"   {n}: {a} -> {b}")


# --------------------------------------------------------------------------- Kommandozeile
def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"geschrieben: {path}")


def build_parser():
    p = argparse.ArgumentParser(prog="crawler.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--contact", default="", help="E-Mail oder Repo-URL fuer den User-Agent (erwuenscht)")
    p.add_argument("--cache", default=".crawl_cache", help="Cache-Ordner (Anfragen werden nur einmal gestellt)")
    p.add_argument("--delay", type=float, default=0.3, help="Pause zwischen Anfragen in Sekunden")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("taxa", help="Arten aus Wikidata")
    t.add_argument("--orders", default="", help="nur diese Ordnungen, kommagetrennt (z. B. Primates,Chiroptera)")
    t.add_argument("--limit-per-order", type=int, default=None, help="zum Testen: hoechstens N Arten je Ordnung")
    t.add_argument("--out", default="crawl/taxa.json")

    e = sub.add_parser("enrich", help="Bilder, Lizenzen und Texte aus Wikipedia/Commons")
    e.add_argument("--taxa", default="crawl/taxa.json")
    e.add_argument("--data", default="data", help="wenn gesetzt: nur Arten ohne echtes Bild oder neue Arten bearbeiten")
    e.add_argument("--all", action="store_true", help="alle Arten bearbeiten")

    tr = sub.add_parser("traits", help="Masse/Lebensdauer aus lokalen Tabellen")
    tr.add_argument("--taxa", default="crawl/taxa.json")
    tr.add_argument("--anage"); tr.add_argument("--pantheria"); tr.add_argument("--combine")

    g = sub.add_parser("gbif", help="Gebietsnaeherung fuer Arten ohne Polygon (und neue Arten)")
    g.add_argument("--taxa", default="crawl/taxa.json"); g.add_argument("--data", default="data")
    g.add_argument("--max-species", type=int, default=200); g.add_argument("--max-records", type=int, default=300)
    g.add_argument("--out", default="crawl/gbif.json")

    x = sub.add_parser("extinct", help="Datensatz ausgestorbener Arten")
    x.add_argument("--taxa", default="crawl/taxa.json")
    x.add_argument("--category", default="Mammal extinctions since 1500", help="Wikipedia-Kategorie ('' = nur Wikidata)")
    x.add_argument("--depth", type=int, default=1)
    x.add_argument("--overrides", default="data/extinct_overrides.json", help="eigene Korrekturen je Art (Jahr, Ort, Ursache ...)")
    x.add_argument("--gbif-max", type=int, default=150)
    x.add_argument("--out", default="data/extinct_species.json")

    m = sub.add_parser("merge", help="in species_part_*.txt zusammenfuehren (ohne --apply nur Vorschau)")
    m.add_argument("--data", default="data"); m.add_argument("--taxa", default="crawl/taxa.json")
    m.add_argument("--gbif", default="crawl/gbif.json")
    m.add_argument("--update-status", action="store_true", help="IUCN-Status aus Wikidata uebernehmen")
    m.add_argument("--no-new", action="store_true", help="keine neuen Arten anlegen")
    m.add_argument("--apply", action="store_true", help="Dateien wirklich schreiben (Sicherung in data/.backup)")
    return p


def main(argv=None, http=None):
    a = build_parser().parse_args(argv)
    http = http or Http(a.cache, a.contact, a.delay)
    if not a.contact and a.cmd not in ("merge", "traits"):
        print("Hinweis: Bitte --contact angeben (Wikimedia/GBIF erwarten einen erreichbaren Kontakt im User-Agent).", file=sys.stderr)

    if a.cmd == "taxa":
        only = [o for o in a.orders.split(",") if o.strip()]
        _save(a.out, crawl_taxa(http, only or None, a.limit_per_order))
    elif a.cmd == "enrich":
        taxa = _load(a.taxa)
        todo = taxa
        if not a.all and Path(a.data).exists():
            have = {r[1].lower().replace("_", " "): r for r in read_species(a.data)}
            todo = [s for s in taxa if s["sciName"].lower() not in have or needs_image(have[s["sciName"].lower()][5])]
        print(f"{len(todo)} von {len(taxa)} Arten werden angereichert")
        enrich(http, todo)
        _save(a.taxa, taxa)
    elif a.cmd == "traits":
        taxa = _load(a.taxa)
        tables = [(f, load_traits(p, f)) for f, p in (("anage", a.anage), ("pantheria", a.pantheria), ("combine", a.combine)) if p]
        print(f"{apply_traits(taxa, tables)} Werte ergaenzt")
        _save(a.taxa, taxa)
    elif a.cmd == "gbif":
        taxa = _load(a.taxa)
        have = {r[1].lower().replace("_", " "): r for r in read_species(a.data)}
        names = [s["sciName"] for s in taxa if s.get("status") not in ("EX", "EW")
                 and (s["sciName"].lower() not in have or not have[s["sciName"].lower()][8].strip())]
        print(f"{len(names)} Arten ohne Gebiet, bearbeitet werden bis zu {a.max_species}")
        _save(a.out, crawl_gbif(http, names, a.max_species, a.max_records))
    elif a.cmd == "extinct":
        ov = _load(a.overrides) if Path(a.overrides).exists() else {}
        data = build_extinct(http, _load(a.taxa), a.category or None, ov, a.gbif_max, a.depth)
        _save(a.out, {"meta": {"generated": date.today().isoformat(), "count": len(data), "crawler": VERSION,
                               "note": "Jahre, Orte und Ursachen sind Heuristiken. Korrekturen in extinct_overrides.json."},
                      "species": data})
    elif a.cmd == "merge":
        rows = read_species(a.data)
        gb = _load(a.gbif) if Path(a.gbif).exists() else {}
        new_rows, rep, prov = merge_species(rows, _load(a.taxa), gb, a.update_status, not a.no_new)
        print_report(rep)
        if a.apply:
            backup = Path(a.data) / ".backup" / date.today().isoformat()
            backup.mkdir(parents=True, exist_ok=True)
            for n in ("species_part_1.txt", "species_part_2.txt"):
                (backup / n).write_text((Path(a.data) / n).read_text(encoding="utf-8"), encoding="utf-8")
            write_species(a.data, new_rows)
            pp = Path(a.data) / "provenance.json"
            old = json.loads(pp.read_text(encoding="utf-8")) if pp.exists() else {}
            old.update(prov)
            _save(pp, old)
            print(f"Artendateien aktualisiert ({len(new_rows)} Arten), Sicherung: {backup}")
        else:
            print("\nVorschau, es wurde nichts geschrieben. Mit --apply uebernehmen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
