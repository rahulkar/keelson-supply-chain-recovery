# Demo scenario packs (Signal Hub replay)

These packs are deterministic and **simulated** timelines of real-shaped feed payloads (`docs/contracts/100-m6-signals.md` §7). The Signal Hub replays them through its **real adapters**:

- start, advance and reset with `POST /v1/demo/packs/{pack}/start|advance|reset`;
- the presenter goes through the engine at `POST /api/demo/packs/...`;
- fixtures are served at `http://signal-hub-api:8300/demo-feeds/<pack>/<file>`.

Everything a pack produces is labelled:

- signals: `origin: demo`, `provenance: simulated`;
- alerts: `simulated: true`, shown with a DEMO badge.

No pack needs an LLM. Every news item and free-text item ships a pre-baked extraction.

| pack | story | expected exposure (reference network) |
|---|---|---|
| [`typhoon-hcmc`](typhoon-hcmc/README.md) | Act 1: four GDACS TC advisories (Orange → Red) for the fictional Typhoon KESTREL-26 tracking onto Ho Chi Minh City, plus one Demo Freight Daily item (Cat Lai suspends operations) | VNSGN, F-VN-01, SUP-VN-01 (intersects); SGN-* + PNH-SGN-RIV lanes; Cambodian nodes (dwithin) |
| [`a-outerwear-trim-delay`](a-outerwear-trim-delay/README.md) | Merxa `supplier_message.received` (trims delayed 7 days at TRIM-BD-01), plus a cold-weather demand advisory | TRIM-BD-01 (entity_match); the advisory is context only |
| [`suez-closure`](suez-closure/README.md) | Three news items from two fictional outlets (RSS 2.0 + Atom) about a Red Sea / Suez closure | SGN-HAM-SEA, SGN-GDN-SEA (lane_crosses) |
| [`hurricane-cape-route`](hurricane-cape-route/README.md) | M8: three NHC advisories (category 1 → 3) for the fictional Hurricane ALBATROSS tracking north-west along the West African coast, with cone and track KML | CGP-FXT-SEA, CGP-RTM-SEA, SGN-HAM-CAPE, SGN-GDN-CAPE (lane_crosses); no site, so no case is drafted |

## Layout

- `<pack>/pack.yaml`: the `ScenarioPack` contract (`id`, `title`, `description`, `start_at`, `steps[{t_offset_s, adapter, payload, note}]`).
- `<pack>/*.geojson|*.json|*.xml`: fixtures named by `payload.fixture`. They must be flat files in the pack directory.
- `<pack>/README.md`: the narrative and timeline, the labelling, and notes.
- `<pack>/expected.yaml`: the expected events, members, severities, alerts and exact exposures at each checkpoint.
- `network-value-fashion-eu.yaml`: the reference tenant network the expectations are computed against. It mirrors what the engine pushes for a `value-fashion-eu` tenant on the Merxa `full` seed. This is not a pack.
- `validate.py`: the offline validator (see below).
- `tools/gen_typhoon_hcmc.py`: generates the typhoon advisories. Use `--check` to detect drift.
- `tools/gen_hurricane_cape_route.py`: generates the NHC advisories, cones and tracks of `hurricane-cape-route` (`--check` too).
- `tests/test_packs.py`: regression tests for the packs and the validator.

## Step payloads per adapter (as `signal_hub.replay` reads them)

| adapter | payload |
|---|---|
| `gdacs` / `usgs` | `{fixture: <file>.geojson}`. This is a GDACS `geteventlist` FeatureCollection, which may embed the `getgeometry` footprints of the same `eventid`/`episodeid`, or a USGS summary feed. |
| `rss` | `{fixture: <file>.xml, connector_id: rss.<slug>, source_name, extractions: {<guid or Atom id or link or title>: NewsExtraction}}` (or a single `extraction` that applies to every item). |
| `merxa_inject` | `{fixture: <file>.json \| event: {type, id, timestamp, data}, extraction}`. The body is a Merxa Standard Webhooks payload, as the engine forwards it. |
| `chat_hint` | `{text}`. A presenter hint. It is only logged. |
| `nhc` (M8) | `{fixture: <file>.json, gis: {<storm id>: [<file>.kml \| <file>.kmz, …]}}`. A `CurrentStorms.json`-shaped document plus the cone / track products per storm. |
| `copernicus_ems` (M8) | `{fixture: <file>.json}`. A Copernicus EMS `public-activations-info` answer. |
| `meteoalarm` (M8) | `{fixture: <file>.atom, connector_id: meteoalarm.<country>, details: {<CAP identifier>: <file>.xml}}`. A MeteoAlarm legacy Atom feed plus the CAP documents with the area polygons. |
| `gdelt` (M8) | `{fixture: <file>.CSV \| <file>.zip}`. One GDELT 2.0 15-minute event export. |

