"""Offline-Tests des Crawlers: Antworten der APIs werden durch Attrappen ersetzt."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import crawler as c  # noqa: E402


class Fake:
    """Beantwortet get_json anhand von Matchern. Merkt sich alle Anfragen."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_json(self, url, params=None, headers=None):
        params = params or {}
        self.calls.append((url, params))
        for match, resp in self.routes:
            if match(url, params):
                return resp(params) if callable(resp) else resp
        raise AssertionError(f"unerwartete Anfrage: {url} {params}")


def B(**kv):                                   # SPARQL-Binding
    return {k: {"value": str(v)} for k, v in kv.items() if v is not None}


def sparql_has(*needles):
    return lambda url, p: url == c.WIKIDATA_SPARQL and all(n in p["query"] for n in needles)


def sparql_result(rows):
    return {"results": {"bindings": rows}}


# ------------------------------------------------------------------ kleine Funktionen
def test_status_und_formatierung():
    assert c.status_code("least concern") == "LC" and c.status_code("Endangered species") == "EN"
    assert c.status_code("", "Q237350") == "EX" and c.status_code("unbekannt") == ""
    assert c.fmt_weight(214) == "214 g" and c.fmt_weight(1400) == "1.4 kg" and c.fmt_weight(22.5) == "22.5 g"
    assert c.fmt_lifespan(12) == "12 J." and c.fmt_lifespan(4.5) == "4.5 J." and c.fmt_weight(None) is None
    assert c.title_case_common("red fox") == "Red Fox" and c.title_case_common("Gray-headed flying fox") == "Gray-Headed Flying Fox"
    assert c.image_file_from_uri("http://commons.wikimedia.org/wiki/Special:FilePath/Red%20fox.jpg") == "Red_fox.jpg"


def test_zeilen_werden_je_art_zusammengefasst():
    rows = [
        B(item="http://www.wikidata.org/entity/Q1", name="Vulpes vulpes", label="red fox", statusLabel="least concern",
          status="http://www.wikidata.org/entity/Q211005", famName="Canidae", title="Red fox", mass="5.2", life="315569520",
          image="http://commons.wikimedia.org/wiki/Special:FilePath/A.jpg"),
        B(item="http://www.wikidata.org/entity/Q1", name="Vulpes vulpes", label="red fox", statusLabel="vulnerable species",
          image="http://commons.wikimedia.org/wiki/Special:FilePath/B.jpg"),
    ]
    (s,) = c.collapse_rows(rows, "Carnivora")
    assert s["status"] == "VU" and s["statusConflict"] is True          # schaerfere Kategorie, Konflikt markiert
    assert s["common"] == "Red Fox" and s["family"] == "CANIDAE" and s["order"] == "CARNIVORA"
    assert s["imageFile"] == "A.jpg" and s["massG"] == 5200.0
    assert round(s["lifespanYears"], 1) == 10.0                         # 315569520 s = 10 Jahre


def test_taxa_mit_seitenweise_abfrage():
    orders = sparql_result([B(o="http://www.wikidata.org/entity/Q10", name="Primates")])
    def species(p):
        off = int(p["query"].split("OFFSET")[1].split()[0])
        rows = [B(item=f"http://www.wikidata.org/entity/Q{100 + off + i}", name=f"Genus sp{off + i}", statusLabel="least concern") for i in range(2)]
        return sparql_result(rows if off < 2 else rows[:1])               # zweite Seite kuerzer -> Ende
    http = Fake([(sparql_has("P171*"), orders), (sparql_has("SELECT DISTINCT ?item"), species)])
    taxa = c.crawl_taxa(http, page=2, log=lambda *a: None)
    assert len(taxa) == 3 and all(t["order"] == "PRIMATES" and t["status"] == "LC" for t in taxa)


