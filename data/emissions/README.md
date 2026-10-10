# Transport emission factors

`factors.yaml` holds the transport emission factor sets the engine sends to the optimizer for the plan
CO₂e KPI (contract: `docs/contracts/126-m8-co2-robustness.md` §2). It is platform data: tenants pick a
set (`AgentSpec.planning.emissions.factors_id`) but cannot edit the factors.

## Set `glec-v3-indicative`

| Mode | g CO₂e / tkm (WTW) | Published GLEC default range used for the mid-point |
|---|---|---|
| sea (container, average) | 16 | 8–20 (generic maritime) |
| air (freighter, long haul) | 850 | 600–1,100 |
| road (articulated truck, average) | 100 | ≈ 83 tank-to-wheel + 17 well-to-tank (general road) |
| rail (average freight train) | 25 | 21–30 |

Distance rules (GLEC Framework / EN 16258 practice): road legs are the great-circle distance × 1.2
(`road_circuity_bp: 12000`), air legs the great-circle distance + 95 km (`air_uplift_km: 95`), sea legs
follow the hand-made polylines of `data/geo/sea_routes.json` (Cape / Suez variants, the Jebel Ali sea-air
hub).

### Source and status

- Source: Smart Freight Centre, *Global Logistics Emissions Council (GLEC) Framework for Logistics
  Emissions Accounting and Reporting*, version 3 (aligned with ISO 14083), default emission intensity
  values (well-to-wheel). <https://www.smartfreightcentre.org/en/our-programs/emissions-accounting/global-logistics-emissions-council/>
- Retrieved 2026-10-08 from secondary summaries of the framework; **the factors were not checked against
  the official tables**, so the set is `verified: false` and every screen labels the CO₂e figures
  *indicative*. A second attempt (2026-10-10) found the official default-value spreadsheet available
  only on request from Smart Freight Centre and the public excerpts image-only; the secondary sources
  consulted agree with the magnitudes above (generic sea 8–20, long-haul air ≈ 790, road ≈ 100, diesel
  rail ≈ 31 g CO₂e/tkm WTW). Note that GLEC v3.2 (October 2025) updated several defaults and renumbered
  tables, so pin the edition when verifying.
- To verify: copy the exact default intensities of the cited tables, set `verified: true` and record
  the edition and table numbers in `source.table` (the screens then drop the "indicative" label).

## Scope

Transport only (well-to-wheel), per unit moved: `unit weight (g) × Σ legs (km × factor)`. Production,
packaging, warehousing and the last mile are out of scope. The distances come from illustrative sea
polylines and the unit weights are tenant assumptions (`planning.emissions.unit_weights`, default
`planning.grams_per_unit`), so the figures support comparing plan options, not an emissions inventory.
