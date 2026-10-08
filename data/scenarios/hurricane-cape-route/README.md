# hurricane-cape-route: Hurricane Albatross over the Cape route off West Africa (simulated)

**Simulated demo data.** Hurricane ALBATROSS and its id `al992026` are fictional: NHC numbers 90–99 are invest designations and never belong to a named storm, so the id cannot collide with a real advisory. Every URL is on a `*.example` host, which RFC 2606 reserves, so none of them resolves. Nothing here is a real NHC product.

The Signal Hub labels everything it produces from this pack `origin: demo` and `provenance: simulated`. Its alerts are `simulated: true` and show the DEMO badge.

This pack shows the M8 NHC feed (`docs/contracts/124-m8-signal-feeds.md`): a hurricane far from every site still matters when it sits on the shipping lanes.

- A replayed NHC hurricane forms off Guinea and tracks north-west along the West African coast.
- Its forecast cone lies across the Cape-of-Good-Hope route, the alternative to Suez.
- The alert lists the exposed lanes only. No site is near, so no case is drafted automatically.

## Timeline

The sim clock starts at 2026-10-12T00:00Z, which is Merxa week 42.

| step | t+ | adapter | what happens |
|---|---|---|---|
| 0 | 0 h | nhc | **Advisory 7: category 1.** 70 kt, about 80 km off Conakry, moving north-west. The cone and the track cross the Cape route. An **alert** is raised (medium, 6500 bp). |
| 1 | 6 h | nhc | **Advisory 8: category 3.** Rapid intensification to 100 kt. Severity rises to 8500 bp (+2000), which re-notifies as critical. The action stays `alert`: the default draft-case rule needs an exposed supplier, factory or port. |
| 2 | 12 h | nhc | **Advisory 9: category 3.** 110 kt, moving north-west towards Guinea-Bissau's offshore islands. The severity band is unchanged (8500 bp), so the alert is edited only. |
| 3 | 12 h | chat_hint | Presenter hint: "what if the Cape-routed sailings lose four days to the hurricane?" |

### Advisory fixtures

Each advisory is three files, all **generated** by `../tools/gen_hurricane_cape_route.py` (do not hand-edit; run it with `--check` to confirm the committed files match its output):

- `nhc-adv<n>.json`: a `CurrentStorms.json`-shaped document with one storm. As in the live feed (recorded 2026-10-08), `intensity` and `pressure` are digit strings, the position comes as numbers and as text (`9.3N`), and each product is an object `{advNum, issuance, fileUpdateTime, url | kmzFile, zipFile}`.
- `nhc-adv<n>-cone.kml`: the 5-day forecast cone in the KML 2.1 namespace, like NHC's cone product. The generator builds it as the convex hull of uncertainty circles (25 km at the advisory time, growing to 195 km at 72 h) around the forecast positions.
- `nhc-adv<n>-track.kml`: the forecast track in the KML 2.2 namespace, a line plus forecast points.

The pack payload names the two KML files under `gis`, so the replay reads them directly. The `kmzFile` links in the JSON point to `*.example` and are never fetched. The adapter merges the cone and the track line with the current position into the storm's footprint. It drops the track's forecast points, which add nothing to the line.

NHC dates a storm from its advisory count: `starts_at` is the issuance time minus 6 h per earlier advisory, so all three versions share one start (2026-10-10T12:00Z). Each advisory expires 12 h after it was issued.

## Expected exposure

`expected.yaml` holds the full expectation, and `../validate.py` checks it. It assumes a `value-fashion-eu` tenant on the Merxa `full` seed.

- **lane_crosses**: the four lanes routed via the Cape of Good Hope. Their shared West African leg (`data/geo/sea_routes.json`) runs about 17–19° W between 12° N and 24° N.
  - **CGP-FXT-SEA, CGP-RTM-SEA** (Chattogram → Felixstowe / Rotterdam)
  - **SGN-HAM-CAPE, SGN-GDN-CAPE** (Ho Chi Minh City → Hamburg / Gdańsk)
- **Not exposed**: every supplier, factory, port and DC. The nearest node, Tanger Med (MATNG), is more than 2 000 km away, far beyond the 300 km tropical-cyclone radius. The Suez lanes, the sea-air lanes and the European road lanes are not exposed either.

The alert summary therefore reads **4 lanes** and no sites. The alert severity is medium at advisory 7 and critical from advisory 8.

## Notes

- **No LLM is needed.** The NHC adapter is a deterministic parser (`provenance: fact` outside the demo).
- **Replays are deterministic.** Ids are `uuid5` of connector, native id and version, and every timestamp comes from the sim clock. A reset followed by a start reproduces the same signals, events and alerts.
- **There is no OFAC demo pack, on purpose.** A simulated sanctions designation that matches seed supplier names, which could belong to real companies, is not acceptable demo data.
- **To check the pack**, run `python data/scenarios/validate.py hurricane-cape-route` and `python data/scenarios/tools/gen_hurricane_cape_route.py --check`.