# ------------------------------------------------------------------ Wikipedia und Commons
def test_enrich_bild_text_und_lizenz():
    pages = {"query": {"pages": [
        {"title": "Red fox", "pageprops": {"wikibase_item": "Q1"}, "pageimage": "Vulpes_vulpes.jpg",
         "thumbnail": {"source": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Vulpes_vulpes.jpg/250px-Vulpes_vulpes.jpg"},
         "extract": "The red fox is a mammal. " * 40},
        {"title": "Nichts", "missing": True}]}}
    lic = {"query": {"pages": [{"title": "File:Vulpes vulpes.jpg", "imageinfo": [{"extmetadata": {
        "LicenseShortName": {"value": "CC BY-SA 4.0"}, "Artist": {"value": '<a href="x">Jane  Doe</a>'},
        "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"}}}]}]}}
    http = Fake([(lambda u, p: u == c.WIKIPEDIA_API, pages), (lambda u, p: u == c.COMMONS_API, lic)])
    sp = [{"qid": "Q1", "wikiTitle": "Red fox"}, {"qid": "Q2"}]
    c.enrich(http, sp, log=lambda *a: None)
    assert sp[0]["img"].endswith("250px-Vulpes_vulpes.jpg") and len(sp[0]["summary"]) <= 601
    assert sp[0]["imgLicense"] == "CC BY-SA 4.0" and sp[0]["imgArtist"] == "Jane Doe"
    assert sp[0]["imgPage"].endswith("File:Vulpes_vulpes.jpg") and "img" not in sp[1]
    wiki_call = next(p for u, p in http.calls if u == c.WIKIPEDIA_API)
    assert wiki_call["pilicense"] == "free" and wiki_call["pithumbsize"] == "250"


# ------------------------------------------------------------------ Tabellen
def test_traits_aus_anage_pantheria_combine(tmp_path):
    (tmp_path / "anage.txt").write_text("HAGRID\tClass\tGenus\tSpecies\tMaximum longevity (yrs)\tAdult weight (g)\n"
                                        "1\tMammalia\tVulpes\tvulpes\t14.5\t5200\n2\tAves\tAnas\tanas\t20\t900\n", encoding="utf-8")
    (tmp_path / "pan.txt").write_text("MSW05_Binomial\t5-1_AdultBodyMass_g\t17-1_MaxLongevity_m\nCanis lupus\t31756\t354\nFoo bar\t-999\t-999\n", encoding="utf-8")
    (tmp_path / "comb.csv").write_text("iucn2020_binomial,adult_mass_g,max_longevity_d\nFelis catus,4000,7305\n", encoding="utf-8")
    a, p, k = (c.load_traits(tmp_path / f, fmt) for f, fmt in (("anage.txt", "anage"), ("pan.txt", "pantheria"), ("comb.csv", "combine")))
    assert a == {"vulpes vulpes": {"massG": 5200.0, "lifespanYears": 14.5}}
    assert round(p["canis lupus"]["lifespanYears"], 1) == 29.5 and "foo bar" not in p
    assert round(k["felis catus"]["lifespanYears"], 1) == 20.0
    sp = [{"sciName": "Vulpes vulpes", "massG": None}, {"sciName": "Canis lupus", "massG": 30000.0}]
    assert c.apply_traits(sp, [("anage", a), ("pantheria", p)]) == 3        # Fuchs: 2, Wolf: nur Lebensdauer
    assert sp[1]["massG"] == 30000.0 and sp[0]["traitSource"]["massG"] == "anage"   # vorhandenes bleibt


# ------------------------------------------------------------------ GBIF
def test_huelle_und_ausreisser():
    pts = [(10 + i * 0.1, 5 + j * 0.1) for i in range(10) for j in range(10)] + [(170.0, -60.0)]   # ein Ausreisser
    r = c.hull_range(pts)
    xs = [float(p.split("|")[0]) for p in r.split(";")]
    assert max(xs) < 12 and len(r.split(";")) >= 3                       # Ausreisser gekappt
    assert c.hull_range(pts[:3]) is None                                  # zu wenige Punkte
    assert c.hull_range([(-170, 0), (170, 0), (0, 5), (1, 6), (2, 7), (3, 1)], trim=0) is None   # ueber die Datumsgrenze


