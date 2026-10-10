# port-strike-syndication: Hamburg dock strike, one story syndicated (simulated)

**This pack is simulated demo data.** The following are all fictional:

- the dock strike in the Port of Hamburg and the Hamburg-Waltershof walkout;
- the three outlets, *Demo Freight Daily* (RSS 2.0), *Simulated Shipping Wire* (Atom 1.0) and *Demo Port
  Bulletin* (RSS 2.0);
- every URL and every Atom `tag:` id, which all sit on reserved `*.example` hosts.

Everything the Signal Hub produces from this pack has `origin: demo` and `provenance: simulated`. Its alerts have `simulated: true` and show a DEMO badge.

The pack shows clustering v2 (`docs/contracts/123-m8-signal-clustering.md`): the alert drawer's
"Why these reports are grouped" section lists one report per grouping reason.

## Timeline

The sim clock starts at 2026-10-06T08:00Z.

| step | t+ | adapter | what happens |
|---|---|---|---|
| 0 | 0 h | rss | Demo Freight Daily reports that a dockworkers' strike halts container handling at the Port of Hamburg. The pre-baked extraction is `labor/strike` with the port "Port of Hamburg" → `UNLOCODE:DEHAM` (+ `ISO3166:DE`). It founds the event: **founded**. **Alert** (medium, 5500 bp). |
| 1 | 2 h | rss | Simulated Shipping Wire (Atom) runs a syndicated copy of the same story: one headline word changed, same snippet. SimHash finds it within a few bits of the original from another source: **near_duplicate**. It joins the original's event and adds **no** corroboration (the event stays a 1-source `candidate`). |
| 2 | 6 h | rss | Demo Port Bulletin reports that workers at the "Hamburg-Waltershof terminal" join the strike. The gazetteer does not know that name, so the item has no location ref and no geometry, and the v1 rules cannot place it. Its cluster terms (`labor`, `strike`, places `hamburg`, `waltershof`) are similar to the event's (similarity 7977 bp ≥ the strict threshold 7500 for a report without any location): **attach_vector**. The event now has two independent sources and is `confirmed`. |
| 3 | 6 h | chat_hint | Presenter hint. |

## Expected exposure

`expected.yaml` holds the expected exposure, `memberships` and independent `sources` per checkpoint, and
`../validate.py` checks them (it replays SimHash and the hub's own `cluster.decide`).

This is a news-only event, so its radius is the `strike` radius of 25 km around the Hamburg point:

- **intersects**: port **DEHAM** (the reports are geocoded to the port itself; its `UNLOCODE:DEHAM` ref would also match);
- **lane_crosses**: the lanes that start or end at Hamburg: **SGN-HAM-SEA**, **SGN-HAM-CAPE**, **HAM-DE1-RD**,
  **HAM-PL1-RD**.

The event is medium severity (strike prior 5500 bp), so it raises an alert without drafting a case.

## Notes

- **No LLM is needed.** Every item has a pre-baked extraction keyed by guid or Atom id; pre-baked extractions
  always win over reusing an original's extraction. Each mention is copied verbatim from the headline or snippet.
- **Determinism.** SimHash and the local term vectors are integer functions; the replay also runs the cluster
  merge pass after every step (nothing merges here: there is only one event).
- **To check the pack**, run `python data/scenarios/validate.py port-strike-syndication`.
