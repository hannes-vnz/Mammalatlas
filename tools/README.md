# Data crawler

`crawler.py` keeps the atlas data current. It uses only the Python standard library (3.9+), caches every request on disk (so runs can be resumed) and waits between requests.

| Source | What it provides |
|---|---|
| Wikidata (SPARQL) | species list per order, IUCN status, mass, lifespan, family/order, image file, GBIF id |
| Wikipedia API | lead image (free licences only), intro text, category of extinct mammals |
| Wikimedia Commons | licence and author of every image |
| GBIF | occurrence points: a rough range from a convex hull and the last record year |
| AnAge, PanTHERIA, COMBINE | lifespan and body mass from local files you download yourself |

## Run

```bash
cd mammalatlas
CONTACT="you@example.com"                      # Wikimedia and GBIF expect a contact in the User-Agent

python tools/crawler.py --contact $CONTACT taxa                       # all mammal species with an IUCN entry
python tools/crawler.py --contact $CONTACT enrich                     # photos, licences, texts (only where missing)
python tools/crawler.py traits --anage anage_data.txt --pantheria PanTHERIA_1-0_WR05_Aug2008.txt   # optional
python tools/crawler.py --contact $CONTACT gbif --max-species 300     # rough ranges for species without a polygon
python tools/crawler.py --contact $CONTACT extinct                    # data/extinct_species.json
python tools/crawler.py merge                                         # preview of what would change
python tools/crawler.py merge --apply --update-status                 # write species_part_*.txt (backup in data/.backup)
```

First try it small: `taxa --orders Primates --limit-per-order 10` and look at `crawl/taxa.json`.

## What the merge does and does not do

- It **fills gaps only**: a photo is replaced when the current one is just the IUCN status icon (about 2,200 species), weight and lifespan are filled when unknown. Existing values are never overwritten.
- IUCN status from Wikidata is applied only with `--update-status`, and every change is listed in the report. Wikidata can lag behind the Red List, so check the list.
- **New species** are added only when they have a status. Species marked extinct go to the extinct dataset instead of the living atlas.
- Ranges built from GBIF points are a **rough approximation** (trimmed convex hull of up to 300 records, biased towards well-sampled regions). They are flagged as `gbif-hull` in `data/provenance.json` and the species profile says so. They are no substitute for expert range maps.
- Extinction years and causes in the extinct dataset are **heuristics** from the Wikipedia text and GBIF. Correct them in `data/extinct_overrides.json`, for example:

```json
{ "Thylacinus cynocephalus": { "year": 1936, "lat": -42.0, "lng": 146.5, "region": "Tasmania", "causes": ["Jagd"] } }
```

## Licences

Wikipedia text is CC BY-SA, images carry the licence stored per species, GBIF records are CC0 or CC BY per dataset. Keep the attribution when you publish the data. Check the licence of AnAge, PanTHERIA and COMBINE before redistributing values taken from them.

## Tests

`pytest tests/test_crawler.py` runs offline against mocked API answers (12 tests). The crawler has not been run against the live services in this repository's build environment, so do a small first run and check the output before a full crawl.