def test_gbif_letztes_jahr_ohne_fossilien():
    http = Fake([(lambda u, p: u.endswith("/species/match"), {"matchType": "EXACT", "rank": "SPECIES", "usageKey": 42}),
                 (lambda u, p: u.endswith("/occurrence/search"), {"endOfRecords": True, "results": [
                     {"decimalLatitude": -42.0, "decimalLongitude": 146.0, "year": 1930, "basisOfRecord": "PRESERVED_SPECIMEN"},
                     {"decimalLatitude": -41.5, "decimalLongitude": 146.5, "year": 1936, "basisOfRecord": "PRESERVED_SPECIMEN"},
                     {"decimalLatitude": -30.0, "decimalLongitude": 140.0, "year": 2001, "basisOfRecord": "FOSSIL_SPECIMEN"},
                     {"decimalLatitude": None, "decimalLongitude": None}]})])
    out = c.crawl_gbif(http, ["Thylacinus cynocephalus"], log=lambda *a: None)["Thylacinus cynocephalus"]
    assert out["lastYear"] == 1936 and out["n"] == 2 and out["fossils"] == 1 and out["gbifKey"] == 42 and out["range"] is None   # <5 Punkte
    assert out["lastSpecimenYear"] == 1936


# ------------------------------------------------------------------ Hinweise aus dem Text
def test_aussterbejahr_und_ursachen():
    t = ("The thylacine was hunted with bounties. The last known individual died in captivity in 1936. "
         "Habitat loss and disease contributed.")
    year, approx, _ = c.extinction_hint(t)
    assert year == 1936 and approx is False
    assert c.extinction_hint("It went extinct around 1903 on Christmas Island.")[:2] == (1903, True)
    assert c.extinction_hint("A rodent living in caves.") is None
    assert {"Jagd", "Lebensraumverlust", "Krankheit"} <= set(c.cause_hints(t))


# ------------------------------------------------------------------ Zusammenfuehren
ROWS = [
    ["Red Fox", "Vulpes_vulpes", "CARNIVORA", "CANIDAE", "LC", "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4b/Status_iucn3.1_LC.svg/250px-Status_iucn3.1_LC.svg.png", "? kg", "? J.", "1|1;2|1;2|2"],
    ["Gray Wolf", "Canis_lupus", "CARNIVORA", "CANIDAE", "LC", "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a0/Wolf.jpg/250px-Wolf.jpg", "30 kg", "? J.", ""],
]
TAXA = [
    {"sciName": "Vulpes vulpes", "status": "NT", "img": "https://x/fox.jpg", "imgLicense": "CC BY 4.0", "imgPage": "p", "massG": 5200, "lifespanYears": 14.5},
    {"sciName": "Canis lupus", "status": "LC", "img": "https://x/neu.jpg", "imgLicense": "CC0", "massG": 99999, "lifespanYears": 12},
    {"sciName": "Aaa bbb", "common": "Neue Art", "order": "RODENTIA", "family": "MURIDAE", "status": "EN", "massG": 25, "lifespanYears": None, "img": "https://x/n.jpg", "imgLicense": "CC BY-SA 4.0"},
    {"sciName": "Ccc ddd", "common": "Ohne Status", "status": ""},
    {"sciName": "Eee fff", "common": "Weg", "status": "EX"},
]


def test_merge_fuellt_nur_luecken_und_loescht_nichts():
    rows, rep, prov = c.merge_species(ROWS, TAXA, {"Canis lupus": {"range": "5|5;6|5;6|6"}})
    by = {r[1]: r for r in rows}
    fox, wolf, new = by["Vulpes_vulpes"], by["Canis_lupus"], by["Aaa_bbb"]
    assert fox[5] == "https://x/fox.jpg" and fox[6] == "5.2 kg" and fox[7] == "14.5 J." and fox[4] == "LC"   # Status unveraendert
    assert wolf[5].endswith("Wolf.jpg") and wolf[6] == "30 kg"            # vorhandenes Foto/Gewicht bleibt
    assert wolf[7] == "12 J." and wolf[8] == "5|5;6|5;6|6"
    assert new[:5] == ["Neue Art", "Aaa_bbb", "RODENTIA", "MURIDAE", "EN"] and new[6] == "25 g" and new[7] == "? J."
    assert len(rows) == 3 and rep["neu"] == 1 and rep["neu_ausgestorben"] == 1 and rep["uebersprungen_ohne_status"] == 1
    assert prov["Aaa_bbb"]["added"] and prov["Canis_lupus"]["range"].startswith("gbif-hull")
    assert ROWS[0][6] == "? kg"                                           # Eingabe nicht veraendert


