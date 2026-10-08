"""The schema export is deterministic and the committed schemas are up to date."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = ROOT.parent / "contracts-ts" / "schemas"


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_schemas", ROOT / "scripts" / "export_schemas.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_schemas"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return load_script()


def test_deterministic_output(script: ModuleType, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    script.write(a)
    script.write(b)
    files_a = {p.name: p.read_bytes() for p in a.iterdir()}
    files_b = {p.name: p.read_bytes() for p in b.iterdir()}
    assert files_a == files_b
    assert script.check(a) == []


def test_expected_schema_set(script: ModuleType) -> None:
    names = set(script.render_all())
    expected = {
        "index.json",
        "block.envelope.schema.json",
        "block.provenance.schema.json",
        "agui.sc-metadata.schema.json",
        "agui.interrupt.schema.json",
        "run.request.schema.json",
        *(f"block.{k}.v1.props.schema.json" for k in (
            "kpi_strip", "approach_card", "recommended_option", "plan_diff",
            "approval_card", "commit_tracker", "narrative", "evidence_pack",
            "network_map", "impact_graph", "stockout_timeline", "scenario_compare",
            "remediation_checklist", "alert_origin",
        )),
        *(f"block.{k}.v1.data.schema.json" for k in ("network_map", "impact_graph", "stockout_timeline")),
        *(f"llm.{n}.schema.json" for n in (
            "intake-result", "disruption-extract", "entity-mention", "entity-ref",
            "claim", "findings", "option-reason", "recommendation",
            # M4 (90): planner, critic, follow-ups, quarantine, scenario updates
            "scenario-update", "plan-budget", "investigation-task", "investigation-plan",
            "critique-issue", "critique", "constraint-delta", "followup-classification",
            "guard-verdict", "untrusted-item", "untrusted-extraction",
            # M6 (100 §3): quarantined news extraction
            "news-extraction",
        )),
        # M5 (95): approval policy admin + quorum, demo reset
        *(f"{n}.schema.json" for n in (
            "policy.rule", "policy.set", "policy.context", "policy.decision", "policy.quorum",
            "demo.reset-request", "demo.reset-result",
        )),
        # M6 (100): Signal Hub
        *(f"signals.{n}.schema.json" for n in (
            "signal", "event", "exposure", "exposure-summary", "alert", "alert-detail",
            "alert-status-update", "alert-rule", "scenario-pack", "tenant-network",
        )),
        # M7 (110): back-office, custom specialists, tier-A mappings, ledger, capabilities
        *(f"{n}.schema.json" for n in (
            "admin.change-request", "admin.change-request-decision", "admin.user", "admin.user-create",
            "admin.user-created", "admin.user-patch", "agents.custom-specialist", "agents.playground-request",
            "agents.playground-result", "mapping.read", "mapping.action", "mapping.dry-run-request",
            "mapping.dry-run-result", "scn.snapshot-label", "commit.ledger-entry", "connectors.capabilities",
        )),
        # M8 (123): signal clustering — memberships, platform clustering settings + status
        *(f"signals.{n}.schema.json" for n in (
            "event-membership", "clustering-config", "clustering-config-update", "clustering-status",
        )),
    }
    assert names == expected


def test_files_are_canonical_json(script: ModuleType) -> None:
    for name, content in script.render_all().items():
        data = json.loads(content)
        assert content == json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n", name
        assert "\r" not in content
        if name != "index.json":
            assert data["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_sc_metadata_schema_is_camel_case(script: ModuleType) -> None:
    schema = json.loads(script.render_all()["agui.sc-metadata.schema.json"])
    assert set(schema["properties"]) == {
        "tenantId", "caseId", "runId", "seq", "stage", "prov", "label", "agent", "evidenceRefs"
    }
    assert schema["additionalProperties"] is False
    assert "runId" not in schema["required"]  # optional: older events without it stay valid


def test_check_detects_drift(script: ModuleType, tmp_path: Path) -> None:
    script.write(tmp_path)
    (tmp_path / "llm.claim.schema.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "old.schema.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "narrative@1.schema.json").unlink(missing_ok=True)
    problems = script.check(tmp_path)
    assert "stale: llm.claim.schema.json" in problems
    assert "unexpected: old.schema.json" in problems
    script.write(tmp_path)  # rewrite removes stale managed files
    assert script.check(tmp_path) == []


def test_committed_schemas_are_up_to_date(script: ModuleType) -> None:
    """Regenerate with ``uv run python scripts/export_schemas.py`` when this fails."""
    assert script.check(SCHEMAS_DIR) == []


def test_exported_schemas_are_self_contained(script: ModuleType) -> None:
    """No $ref/$defs/oneOf: json-schema-to-zod 2.8.x cannot resolve refs."""
    for name, content in script.render_all().items():
        assert '"$ref"' not in content and '"$defs"' not in content and '"oneOf"' not in content, name


def test_envelope_union_branches_pin_discriminators(script: ModuleType) -> None:
    schema = json.loads(script.render_all()["block.envelope.schema.json"])
    keys = {
        (b["properties"]["kind"]["const"], b["properties"]["v"]["const"]) for b in schema["anyOf"]
    }
    assert len(keys) == len(schema["anyOf"]) == 14  # mutually exclusive → anyOf == oneOf (M1 8 + M3 4 + M5 1 + M6 1)


def test_self_contained_inlines_and_rejects_recursion(script: ModuleType) -> None:
    schema = {
        "$defs": {"A": {"type": "string", "title": "A"}},
        "type": "object",
        "properties": {"a": {"$ref": "#/$defs/A", "description": "d"}, "u": {"oneOf": [{"$ref": "#/$defs/A"}]}},
    }
    out = script.self_contained(schema)
    assert out["properties"]["a"] == {"type": "string", "title": "A", "description": "d"}
    assert out["properties"]["u"] == {"anyOf": [{"type": "string", "title": "A"}]}
    loop = {"$defs": {"N": {"type": "array", "items": {"$ref": "#/$defs/N"}}}, "$ref": "#/$defs/N"}
    with pytest.raises(ValueError, match="recursive"):
        script.self_contained(loop)


def test_tuples_stay_closed_prefix_items(script: ModuleType) -> None:
    """``[lon_e6, lat_e6]`` and ``bbox`` are closed 2020-12 tuples of integers (gen-zod maps
    them to ``z.tuple``)."""
    data = json.loads(script.render_all()["block.network_map.v1.data.schema.json"])
    path = data["properties"]["links"]["items"]["properties"]["path"]
    point = path["items"]
    assert point["minItems"] == point["maxItems"] == 2 and "items" not in point
    assert [p["type"] for p in point["prefixItems"]] == ["integer", "integer"]
    assert point["prefixItems"][0]["maximum"] == 180_000_000 and point["prefixItems"][1]["maximum"] == 90_000_000
    props = json.loads(script.render_all()["block.network_map.v1.props.schema.json"])
    assert props["properties"]["bbox"]["minItems"] == props["properties"]["bbox"]["maxItems"] == 4
    link_required = data["properties"]["links"]["items"]["required"]
    assert "from" in link_required and "from_" not in json.dumps(data)


def test_index_sources_name_real_models(script: ModuleType) -> None:
    import importlib

    index = json.loads(script.render_all()["index.json"])
    for entry in index["schemas"]:
        module, _, name = entry["source"].rpartition(".")
        assert hasattr(importlib.import_module(module), name), entry
