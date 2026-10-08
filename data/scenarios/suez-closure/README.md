# suez-closure: Suez Canal closed after a Red Sea incident (simulated)

**This pack is simulated demo data.** The following are all fictional:

- the closure and the security incident;
- the two outlets, *Demo Freight Daily* (RSS 2.0) and *Simulated Shipping Wire* (Atom 1.0);
- every URL and every Atom `tag:` id, which all sit on reserved `*.example` hosts.

Everything the Signal Hub produces from this pack has `origin: demo` and `provenance: simulated`. Its alerts have `simulated: true` and show a DEMO badge.

This pack is the "actual" counterpart to the Act 2 chat what-if ("What if Suez closes for 3 weeks?"). Here the closure arrives as news rather than as a question.

## Timeline

The sim clock starts at 2026-10-05T06:00Z (Merxa week 41).

| step | t+ | adapter | what happens |
|---|---|---|---|
| 0 | 0 h | rss | Demo Freight Daily reports that Suez Canal transits are suspended after a Red Sea security incident. The pre-baked extraction is `geopolitical/chokepoint_closure`. It maps "Suez Canal" to `CHOKEPOINT:SUEZ` and "Red Sea" to `CHOKEPOINT:BAB_EL_MANDEB`, both gazetteer polygons. **Alert** (high, 7500 bp). |
| 1 | 6 h | rss | Simulated Shipping Wire (Atom) reports that carriers are pausing Suez routings, adding 10 to 14 days. The item attaches to the same event through the shared `CHOKEPOINT:SUEZ` ref, which corroborates it. |
| 2 | 24 h | rss | Demo Freight Daily reports that the closure could last up to three weeks. The feed is cumulative, so it repeats the earlier item, which is deduplicated by guid. |
| 3 | 24 h | chat_hint | Presenter hint. |

## Expected exposure

`expected.yaml` holds the expected exposure, and `../validate.py` checks it.

This is a news-only event, so its radius is the `chokepoint_closure` radius of 25 km. Only lanes are exposed:

- **lane_crosses**: **SGN-HAM-SEA** and **SGN-GDN-SEA**. These are the Vietnam → North Europe lanes via SUEZ in `data/geo/sea_routes.json`.
- **Not exposed**:
  - the Cape lanes: SGN-*-CAPE, and CGP-* via LKCMB/CAPE;
  - the sea-air lanes via Jebel Ali;
  - all sites.

  These are the alternatives the investigation can propose.

The event is high severity but exposes no supplier, factory or port. Under the default rules it therefore raises an alert without drafting a case. The planner opens the case from the inbox.

## Notes

- **No LLM is needed.** Every item has a pre-baked extraction keyed by guid or Atom id, including the repeated item. Each mention is copied verbatim from the headline or snippet.
- **To check the pack**, run `python data/scenarios/validate.py suez-closure`.