There is no `ofac` step on purpose: a simulated sanctions designation that matches seed supplier names, which could belong to real companies, is not acceptable demo data. The M8 steps are parsed by `signal_hub.replay_feeds` (the same code as the live pollers' parsers), always with the code defaults of the GDELT filter and the MeteoAlarm level, so a pack replays identically on every deployment.

## Authoring rules (enforced by `validate.py`)

- **Clearly fictional.**
  - `title` says "(simulated)".
  - `description` starts with `SIMULATED`.
  - Fixtures carry a SIMULATED note.
  - Outlets and storms are fictional.
  - Every URL is on a reserved RFC 2606 `*.example` host. Simulated data never links a real GDACS report or a real article.
- **Real feed shapes.**
  - GDACS (checked against the live API v2, 2026-10-08):
    - capitalised alert levels and naive-UTC dates;
    - `alertscore` is the integer level score (Green 1, Orange 2, Red 3), and `episodealertscore` is a number;
    - `Point_Centroid` events, with footprints keyed by event and episode;
    - `getgeometry` track points are small `Polygon` circles (`Point_Polygon_Point_<n>`), not Points.
  - RSS 2.0 or Atom:
    - snippets of at most 300 characters and headlines of at most 300 after HTML stripping (the hub keeps no more);
    - at most 40 items per feed (the hub's RSS adapter reads only the first 40);
    - RFC 822 weekdays that match the date.
  - Merxa webhook bodies within Merxa's limits:
    - a subject of at most 300 characters (the hub's title);
    - uuidv7 ids whose embedded time matches `timestamp` / `received_at`.
  - GeoJSON is 2D WGS84 with closed, simple, counter-clockwise exterior rings.
  - NHC (M8, checked against the live feed 2026-10-08): `{activeStorms: [...]}` with storm ids like `al092026`, digit-string `intensity` / `pressure`, numeric positions, `publicAdvisory.advNum` like `007`, ISO issuance times; cone / track files are KML (`<kml>` root, any KML namespace).
  - Copernicus EMS, MeteoAlarm and GDELT fixtures (M8) carry a SIMULATED note and only `*.example` links; their shapes are checked by parsing them with the real adapters at the replay level.
- **No time travel.** No item, channel or feed is published, built, modified or received after its step's sim time.
- **Repeated news items are identical.** The hub stores a news item once per connector and guid (`ON CONFLICT DO NOTHING`) and never re-reads it. A cumulative feed may repeat an earlier item, but only with the same text and the same pre-baked extraction. Publish an update under a new guid.
- **Pre-baked extractions.**
  - Every item has one.
  - It follows the strict `NewsExtraction` schema.
  - `injection_suspected: false`.
  - Every mention is copied verbatim from the headline or snippet.
  - Each one passes the hub's real quarantine screen.
- **Exact expectations.** At each checkpoint, the replayed events, members (with the cluster v1 attach rules), severity, kind radius, alert action (`DEFAULT_ALERT_RULES`) and exposures must match `expected.yaml` exactly. Geometric decisions within 5 km of a threshold fail as fragile.

## Checking

```sh
python data/scenarios/validate.py                         # shape + contract levels (stdlib, PyYAML, keelson_contracts)
python data/scenarios/tools/gen_typhoon_hcmc.py --check   # typhoon fixtures match the generator
# all levels incl. replay through the real Signal Hub adapters (Docker, from the repo root):
MSYS_NO_PATHCONV=1 docker run --rm -v D:/primark:/work -v keelson_uvcache:/root/.cache/uv \
  -v <venv-volume>:/venv -e UV_PROJECT_ENVIRONMENT=/venv -w /work/services/signal-hub \
  ghcr.io/astral-sh/uv:python3.14-trixie-slim sh -c \
  "uv sync --frozen && uv run --no-sync python /work/data/scenarios/validate.py --require replay && \
   uv run --no-sync python -m pytest /work/data/scenarios/tests -p no:cacheprovider -c /dev/null --rootdir /work/data/scenarios"
```

The Signal Hub's own PostGIS suite also replays these packs end to end (`services/signal-hub/tests/test_db_real_packs.py`).

## Adding a pack

1. Create `data/scenarios/<id>/` with `pack.yaml`, the fixtures, `README.md` and `expected.yaml`.
2. Run `validate.py` and iterate until it reports 0 errors and 0 warnings.
3. If the Merxa seed network changes, update `network-value-fashion-eu.yaml`. The validator reports any expectation that drifts.
