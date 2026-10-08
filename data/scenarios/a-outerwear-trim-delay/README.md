# a-outerwear-trim-delay: outerwear trims delayed 7 days (simulated)

**This pack is simulated demo data.** All of its sources are fictional:

- Meghna Trims Ltd (`TRIM-BD-01`) is a fictional supplier from the Merxa mock-ERP seed.
- *Demo Weather Desk* is a fictional outlet, and its cold-snap forecast is invented.
- Every URL is on a reserved `*.example` host.

Everything the Signal Hub produces from this pack has `origin: demo` and `provenance: simulated`. Its alerts have `simulated: true` and show a DEMO badge.

The pack is the signal side of scenario A, which is Act 3 of the demo ("supplier email to remediation"). A Merxa supplier-portal message reports that trims are delayed 7 days. A separate retail weather advisory adds demand context.

## Timeline

The sim clock starts at 2026-10-05T08:00Z (Merxa week 41).

| step | t+ | adapter | what happens |
|---|---|---|---|
| 0 | 0 min | merxa_inject | Replays `merxa-supplier-message.json`, a Merxa Standard Webhooks body of type `supplier_message.received`. This is what the engine forwards to `POST /v1/ingest/connector-event`. The subject and body are the Merxa `trim_delay` inject text (`TRIM_DELAY_MESSAGE`). The **pre-baked extraction** is `supplier/supplier_delay`. Raises a medium **alert** (4500 bp). |
| 1 | 30 min | rss | Demo Weather Desk forecasts an early cold snap in the UK, Ireland and Benelux in weeks 46–47, which lifts demand for padded outerwear. The **pre-baked extraction** is `demand/demand_spike`. See [The cold-snap signal](#the-cold-snap-signal) for why this raises no alert. |
| 2 | 30 min | chat_hint | Presenter hint. |

### The cold-snap signal

The step 1 advisory is a context signal only.

- **No footprint:** countries and regions are kept as refs only, so the signal has no footprint and no exposure.
- **No alert:** its 3500 bp prior is below the 4000 bp alert threshold.
- **Still visible to the investigation:** the `external_intel` specialist can find it with `search_signals`.

## Expected exposure

`expected.yaml` lists the expected exposure, and `../validate.py` checks it.

- **Trim-delay event:** supplier **TRIM-BD-01**, by `entity_match`.
  - The `SUPPLIER:TRIM-BD-01` ref comes from the webhook's trusted, structured `supplier_code`, never from the free text.
  - The text also names F-BD-01, F-BD-02, Tongi, Gazipur and Narayanganj. None of these are gazetteer entries, so they stay unresolved mentions and never become codes.
- **Cold-snap event:** no exposure.

## Notes

- **Untrusted text:** the supplier message body and the weather item are untrusted. They reach only the quarantine, which here is the deterministic injection-pattern screen plus the pre-baked extraction, with no LLM. They can only ever become `inference` / `simulated` signals.
- **Scope:** this pack feeds the **Signal Hub** only and does not write the message into Merxa. To run the full Act 3 flow (Merxa supplier portal → planner case), use the Merxa admin inject `trim_delay`. Its webhook reaches the hub the same way as this fixture.
- **Checking the pack:** run `python data/scenarios/validate.py a-outerwear-trim-delay`.