def test_merge_status_nur_mit_option():
    _, rep, _ = c.merge_species(ROWS, TAXA)
    assert rep["status_geaendert"] == []
    rows, rep, prov = c.merge_species(ROWS, TAXA, update_status=True)
    assert rep["status_geaendert"] == [("Vulpes vulpes", "LC", "NT")] and prov["Vulpes_vulpes"]["status"] == "wikidata"


def test_merge_cli_vorschau_und_apply(tmp_path):
    d = tmp_path / "data"; d.mkdir()
    c.write_species(d, [list(r) for r in ROWS])
    (tmp_path / "taxa.json").write_text(json.dumps(TAXA), encoding="utf-8")
    before = (d / "species_part_1.txt").read_text(encoding="utf-8")
    c.main(["merge", "--data", str(d), "--taxa", str(tmp_path / "taxa.json"), "--gbif", str(tmp_path / "x.json")])
    assert (d / "species_part_1.txt").read_text(encoding="utf-8") == before            # Vorschau schreibt nichts
    c.main(["merge", "--data", str(d), "--taxa", str(tmp_path / "taxa.json"), "--gbif", str(tmp_path / "x.json"), "--apply"])
    rows = c.read_species(d)
    assert len(rows) == 3 and any(r[1] == "Aaa_bbb" for r in rows)
    assert list((d / ".backup").glob("*/species_part_1.txt")) and (d / "provenance.json").exists()
    assert (d / "species_part_1.txt").read_text(encoding="utf-8").startswith("common\tsciName")


# ------------------------------------------------------------------ Ausgestorbene Arten
def test_ausgestorbenen_datensatz_aufbauen(tmp_path):
    Q = "http://www.wikidata.org/entity/"
    thylacine = B(item=Q + "Q30", name="Thylacinus cynocephalus", label="thylacine", status=Q + "Q237350", statusLabel="extinct species",
                  famName="Thylacinidae", orderName="Dasyuromorphia", title="Thylacine")
    http = Fake([
        (lambda u, p: p.get("list") == "categorymembers", {"query": {"categorymembers": [
            {"ns": 0, "title": "Quagga"}, {"ns": 14, "title": "Category:Sub"}]}}),
        (lambda u, p: p.get("prop") == "pageprops", {"query": {"pages": [{"title": "Quagga", "pageprops": {"wikibase_item": "Q31"}}]}}),
        (sparql_has("FILTER(?rank IN"), sparql_result([
            B(item=Q + "Q31", name="Equus quagga quagga", label="quagga", orderName="Perissodactyla", famName="Equidae", title="Quagga")])),
        (lambda u, p: u == c.WIKIPEDIA_API and "pageimages" in p.get("prop", ""), {"query": {"pages": [
            {"title": "Thylacine", "pageprops": {"wikibase_item": "Q30"}, "pageimage": "Thylacine.jpg",
             "thumbnail": {"source": "https://img/thyl.jpg"},
             "extract": "The thylacine was hunted. The last known individual died in 1936. Habitat loss followed."},
            {"title": "Quagga", "pageprops": {"wikibase_item": "Q31"}, "extract": "Last captive died in 1883."}]}}),
        (lambda u, p: u == c.COMMONS_API, {"query": {"pages": [{"title": "File:Thylacine.jpg", "imageinfo": [{"extmetadata": {
            "LicenseShortName": {"value": "Public domain"}, "Artist": {"value": "Baker"}}}]}]}}),
        (sparql_has("wdt:P183"), sparql_result([B(item=Q + "Q30", place=Q + "Q40", placeLabel="Tasmania", coord="Point(146.5 -42.0)")])),
        (sparql_has("wdt:P8556"), sparql_result([B(item=Q + "Q31", y="1883")])),
        (lambda u, p: u.endswith("/species/match"), {"matchType": "NONE"}),
    ])
    taxa = [{"qid": "Q30", "sciName": "Thylacinus cynocephalus", "common": "Thylacine", "status": "EX", "wikiTitle": "Thylacine", "order": "DASYUROMORPHIA", "family": "THYLACINIDAE"},
            {"qid": "Q99", "sciName": "Vulpes vulpes", "common": "Red Fox", "status": "LC"}]
    ov = {"Equus quagga quagga": {"lat": -32.0, "lng": 22.0, "region": "Suedafrika"}}
    out = c.build_extinct(http, taxa, "Mammal extinctions since 1500", ov, log=lambda *a: None)
    by = {r["id"]: r for r in out}
    assert set(by) == {"thylacinus_cynocephalus", "equus_quagga_quagga"}          # Rotfuchs nicht dabei
    t, q = by["thylacinus_cynocephalus"], by["equus_quagga_quagga"]
    assert t["year"] == 1936 and t["yearSource"] == "wikipedia-text" and t["region"] == "Tasmania" and (t["lat"], t["lng"]) == (-42.0, 146.5)
    assert t["img"] == "https://img/thyl.jpg" and t["imgLicense"] == "Public domain" and {"Jagd", "Lebensraumverlust"} <= set(t["causes"])
    assert q["year"] == 1883 and q["yearSource"] == "wikidata" and q["lat"] == -32.0 and q["region"] == "Suedafrika" and q["verified"] and q["source"] == "wikipedia-category"
    assert [r["year"] for r in out] == [1883, 1936]                              # nach Jahr sortiert


