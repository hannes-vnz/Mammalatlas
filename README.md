# Mammalatlas

Interactive world atlas of mammals: distribution ranges as map layers, filterable by order and IUCN status, with species photos from Wikimedia Commons. Runs entirely in the browser.

**[Live demo](https://hannes-vnz.github.io/Mammalatlas/)** · Single HTML file + two data files, no framework

<!-- ![Screenshot](screenshots/preview.png) -->

## Features

- 5,469 species with distribution polygons rendered with MapLibre GL JS
- Dark map theme, colour-coding for all IUCN categories (LC, NT, VU, EN, CR, EW, EX, DD)
- Photo markers from Wikimedia Commons, each species profile links to the image's licence page
- Markers follow the visible map area: the more you zoom in, the more species of that region appear (placed inside their range, decluttered by screen distance, photos and threatened species first)
- Search and filters by order, family and conservation status
- **Extinct species mode:** switch to "† Ausgestorben" for an atlas of extinct mammals with a timeline slider (with a time-lapse), cause filter and species profiles. The bundled `data/extinct_species.json` is a small hand-curated seed (12 species, unverified); the crawler replaces it with a much larger, sourced dataset.
- **[Insights page](https://hannes-vnz.github.io/Mammalatlas/insights.html):** an analysis dashboard on top of the same data
  - hotspot map with four views (species richness, threatened species, share threatened, endemism index), order filter, 2° / 4° grid and a 3D mode
  - region explorer: click a cell to see its species with status, photo and a link into the atlas
  - latitude profile, conservation-status donut, most threatened orders, range-size distribution, smallest and largest ranges, sortable family table
  - shareable state in the URL hash and CSV export of the current grid
- Responsive layout with a bottom sheet on mobile

## Data

Species data lives in `data/species_part_1.txt` and `data/species_part_2.txt` (tab-separated, split to stay below file size limits):

| Column | Meaning |
|---|---|
| `common`, `sciName` | Common and scientific name |
| `order`, `family` | Taxonomy |
| `status` | IUCN category (e.g. LC, EN, CR) |
| `img` | Image URL (Wikimedia Commons) |
| `w`, `l` | Weight and lifespan (`?` if unknown) |
| `range` | Distribution polygon as `lon\|lat` pairs separated by `;`, multiple polygons separated by `*` |

### Data notes

- 3,089 species have a real photo. For 2,238 species the image field only contains the IUCN status icon from the Wikipedia infobox, and 142 have no image at all. The app shows a placeholder for both.
- Lifespan is not available yet (empty for all species), weight is missing for 2,156 species. Unknown values are hidden in the species profile.
- 27 species have no distribution polygon and therefore do not appear on the map.

<!-- TODO: add sources and licences for range polygons, IUCN status and the enrichment pipeline before publishing -->

## Run locally

The app loads the data files with `fetch`, so it needs a local server (opening the file directly will not work):

```bash
python -m http.server 8000
# then open http://localhost:8000
```

## Project structure

```
index.html        # atlas (map, filters, species profiles, extinct species mode)
insights.html     # analysis page (hotspots, statistics)
data/             # species_part_1.txt, species_part_2.txt, extinct_species.json, provenance.json (after a crawl)
tools/crawler.py  # data crawler (Wikidata, Wikipedia, Commons, GBIF), standard library only
tests/            # offline tests for the crawler (pytest)
```

Both pages share the same small core (range parsing, point-in-polygon, marker selection, richness raster), kept in one block marked `ATLAS-CORE` at the top of each script.

## Keeping the data current

[`tools/crawler.py`](tools/README.md) pulls current data from open sources, fills gaps (missing photos, weights, lifespans), adds species that are not in the atlas yet and builds the extinct-species dataset. It never deletes rows, writes a preview by default and records where every changed value came from in `data/provenance.json`.

## Ideas / roadmap

- More analysis pages (e.g. body size vs. threat status, taxonomy explorer)
- Run the crawler on the full dataset, review the report and commit the refreshed data
- Authoritative range maps and status from the IUCN Red List (needs a free API token and respect for its terms)
- Replace icon-only image entries with real photos, fill in lifespan data

## Built with

[MapLibre GL JS](https://maplibre.org/), CARTO Dark Matter basemap, Wikimedia Commons images (see each image's page for its licence).
