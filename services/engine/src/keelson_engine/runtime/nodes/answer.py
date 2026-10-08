"""Answer path: ``answer_investigator`` (LLM + read tools + calc) → ``answer_structurer``
(LLM, ``Findings``) → ``compose_answer`` (deterministic text with resolved placeholders)."""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents.context import Context
from keelson_contracts.blocks import EvidencePackProps, NarrativeProps
from keelson_contracts.common import Provenance
from keelson_contracts.llm import Findings

from ..agents import fill_nullable
from ..compose import (
    UNVERIFIED_NOTE,
    ComposeContext,
    allowed_tokens_from,
    strip_unverified,
)
from ..context import current_run
from ..quarantine import display_results
from .common import ENGINE_COMPOSE, block_event, input_dict, say
from .planning import evidence_items

__all__ = ["compose_answer"]

_EV_RE = re.compile(r"\{\{\s*ev\s*:\s*(ev_[0-9A-Za-z_-]+)#")
_CALC_RE = re.compile(r"\{\{\s*calc\s*:\s*(ev_[0-9A-Za-z_-]+)\s*\}\}")


async def compose_answer(ctx: Context, node_input: Any) -> AsyncGenerator[Any]:
    run = current_run()
    findings = Findings.model_validate(fill_nullable(Findings, input_dict(node_input)))
    texts = [c.text for c in findings.claims]
    ids = sorted(
        {m for t in texts for m in _EV_RE.findall(t)}
        | {m for t in texts for m in _CALC_RE.findall(t)}
        | {c.evidence_id for c in findings.claims if c.evidence_id}
        | {c.calc_id for c in findings.claims if c.calc_id}
    )
    rows = await run.deps.repo.get_evidence(tenant=run.tenant, ids=ids) if ids else []
    results = display_results(rows, case_id=run.case_id)
    rows = [r for r in rows if str(r["id"]) in results]
    calcs = {
        rid: int(res["value"])
        for rid, res in results.items()
        if isinstance(res, dict) and isinstance(res.get("value"), int) and not isinstance(res.get("value"), bool)
    }
    compose_ctx = ComposeContext(evidence=results, calcs=calcs)
    allowed = allowed_tokens_from(list(results.values()) + [c.model_dump() for c in findings.affected])
    sentences: list[str] = []
    dropped_total = 0
    for claim in findings.claims:
        text, dropped = strip_unverified(claim.text, compose_ctx, allowed)
        dropped_total += dropped
        if not text:
            continue
        cited = (claim.evidence_id in results) or (claim.calc_id in results)
        if claim.kind == "inference" or not cited:
            text = f"{text} _(inference)_"
        sentences.append(text)
    if findings.status == "failed" or not sentences:
        body = "I could not answer that from the connected systems."
        if findings.gaps:
            body += " " + " ".join(g for g in findings.gaps if not any(ch.isdigit() for ch in g))
    else:
        body = "\n".join(f"- {s}" for s in sentences) if len(sentences) > 1 else sentences[0]
    if dropped_total:
        body += "\n\n" + UNVERIFIED_NOTE
    yield await block_event(
        run, "narrative", NarrativeProps(markdown=body), title="Answer", prov=Provenance.INFERENCE,
        engine=ENGINE_COMPOSE, key=("narrative", run.run_id, "answer"),
    )
    cited_ids = [str(r["id"]) for r in rows]
    items = await evidence_items(run, cited_ids)
    if items:
        yield await block_event(
            run, "evidence_pack", EvidencePackProps(items=items), title="Evidence", prov=Provenance.FACT,
            engine=ENGINE_COMPOSE, key=("evidence", run.run_id, "answer"), placement="canvas",
        )
    yield await say(run, "answer", body)
    yield {"answered": bool(sentences), "unverified": dropped_total}