# ------------------------------------------------------------------ Fehler, die erst bei echten Antworten auftreten
def test_qid_als_label_wird_ignoriert_und_arten_nur_einmal():
    Q = "http://www.wikidata.org/entity/"
    (s,) = c.collapse_rows([B(item=Q + "Q5", name="Aaa bbb", label="Q5", title="Aaa bbb")], "Rodentia")
    assert s["common"] == "Aaa bbb"
    orders = sparql_result([B(o=Q + "Q10", name="Artiodactyla"), B(o=Q + "Q11", name="Cetartiodactyla")])
    whale = sparql_result([B(item=Q + "Q7", name="Balaena mysticetus", statusLabel="least concern")])
    http = Fake([(sparql_has("P171*"), orders), (sparql_has("SELECT DISTINCT ?item"), whale)])
    assert [t["qid"] for t in c.crawl_taxa(http, log=lambda *a: None)] == ["Q7"]


def test_http_fehlerantwort_wird_nicht_gecacht(tmp_path, monkeypatch):
    class Resp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(c.urllib.request, "urlopen", lambda req, timeout: Resp(b'{"error": {"code": "toomanyvalues", "info": "zu viele"}}'))
    http = c.Http(tmp_path, delay=0)
    with pytest.raises(c.CrawlError, match="toomanyvalues"):
        http.get_json(c.WIKIPEDIA_API, {"action": "query"})
    assert not list(tmp_path.glob("*.json"))
    assert c._retry_after("12") == 12 and c._retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 1 and c._retry_after("x") is None


def test_wikipedia_folgt_continue_und_entities_im_urheber():
    first = {"continue": {"excontinue": "1", "continue": "||"}, "query": {"pages": [
        {"title": "A", "pageprops": {"wikibase_item": "Q1"}, "extract": "Erster Text."},
        {"title": "B", "pageprops": {"wikibase_item": "Q2"}}]}}
    second = {"query": {"pages": [{"title": "A", "pageprops": {"wikibase_item": "Q1"}},
                                  {"title": "B", "pageprops": {"wikibase_item": "Q2"}, "extract": "Zweiter Text."}]}}
    http = Fake([(lambda u, p: "excontinue" in p, second), (lambda u, p: u == c.WIKIPEDIA_API, first)])
    out = c.wikipedia_pages(http, ["A", "B"])
    assert out["Q1"]["summary"] == "Erster Text." and out["Q2"]["summary"] == "Zweiter Text."
    assert c._strip_html("Jane&nbsp;Doe &amp; Co") == "Jane Doe & Co"


