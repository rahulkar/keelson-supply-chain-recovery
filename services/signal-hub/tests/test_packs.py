"""Every demo pack (``data/scenarios/<id>/pack.yaml``, plus the test pack) loads as a
:class:`ScenarioPack` and each step's payload is usable by the real adapters (offline: fixtures are
parsed directly, pre-baked extractions validated). Skips packs that do not exist yet."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import REPO, SCENARIOS, T0
from keelson_contracts.llm import NewsExtraction, news_extraction_violations
from keelson_contracts.signals import signal_id
from signal_hub.adapters.connector import ConnectorEvent, parse_connector_event
from signal_hub.adapters.gdacs import parse_gdacs
from signal_hub.adapters.rss import parse_feed
from signal_hub.adapters.usgs import parse_usgs
from signal_hub.gazetteer import Gazetteer
from signal_hub.models import SourceContext
from signal_hub.replay import fixture_path, list_packs, load_pack

PRODUCT = Path(os.environ.get("SCENARIOS_DIR") or REPO / "data" / "scenarios")


def _pack_dirs() -> list[tuple[Path, str]]:
    out = [(SCENARIOS, "typhoon-test")]
    if PRODUCT.is_dir():
        out += [(PRODUCT, p.name) for p in sorted(PRODUCT.iterdir()) if (p / "pack.yaml").is_file()]
    return out


@pytest.mark.parametrize(("root", "pack_id"), _pack_dirs(), ids=lambda v: v if isinstance(v, str) else "")
def test_pack_is_replayable(root: Path, pack_id: str, gaz: Gazetteer) -> None:
    pack = load_pack(root, pack_id)
    ctx = SourceContext(retrieved_at=T0, tenant="demo-retail", pack_id=pack.id)
    produced = 0
    for step in pack.steps:
        payload = step.payload
        fixture = payload.get("fixture")
        body = fixture_path(root, pack.id, fixture).read_bytes() if isinstance(fixture, str) else None
        if step.adapter in ("gdacs", "usgs"):
            assert body is not None, f"{pack.id}: {step.adapter} step needs a fixture"
            parse = parse_gdacs if step.adapter == "gdacs" else parse_usgs
            drafts = parse(json.loads(body), ctx, gaz)
            assert drafts, f"{pack.id}: {fixture} produced no signal"
            for d in drafts:
                sig = d.to_signal(signal_id=signal_id(d.connector_id, d.native_key, 1), version=1, supersedes=None,
                                  confidence_bp=9000, event_id=None)
                assert sig.simulated and sig.origin == "demo"
            produced += len(drafts)
        elif step.adapter == "rss":
            assert body is not None
            items = parse_feed(body, ctx, connector_id=str(payload.get("connector_id") or "rss.demo"),
                               source_name=str(payload.get("source_name") or "Demo Freight Daily"))
            assert items, f"{pack.id}: {fixture} has no items"
            produced += len(items)
        elif step.adapter == "merxa_inject":
            raw = payload.get("event") if payload.get("event") is not None else json.loads(body or b"null")
            drafts, texts = parse_connector_event(ConnectorEvent.model_validate(raw), ctx)
            assert drafts or texts, f"{pack.id}: merxa_inject produced nothing"
            produced += len(drafts) + len(texts)
        elif step.adapter == "chat_hint":
            assert isinstance(payload.get("text"), str)
        prebaked = [payload["extraction"]] if isinstance(payload.get("extraction"), dict) else []
        prebaked += list((payload.get("extractions") or {}).values())
        for raw_extraction in prebaked:
            extraction = NewsExtraction.model_validate(raw_extraction)
            assert news_extraction_violations(extraction) == []
    assert produced > 0


def test_list_packs() -> None:
    packs = list_packs(SCENARIOS)
    assert [p["id"] for p in packs] == ["typhoon-test"] and packs[0]["steps"] == 5
    assert list_packs(Path("/nonexistent")) == []
