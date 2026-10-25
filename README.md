# Mammalatlas

Interactive world atlas of mammals: distribution ranges as map layers, filterable by order and IUCN status, with species photos from Wikimedia Commons. Runs entirely in the browser.

**[Live demo](https://hannes-vnz.github.io/Mammalatlas/)** · Single HTML file + two data files, no framework

<!-- ![Screenshot](screenshots/preview.png) -->

## Features

- 5,400+ species with distribution polygons rendered with MapLibre GL JS
- Dark map theme, IUCN status colour-coding
- Photo markers from Wikimedia Commons
- Filters by order and conservation status

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

<!-- TODO: add sources and licences for range polygons, IUCN status and the enrichment pipeline before publishing -->

## Run locally

The app loads the data files with `fetch`, so it needs a local server (opening the file directly will not work):

```bash
python -m http.server 8000
# then open http://localhost:8000
```

## Built with

[MapLibre GL JS](https://maplibre.org/), CARTO Dark Matter basemap, Wikimedia Commons images (see each image's page for its licence).