def test_gbif_synonym_nimmt_anerkannten_schluessel():
    http = Fake([(lambda u, p: u.endswith("/species/match"),
                  {"matchType": "EXACT", "rank": "SPECIES", "status": "SYNONYM", "usageKey": 1, "acceptedUsageKey": 2})])
    assert c.gbif_key(http, "Alter name") == 2


def test_lebensdauer_aus_tabelle_ersetzt_wikidata():
    sp = [{"sciName": "Dasyphractus vellerosus", "synonyms": ["Chaetophractus vellerosus"], "lifespanYears": 9.0, "massG": 800.0}]
    c.apply_traits(sp, [("anage", {"chaetophractus vellerosus": {"massG": 900.0, "lifespanYears": 30.0}})])
    assert sp[0]["lifespanYears"] == 30.0 and sp[0]["massG"] == 800.0 and sp[0]["traitSource"] == {"lifespanYears": "anage"}


# ------------------------------------------------------------------ MDD
MDD_CSV = ("sciName,id,mainCommonName,order,family,iucnStatus,extinct,domestic\n"
           "Vulpes_vulpes,1,Red Fox,Carnivora,Canidae,LC,0,0\n"
           "Dasyphractus_vellerosus,2,Screaming Hairy Armadillo,Cingulata,Chlamyphoridae,LC (as Chaetophractus vellerosus),0,0\n"
           "Dactylonax_palpator,3,Long-fingered Triok,Diprotodontia,Petauridae,NA,0,0\n"
           "Neue_art,4,NA,Rodentia,Muridae,NE,0,0\n"
           "Canis_familiaris,5,Dog,Carnivora,Canidae,NE,0,1\n"
           "Bettongia_penicillata,6,Southeastern Bettong,Diprotodontia,Potoroidae,NT,1,0\n")
SYN_CSV = ("MDD_species,MDD_original_combination,MDD_normalized_original_combination,MDD_original_rank\n"
           "Dactylonax palpator,Dactylopsila palpator,,species\n"
           "Vulpes vulpes,Canis alopex,,species\n")


def test_mdd_einlesen_und_synonyme(tmp_path):
    (tmp_path / "MDD_v2.5_6species.csv").write_text(MDD_CSV, encoding="utf-8")
    (tmp_path / "Species_Syn_Current_v2.5.csv").write_text(SYN_CSV, encoding="utf-8")
    f, syn = c.find_mdd_files(tmp_path)
    taxa = c.load_mdd(f)
    by = {t["sciName"]: t for t in taxa}
    arm, neu = by["Dasyphractus vellerosus"], by["Neue art"]
    assert arm["status"] == "LC" and arm["synonyms"] == ["Chaetophractus vellerosus"] and arm["order"] == "CINGULATA"
    assert neu["common"] == "Neue art" and neu["status"] == "NE" and by["Dactylonax palpator"]["status"] == ""
    assert by["Canis familiaris"]["domestic"] and by["Bettongia penicillata"]["extinct"]
    assert c.link_synonyms(taxa, syn, ["Dactylopsila_palpator", "Vulpes_vulpes", "Unbekannt_x"]) == 1
    assert by["Dactylonax palpator"]["synonyms"] == ["Dactylopsila palpator"]
    assert len(c.load_mdd(f, ["Carnivora"])) == 2 and len(c.load_mdd(f, limit_per_order=1)) == 4


