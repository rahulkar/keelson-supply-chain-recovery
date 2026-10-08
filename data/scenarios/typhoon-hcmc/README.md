# typhoon-hcmc: Typhoon Kestrel approaching Ho Chi Minh City (simulated)

**Simulated demo data.** Typhoon KESTREL-26, its GDACS event id 9260041 and the outlet *Demo Freight Daily* are fictional. Every URL is on a `*.example` host, which RFC 2606 reserves, so none of them resolves. Nothing here is a real GDACS product or a real news report.

The Signal Hub labels everything it produces from this pack `origin: demo` and `provenance: simulated`. Its alerts are `simulated: true` and show the DEMO badge.

This pack drives Act 1 of the demo (plan §18):

- a replayed GDACS typhoon approaches HCMC;
- an alert appears with its exposure;
- a case is drafted automatically when the advisory turns Red.

## Timeline

The sim clock starts at 2026-10-05T00:00Z, which is Merxa week 41.

| step | t+ | adapter | what happens |
|---|---|---|---|
| 0 | 0 h | gdacs | **Advisory 1: Orange.** 130 km/h, about 570 km east of HCMC. The forecast cone already covers Cat Lai. An **alert** is raised (medium, 6055 bp). |
| 1 | 6 h | gdacs | **Advisory 2: Orange.** 150 km/h. The cone shifts south onto HCMC. The alert is edited (+100 bp, no re-notification). |
| 2 | 12 h | gdacs | **Advisory 3: Red.** 175 km/h, landfall near Vung Tau within 12 h. Severity jumps to 8780 bp (+2625), which re-notifies. An exposed port, factory and supplier match the default `alert_and_draft_case` rule, so the engine opens the case. The case still waits at the approach card. |
| 3 | 14 h | rss | **News item.** Demo Freight Daily reports "Cat Lai terminal suspends operations ahead of Typhoon Kestrel". It carries a **pre-baked extraction**: `logistics/port_closure`, with the port "Cat Lai terminal" resolved to `UNLOCODE:VNSGN`. It attaches to the typhoon event as corroborating news. |
| 4 | 18 h | gdacs | **Advisory 4: Red.** 185 km/h, about 145 km east of HCMC, just off the Binh Thuan coast. The cone is centred on HCMC. Severity rises by 50 bp, so the alert is edited only. |
| 5 | 18 h | chat_hint | Presenter hint: "what if Cat Lai stays closed for 10 days?" |

### Advisory fixtures

The advisories are `gdacs-adv1..4.geojson`. Each file is a GDACS API v2 `geteventlist` FeatureCollection with one `Point_Centroid` event feature. The event feature carries:

- `alertlevel` and `alertscore` (the integer level score, as GDACS sends it: Orange 2, Red 3);
- `episodeid` 12 to 15;
- `severitydata` and `affectedcountries`;
- naive-UTC `fromdate`, `todate` and `datemodified`.

Each file also embeds the `getgeometry` footprint of the same episode:

- the uncertainty cone (`Poly_Cones`), a counter-clockwise polygon of about 28 vertices;
- the forecast track (`Line_Line_0`, labelled with the storm category `TY`);
- the track points (`Point_Polygon_Point_N`). As in the real `getgeometry` response, they are small circles (`Polygon`) labelled like `05/10 06:00 UTC`. The adapter merges them into the footprint, and they lie inside the cone, so they change no exposure.

The files are **generated** by `../tools/gen_typhoon_hcmc.py`, so do not hand-edit them. The generator builds the cone as the convex hull of uncertainty circles, 30 km to 125 km in radius, around a forecast that converges onto HCMC as the storm approaches. Run it with `--check` to confirm the committed files match its output.

### News fixture

The RSS item is RSS 2.0, shaped like a WordPress trade-press feed. The hub keeps only the headline, a snippet of at most 300 characters, and the link.

## Expected exposure

`expected.yaml` holds the full expectation, and `../validate.py` checks it. It assumes a `value-fashion-eu` tenant on the Merxa `full` seed.

Merxa exports factories and suppliers without coordinates. The engine therefore places them at their country's export port: VNSGN for Vietnam and KHPNH for Cambodia.

- **intersects**: inside the cone at every advisory.
  - port **VNSGN**
  - factory **F-VN-01**
  - supplier **SUP-VN-01**
- **lane_crosses**: every lane that touches Cat Lai.
  - **SGN-HAM-SEA, SGN-GDN-SEA, SGN-HAM-CAPE, SGN-GDN-CAPE, SGN-DE1-SA, SGN-PL1-SA**
  - PNH-SGN-RIV
- **dwithin_km**: outside the cone but within the 300 km TC radius.
  - port KHPNH
  - factory F-KH-01
  - suppliers CMT-KH-01 and TRIM-KH-01

  Phnom Penh is about 210 km from HCMC, so any typhoon footprint over HCMC reaches it. This is realistic, so it stays in the expectation.
- **Not exposed**: Bangladesh, Europe and every other region.

The alert summary therefore reads about **7 sites · 7 lanes**. Open PO counts come from the ERP.

## Notes

- **No LLM is needed.** The single news item carries a pre-baked extraction and only passes through the deterministic injection-pattern screen. Nothing in this pack waits as `pending_extraction` when the LLM is unavailable.
- **Replays are deterministic.** Ids are `uuid5` of connector, native id and version, and every timestamp comes from the sim clock. A reset followed by a start reproduces the same signals, events and alerts.
- **To check the pack**, run `python data/scenarios/validate.py typhoon-hcmc` and `python data/scenarios/tools/gen_typhoon_hcmc.py --check`.
