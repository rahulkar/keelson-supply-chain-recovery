"""M7 live check of the Signal Hub's provisioned extraction key (docs/contracts/110 §5; hub README).

The one-shot ``signal-hub-bootstrap`` provisions a LiteLLM key (alias ``keelson-signal-hub``, models
``extract-fast`` / ``guard-default`` only) and stores it encrypted; the hub API reads it at start-up.
This script (live model call: the extraction) checks that ``GET /v1/sources`` reports
``llm_configured`` with ``llm_key_source: db`` (or ``env`` when ``LITELLM_SIGNALS_KEY`` overrides it)
and that a benign supplier message forwarded by Merxa's webhook is **extracted** (no longer
``pending_extraction``), while an injection attempt is still flagged before any model.

Runs inside the engine image (``scripts/verify-live.sh`` step ``m7-hub-key``). Exit 0 pass, 1 fail,
2 environment missing.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_m6_offline as m6  # noqa: E402
import api_thin_slice as ts  # noqa: E402
from api_thin_slice import MERXA, E2EFailure, expect, heading, say  # noqa: E402

INJECTION = m6.INJECTION
EXTRACT_TIMEOUT_S = 240.0


def main() -> int:
    m6.core_init()
    try:
        heading("Signal Hub extraction key (live)")
        status = m6.hub("GET", "/v1/sources")
        say(f"llm_configured={status.get('llm_configured')} llm_key_source={status.get('llm_key_source')} "
            f"pending_extraction={status.get('pending_extraction')}")
        expect(status.get("llm_configured") is True, "the hub API has no LLM key")
        expect(status.get("llm_key_source") in ("db", "env"), f"llm_key_source {status.get('llm_key_source')}")
        before = dict(status.get("tenant_news") or {})
        admin = ts.merxa_login("merxa.admin")

        def inject(body: str) -> None:
            r = admin.post(f"{MERXA}/v1/admin/scenario:inject", headers={"Origin": MERXA, "Idempotency-Key": str(uuid.uuid4())},
                           json={"kind": "supplier_message", "payload": {
                               "supplier_code": "TRIM-BD-01", "subject": "Trims update", "body": body}})
            expect(r.status_code == 200, f"supplier_message inject: HTTP {r.status_code} {r.text[:200]}")

        inject("Zippers for PO 4711 ship 9 days late; we will confirm the new date.")
        inject(f"Update on the trims. {INJECTION}")

        def grew() -> dict[str, Any] | None:
            now = dict(m6.hub("GET", "/v1/sources").get("tenant_news") or {})
            delta = {k: int(now.get(k, 0)) - int(before.get(k, 0)) for k in ("extracted", "flagged", "pending_extraction")}
            return delta if delta["extracted"] >= 1 and delta["flagged"] >= 1 and delta["pending_extraction"] <= 0 else None

        delta = m6.wait_until("the benign supplier text to be extracted", grew, timeout_s=EXTRACT_TIMEOUT_S, every_s=5.0)
        say(f"tenant news delta {delta}: the benign text was extracted with the provisioned key, the injection flagged")
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