def test_mdd_wikidata_abgleich_ueber_synonym():
    Q = "http://www.wikidata.org/entity/"
    rows = sparql_result([B(item=Q + "Q9", name="Chaetophractus vellerosus", label="screaming hairy armadillo",
                            title="Screaming hairy armadillo", gbif="2440", iucn="89604", mass="0.86")])
    http = Fake([(sparql_has("VALUES ?name"), rows)])
    taxa = [{"sciName": "Dasyphractus vellerosus", "synonyms": ["Chaetophractus vellerosus"], "qid": None, "wikiTitle": None,
             "imageFile": None, "iucnId": None, "gbifId": None, "massG": None, "lifespanYears": None}]
    c.attach_wikidata(http, taxa, log=lambda *a: None)
    assert taxa[0]["qid"] == "Q9" and taxa[0]["gbifId"] == "2440" and taxa[0]["massG"] == 860.0
    assert '"Dasyphractus vellerosus"' in http.calls[0][1]["query"]


def test_merge_mit_mdd_synonym_ne_haustier_und_ohne_doppel():
    rows = [["Screaming Hairy Armadillo", "Chaetophractus_vellerosus", "CINGULATA", "CHLAMYPHORIDAE", "LC", "", "? kg", "? J.", ""],
            ["Brush-tailed Bettong", "Bettongia_penicillata", "DIPROTODONTIA", "POTOROIDAE", "NT", "", "? kg", "? J.", ""]]
    taxa = [{"sciName": "Dasyphractus vellerosus", "synonyms": ["Chaetophractus vellerosus"], "status": "LC", "massG": 860.0, "source": "mdd"},
            {"sciName": "Bettongia penicillata", "synonyms": [], "status": "NT", "extinct": True, "source": "mdd"},
            {"sciName": "Neue art", "common": "Neue art", "order": "RODENTIA", "family": "MURIDAE", "status": "NE", "source": "mdd"},
            {"sciName": "Neue art", "common": "Neue art", "order": "RODENTIA", "family": "MURIDAE", "status": "NE", "source": "mdd"},
            {"sciName": "Canis familiaris", "status": "NE", "domestic": True, "source": "mdd"}]
    out, rep, prov = c.merge_species(rows, taxa, update_status=True)
    names = [r[1] for r in out]
    assert names == ["Chaetophractus_vellerosus", "Bettongia_penicillata", "Neue_art"]          # kein Doppel, kein Hund
    assert out[0][6] == "860 g" and prov["Chaetophractus_vellerosus"]["weight"] == "wikidata"
    assert rep["namensabweichungen"] == [("Chaetophractus_vellerosus", "Dasyphractus vellerosus")]
    assert rep["laut_quelle_ausgestorben"] == [("Bettongia_penicillata", "NT")] and out[1][4] == "NT"
    assert rep["uebersprungen_haustier"] == 1 and out[2][4] == "NE" and prov["Neue_art"]["source"] == "mdd"
    _, rep, _ = c.merge_species([["X", "Aaa_bbb", "", "", "DD", "", "? kg", "? J.", ""]], [{"sciName": "Aaa bbb", "status": "NE"}], update_status=True)
    assert rep["status_geaendert"] == []                                   # NE ersetzt keine echte Bewertung


def test_merge_apply_ueberschreibt_keine_sicherung(tmp_path):
    d = tmp_path / "data"; d.mkdir()
    c.write_species(d, [list(r) for r in ROWS])
    original = (d / "species_part_1.txt").read_text(encoding="utf-8")
    (tmp_path / "taxa.json").write_text(json.dumps(TAXA), encoding="utf-8")
    args = ["merge", "--data", str(d), "--taxa", str(tmp_path / "taxa.json"), "--gbif", str(tmp_path / "x.json"), "--apply"]
    c.main(args); c.main(args)                                            # zweimal am selben Tag
    backups = sorted((d / ".backup").iterdir())
    assert len(backups) == 2 and (backups[0] / "species_part_1.txt").read_text(encoding="utf-8") == original
    assert len(c.read_species(d)) == 3                                    # zweites Anwenden aendert nichts


