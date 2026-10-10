"""(M8) The case catalogue the in-app runner reports to the engine (``EvalSuiteInfo`` of
``keelson_contracts.evals``) and the suite hash written into every results file.

The hash is ``sha256`` of the canonical JSON of every case model (sorted by id): any change to a
case file — a message, an expectation, a soft entry — changes it, so runs of different suites are
never mistaken for each other in trends and comparisons.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from .cases import Case

__all__ = ["suite_hash", "suite_info"]


def suite_hash(cases: Sequence[Case]) -> str:
    docs = [c.model_dump(mode="json") for c in sorted(cases, key=lambda c: c.id)]
    text = json.dumps(docs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def suite_info(cases: Sequence[Case]) -> dict[str, Any]:
    """``{suite_hash, cases: [{id, title, tags, turns, turn_kinds, needs_live_embeddings, injects}]}``.

    ``needs_live_embeddings``: the case uploads knowledge-base documents, whose embeddings are not
    part of LLM record/replay (a replay-mode run skips it). ``injects``: the case writes demo data
    into Merxa (an inject or a signal turn)."""
    return {
        "suite_hash": suite_hash(cases),
        "cases": [
            {
                "id": c.id,
                "title": c.title[:200],
                "tags": [t[:40] for t in c.tags][:20],
                "turns": len(c.turns),
                "turn_kinds": [t.kind for t in c.turns],
                "needs_live_embeddings": c.setup.kb_upload is not None,
                "injects": bool(c.setup.inject) or any(t.signal is not None for t in c.turns),
            }
            for c in sorted(cases, key=lambda c: c.id)
        ],
    }
