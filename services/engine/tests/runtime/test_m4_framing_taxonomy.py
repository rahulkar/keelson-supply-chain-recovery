"""Taxonomy loader / validator (``90`` §1): the shipped ``data/taxonomy/events.yaml`` matches the
contract enum, and the loader fails fast on anything unexpected."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml
from keelson_contracts.common import EVENT_TYPE_KEYS, event_family_of
from keelson_contracts.optimizer import TARGET_KINDS_BY_OP

from keelson_engine.runtime.geo import get_geo, parse_chokepoints
from keelson_engine.runtime.nodes.taxonomy import (
    OP_PARAM,
    TaxonomyError,
    check_taxonomy,
    get_taxonomy,
    intake_event_types_block,
    load_taxonomy,
    parse_taxonomy,
    taxonomy_dirs,
)


def _doc() -> dict[str, Any]:
    path = next(d / "events.yaml" for d in taxonomy_dirs() if (d / "events.yaml").is_file())
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_shipped_taxonomy_matches_the_contract_enum() -> None:
    tax = load_taxonomy()
    assert set(tax.event_types) == set(EVENT_TYPE_KEYS)
    assert [k for k, _ in tax.ordered()] == list(EVENT_TYPE_KEYS)
    for key, tpl in tax.event_types.items():
        assert tpl.family == event_family_of(key)
        assert tpl.ttr_days.p10 <= tpl.ttr_days.p50 <= tpl.ttr_days.p90
        for p in tpl.perturbations:
            assert p.value.param == OP_PARAM[p.op]
    assert tax.template("OTHER").perturbations == ()
    assert get_taxonomy() == tax


@pytest.mark.parametrize(
    ("key", "op", "kinds", "value", "duration"),
    [
        ("SUP.delay", "lead_time_shift", {"supplier", "component", "factory", "po"}, ("prior", 5, 7, 14), None),
        ("SUP.capacity_loss", "capacity_scale", {"factory", "supplier"}, ("prior", 2500, 5000, 7500), 14),
        ("LOG.chokepoint_closure", "capacity_scale", {"chokepoint"}, ("fixed", 0), 14),
        ("LOG.port_closure", "capacity_scale", {"port"}, ("fixed", 0), 7),
        ("LOG.strike", "capacity_scale", {"port"}, ("prior", 1000, 3000, 6000), 7),
        ("NAT.storm", "capacity_scale", {"port", "factory"}, ("prior", 0, 0, 5000), 7),
        ("NAT.flood", "capacity_scale", {"port", "factory"}, ("prior", 0, 0, 5000), 7),
        ("DEM.spike", "demand_scale", {"cluster", "style"}, ("prior", 10, 18, 30), None),
        ("DEM.drop", "demand_scale", {"cluster", "style"}, ("prior", -30, -18, -10), None),
    ],
)
def test_contract_templates_and_priors(key: str, op: str, kinds: set[str], value: tuple[Any, ...],
                                       duration: int | None) -> None:
    tpl = get_taxonomy().template(key)
    [p] = tpl.perturbations
    assert p.op == op and kinds <= set(p.target_kinds)
    if value[0] == "fixed":
        assert p.value.fixed == value[1] and p.value.prior is None
    else:
        assert (p.value.prior.p10, p.value.prior.p50, p.value.prior.p90) == value[1:]
    assert (tpl.duration_days.p50 if tpl.duration_days else None) == duration
    assert tpl.ttr_days.p50 >= 0


def test_template_targets_are_resolvable_by_the_optimizer_or_reported() -> None:
    """Every target kind is a selector kind; kinds the optimizer does not resolve for the op are
    exactly the ones reported as an open issue (capacity on factory / supplier)."""
    unresolved = set()
    for tpl in get_taxonomy().event_types.values():
        for p in tpl.perturbations:
            for kind in p.target_kinds:
                if kind not in TARGET_KINDS_BY_OP[p.op] and kind != "receipt":
                    unresolved.add((p.op, kind))
    assert unresolved == {("capacity_scale", "factory"), ("capacity_scale", "supplier")}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["event_types"].pop("NAT.flood"), "missing"),
        (lambda d: d["event_types"].__setitem__("NAT.quake", copy.deepcopy(d["event_types"]["NAT.flood"])), "not in the contract enum"),
        (lambda d: d["event_types"]["SUP.delay"].__setitem__("famliy", "SUP"), "Extra inputs"),
        (lambda d: d["event_types"]["SUP.delay"]["perturbations"][0].__setitem__("targets", ["po"]), "Extra inputs"),
        (lambda d: d["event_types"]["SUP.delay"].__setitem__("family", "LOG"), "does not match"),
        (lambda d: d["event_types"]["SUP.delay"]["perturbations"][0]["value"].__setitem__("prior", {"p10": 9, "p50": 7, "p90": 14}), "p10 <= p50 <= p90"),
        (lambda d: d["event_types"]["SUP.delay"]["perturbations"][0]["value"].__setitem__("param", "factor_bp"), "carries delay_days"),
        (lambda d: d["event_types"]["LOG.strike"]["perturbations"][0].__setitem__("target_kinds", ["harbour"]), "target_kinds"),
        (lambda d: d["event_types"]["LOG.strike"]["perturbations"][0].__setitem__("resolve", "receipts"), "only valid for lead_time_shift"),
        (lambda d: d["event_types"]["LOG.port_closure"]["perturbations"][0]["value"].__setitem__("prior", {"p10": 0, "p50": 0, "p90": 0}), "exactly one"),
        (lambda d: d["event_types"]["OTHER"].__setitem__("perturbations", copy.deepcopy(d["event_types"]["DEM.spike"]["perturbations"])), "OTHER"),
        (lambda d: d["event_types"]["LOG.strike"].__setitem__("duration_days", {"p10": 0, "p50": 7, "p90": 14}), "duration_days"),
        (lambda d: d.__setitem__("versoin", 2), "Extra inputs"),
    ],
)
def test_loader_fails_fast(mutate: Any, message: str) -> None:
    doc = _doc()
    mutate(doc)
    with pytest.raises(TaxonomyError, match=message):
        parse_taxonomy(doc)


def test_missing_file_and_bad_yaml_raise(tmp_path: Path) -> None:
    with pytest.raises(TaxonomyError, match="cannot read"):
        load_taxonomy(tmp_path / "nope.yaml")
    bad = tmp_path / "events.yaml"
    bad.write_text("event_types: [unclosed", encoding="utf-8")
    with pytest.raises(TaxonomyError):
        load_taxonomy(bad)
    with pytest.raises(TaxonomyError, match="mapping"):
        parse_taxonomy(["not", "a", "mapping"])


def test_env_dir_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = _doc()
    doc["event_types"]["SUP.delay"]["label"] = "Tenant delay"
    (tmp_path / "events.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    monkeypatch.setenv("TAXONOMY_DIR", str(tmp_path))
    assert taxonomy_dirs()[0] == tmp_path
    assert load_taxonomy().template("SUP.delay").label == "Tenant delay"


def test_intake_block_lists_every_key_in_order() -> None:
    block = intake_event_types_block()
    lines = block.splitlines()
    assert [ln.split(":", 1)[0].removeprefix("- ") for ln in lines] == list(EVENT_TYPE_KEYS)


def test_startup_check_includes_the_chokepoint_catalogue() -> None:
    assert check_taxonomy() is get_taxonomy()
    cps = get_geo().chokepoints
    assert set(cps) == {"SUEZ", "BAB_EL_MANDEB", "CAPE", "PANAMA", "MALACCA", "HORMUZ", "BOSPORUS", "GIBRALTAR", "DOVER"}
    for cp in cps.values():
        assert len(cp.polygon) >= 4 and cp.polygon[0] == cp.polygon[-1]
        assert all(isinstance(v, int) for pt in cp.polygon for v in pt)
        assert cp.aliases


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "X", "aliases": [], "polygon": [[0, 0], [1, 1], [0, 0]]},
        {"name": "X", "aliases": [], "polygon": [[0, 0], [1, 1], [2, 0], [3, 3]]},
        {"name": "X", "aliases": [], "polygon": [[0, 0], [1, 1], [2, 0], [0, 0]], "rings": 1},
        {"name": "X", "aliases": [], "polygon": [[200_000_000, 0], [1, 1], [2, 0], [200_000_000, 0]]},
    ],
)
def test_chokepoint_catalogue_is_strict(entry: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="chokepoints.json"):
        parse_chokepoints({"chokepoints": {"X": entry}})