def test_karte_als_titelbild_wird_durch_wikidata_bild_ersetzt():
    assert c.looks_like_map("Monodelphis_kunsi_area.png") and c.looks_like_map("250px-Distribution_of_Foo.png")
    assert not c.looks_like_map("Vulpes_vulpes.jpg") and not c.looks_like_map("Project_Area_-_2012.jpg")
    assert c.needs_image("https://upload.wikimedia.org/x/250px-Banka_Shrew_area.png")
    pages = {"query": {"pages": [
        {"title": "Kunsi", "pageprops": {"wikibase_item": "Q1"}, "pageimage": "Monodelphis_kunsi_area.png",
         "thumbnail": {"source": "https://upload/thumb/Monodelphis_kunsi_area.png"}, "extract": "Ein Opossum."},
        {"title": "Fuchs", "pageprops": {"wikibase_item": "Q2"}, "pageimage": "Fuchs.jpg", "thumbnail": {"source": "https://upload/thumb/Fuchs.jpg"}}]}}
    def lic(p):
        return {"query": {"pages": [{"title": "File:" + t[5:].replace("_", " "), "imageinfo": [{"thumburl": "https://upload/250px-" + t[5:],
                "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}}}]} for t in p["titles"].split("|")]}}
    http = Fake([(lambda u, p: u == c.WIKIPEDIA_API, pages), (lambda u, p: u == c.COMMONS_API, lic)])
    sp = [{"qid": "Q1", "wikiTitle": "Kunsi", "imageFile": "Kunsi_photo.jpg", "img": "https://alt/karte.png"},
          {"qid": "Q2", "wikiTitle": "Fuchs", "imageFile": None},
          {"qid": "Q3", "imageFile": "Q3_range_map.png"}]
    c.enrich(http, sp, log=lambda *a: None)
    assert sp[0]["img"] == "https://upload/250px-Kunsi_photo.jpg" and sp[0]["imgSource"] == "wikidata" and sp[0]["summary"] == "Ein Opossum."
    assert sp[1]["img"] == "https://upload/thumb/Fuchs.jpg" and sp[1]["imgSource"] == "wikipedia" and sp[1]["imgLicense"] == "CC BY 4.0"
    assert "img" not in sp[2]                                              # Karte aus Wikidata wird auch nicht genommen


def test_handgepflegte_ausgestorbene_bleiben():
    out = [{"sciName": "Bos primigenius", "common": "Aurochs", "year": 1700, "img": "https://x/a.jpg", "summary": "Text"},
           {"sciName": "Hydrodamalis gigas", "common": "Steller's sea cow", "year": 1768}]
    seed = [{"sciName": "Bos primigenius", "common": "Auerochse", "year": 1627, "img": "", "source": "seed"},
            {"sciName": "Equus ferus ferus", "common": "Tarpan", "year": 1909, "source": "seed"},
            {"sciName": "Foo bar", "year": 1, "source": "wikidata-iucn"}]
    assert c.keep_curated(out, seed) == 2
    by = {r["sciName"]: r for r in out}
    assert by["Bos primigenius"]["common"] == "Auerochse" and by["Bos primigenius"]["year"] == 1627
    assert by["Bos primigenius"]["img"] == "https://x/a.jpg" and by["Bos primigenius"]["summary"] == "Text"
    assert "Equus ferus ferus" in by and "Foo bar" not in by and [r["year"] for r in out] == [1627, 1768, 1909]


def test_vorgeschichtlich_ausgestorbene_fallen_raus():
    Q = "http://www.wikidata.org/entity/"
    http = Fake([(sparql_has("wdt:P183"), sparql_result([])),
                 (sparql_has("wdt:P8556"), sparql_result([B(item=Q + "Q50", y="1350")])),
                 (lambda u, p: u.endswith("/species/match"), {"matchType": "NONE"})])
    taxa = [{"qid": "Q50", "sciName": "Megaladapis edwardsi", "common": "M", "status": "", "extinct": True, "source": "mdd"},
            {"qid": None, "sciName": "Pipistrellus murrayi", "common": "Christmas Island pipistrelle", "status": "EX", "source": "mdd",
             "typeLat": -10.5, "typeLng": 105.6, "countries": "Australia"}]
    out = c.build_extinct(http, taxa, None, log=lambda *a: None)
    assert [r["sciName"] for r in out] == ["Pipistrellus murrayi"]
    assert (out[0]["lat"], out[0]["lng"], out[0]["region"]) == (-10.5, 105.6, "Australia")
