# Keelson self-hosted base map — notices

Written by `scripts/basemap/fetch-basemap.mjs` next to the data it describes (the `basemap`
volume, served at `/basemap/`). See `docs/contracts/127-m8-basemap.md`.

## Map data: Protomaps basemap tiles (`v4/*.pmtiles`)

- Data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), available under the
  [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). The archive is
  an extract of a [Protomaps](https://protomaps.com) daily build (the build and its URL are recorded
  in `manifest.json`); see <https://docs.protomaps.com/basemaps/downloads> for the full list of
  sources (it also includes Natural Earth, public domain).
- The map shows the credit "Protomaps © OpenStreetMap" in its always-expanded attribution control
  whenever these tiles are drawn.

## Glyphs: Noto Sans (`fonts/`)

Noto Sans Regular, Medium, Italic and Devanagari (signed-distance-field glyph ranges from
[protomaps/basemaps-assets](https://github.com/protomaps/basemaps-assets) at the commit recorded in
`manifest.json`), © The Noto Project Authors, licensed under the SIL Open Font License 1.1:
see `OFL.txt`.

## Sprites (`sprites/`)

Sprite sheets from protomaps/basemaps-assets, derived from the MIT-licensed
[tangrams/icons](https://github.com/tangrams/icons): see `sprites-MIT.md`.

## Offline outline (not in this volume)

The web app also bundles a simplified world outline drawn when no detailed base map is
reachable: [Natural Earth](https://www.naturalearthdata.com/) 1:110m land, public domain.
