"""Entity resolution quality (``90`` §1): region / country mentions resolve through the gazetteer
(``data/geo/regions.json``) to the suppliers, factories, ports and DCs located there in the
snapshot; material mentions ("wadding and zips") resolve through ``data/taxonomy/synonyms.yaml``
to the snapshot's component items and walk the BOM; anything else stays an honest inference.

The snapshot directory mirrors the Merxa full seed (``fakes.scn_fixture`` locations plus the second
Bangladesh factory, the Cambodian trim supplier and every component item, exported like Merxa does:
code only, no name); the ProblemData is the full-shaped one of ``test_m4_framing_support`` with the
seed's outerwear / knit BOM."""

from __future__ import annotations

import datetime as dt
from contextlib import asynccontextmanager
from typing import Any, get_args

import pytest
from fakes.merxa_full import DCS
from fakes.scn_fixture import FACTORIES as SCN_FACTORIES
from fakes.scn_fixture import PORTS as SCN_PORTS
from fakes.scn_fixture import SUPPLIERS as SCN_SUPPLIERS
from keelson_contracts.common import Provenance
from keelson_contracts.llm import DisruptionExtract, EntityMention, EntityMentionType
from keelson_contracts.optimizer import ImpactRequest, ProblemData
from keelson_contracts.scenario import CapacityScale, DemandScale, LeadTimeShift
from test_m4_framing_support import FakeCaller, full_problem, po_line_dicts, run_ctx

from keelson_engine.runtime.context import bind_run
from keelson_engine.runtime.geo import get_geo
from keelson_engine.runtime.nodes.framing import (
    SnapshotDirectory,
    build_scenario,
    load_snapshot_directory,
    place_index,
    region_members,
    resolve_components,
    resolve_static,
    retarget_scenario,
)
from keelson_engine.runtime.nodes.prompts import intake_instruction
from keelson_engine.runtime.nodes.taxonomy import (
    ENTITY_TYPE_GUIDANCE,
    TaxonomyError,
    get_gazetteer,
    get_synonyms,
    intake_entity_types_block,
    intake_event_types_block,
    normalize_term,
    parse_gazetteer,
    parse_synonyms,
    singular,
)
from keelson_engine.runtime.scn_store import SnapshotRef

# ─────────────────────────── full-shaped snapshot fixture ───────────────────────────

EXTRA_SUPPLIERS = (("TRIM-KH-01", "KH"), ("CMT-KH-01", "KH"))
EXTRA_FACTORIES = (("F-BD-02", "BD", "CMT-BD-01"), ("F-KH-01", "KH", "CMT-KH-01"))
COMPONENTS = ("CMP-WAD-01", "CMP-ZIP-01", "CMP-WAD-02", "CMP-ZIP-02", "CMP-WAD-03", "CMP-ZIP-03", "CMP-SHL-01",
              "CMP-LIN-01", "CMP-YRN-01", "CMP-FLC-01", "CMP-YRN-02", "CMP-LBL-01")
# component, supplier, factory (None = every factory)  -- the seed's outerwear / knit BOM
PUFFER_BOM = (("CMP-SHL-01", "FAB-CN-01", None), ("CMP-LIN-01", "FAB-CN-01", None), ("CMP-LBL-01", "FAB-CN-01", None),
              ("CMP-WAD-01", "TRIM-BD-01", "F-BD-01"), ("CMP-ZIP-01", "TRIM-BD-01", "F-BD-01"),
              ("CMP-WAD-01", "TRIM-BD-01", "F-BD-02"), ("CMP-ZIP-01", "TRIM-BD-01", "F-BD-02"),
              ("CMP-WAD-02", "TRIM-KH-01", "F-KH-01"), ("CMP-ZIP-02", "TRIM-KH-01", "F-KH-01"),
              ("CMP-WAD-03", "TRIM-TR-01", "F-TR-01"), ("CMP-ZIP-03", "TRIM-TR-01", "F-TR-01"),
              ("CMP-WAD-03", "TRIM-TR-01", "F-MA-01"), ("CMP-ZIP-03", "TRIM-TR-01", "F-MA-01"))
KNIT_BOM = (("CMP-YRN-01", "SUP-VN-01", "F-VN-01"), ("CMP-FLC-01", "SUP-VN-01", "F-VN-01"),
            ("CMP-YRN-02", "CMT-TR-01", "F-TR-01"), ("CMP-YRN-02", "CMT-TR-01", "F-MA-01"))
TR_PO = "PO-TR-0001"


def directory() -> SnapshotDirectory:
    """``scn_location`` (supplier / factory / port / dc) + ``scn_item`` component rows, shaped like the
    Merxa export (DC country ``dc[:2]`` as in ``fakes.scn_fixture``, so ``UK`` must normalise to GB)."""
    locations: list[dict[str, Any]] = [{"kind": "dc", "code": dc, "name": f"{dc} DC", "country": dc[:2]} for dc in DCS]
    locations += [{"kind": "port", "code": code, "name": name, "country": country}
                  for code, name, country, _lat, _lon in SCN_PORTS]
    locations += [{"kind": "supplier", "code": code, "name": code, "country": country}
                  for code, country in (*SCN_SUPPLIERS, *EXTRA_SUPPLIERS)]
    locations += [{"kind": "factory", "code": code, "name": code, "country": country}
                  for code, country, _sup in (*SCN_FACTORIES, *EXTRA_FACTORIES)]
    return SnapshotDirectory(locations=tuple(locations),
                             components=tuple({"code": c, "family": None} for c in COMPONENTS))


def seed_problem() -> ProblemData:
    base = full_problem()
    data = base.model_dump(mode="json")
    styles = sorted({s.style_code for s in base.skus})
    data["bom"] = [{"style_code": st, "component_code": c, "supplier_code": sup, "factory_code": f}
                   for st in styles for c, sup, f in (PUFFER_BOM if st.startswith("PUF-") else KNIT_BOM)]
    for pl in data["po_lines"]:  # the second BD PO is made in the second BD factory (as in the seed)
        if pl["po_code"] == "PO-BD-0002":
            pl["factory_code"] = "F-BD-02"
    puffer = [s.code for s in base.skus if s.style_code.startswith("PUF-")][:3]
    eta = dt.date(2026, 11, 2)
    for n, sku in enumerate(puffer, start=1):  # a near-shore puffer PO (Türkiye) the BD walk must not touch
        ref = f"{TR_PO}#{n}"
        data["po_lines"].append({"ref": ref, "po_code": TR_PO, "supplier_code": "CMT-TR-01", "factory_code": "F-TR-01",
                                 "dest_dc": "DE1", "sku": sku, "qty": 200, "eta": eta.isoformat(),
                                 "ex_factory": (eta - dt.timedelta(days=6)).isoformat(), "mode": "road",
                                 "lane_code": "IST-DE1-RD"})
        data["receipts"].append({"ref": ref, "dc_code": "DE1", "sku": sku, "qty": 200, "eta": eta.isoformat()})
    return ProblemData.model_validate(data)


PROBLEM = seed_problem()
DIRECTORY = directory()
BD_LINES = {pl.ref for pl in PROBLEM.po_lines if pl.po_code.startswith("PO-BD-")}
TR_LINES = {pl.ref for pl in PROBLEM.po_lines if pl.po_code == TR_PO}


def extract(event_type: str, mentions: list[tuple[str, str]], **kw: Any) -> DisruptionExtract:
    data: dict[str, Any] = {
        "event_family": event_type.split(".", 1)[0], "event_type": event_type, "description": f"{event_type} test",
        "entity_mentions": [{"type": t, "text": x} for t, x in mentions], "delay_days": None,
        "demand_change_pct": None, "start_date": None, "duration_days": None, "is_hypothetical": False,
    }
    data.update(kw)
    return DisruptionExtract.model_validate(data)


async def compile_(ext: DisruptionExtract, *, with_directory: bool = True, caller: FakeCaller | None = None) -> Any:
    return await build_scenario(
        run_ctx(), caller or FakeCaller(), extract=ext, overrides={}, po_lines=po_line_dicts(PROBLEM),
        all_clusters=[c.code for c in PROBLEM.clusters], scenario_id="scn-e", snapshot_data=PROBLEM, problem=PROBLEM,
        directory=DIRECTORY if with_directory else None,
    )


def codes(scenario: Any, kind: str) -> set[str]:
    return {m.code for m in scenario.matched if m.type == kind}


def assumption(scenario: Any, key: str) -> Any:
    return next((a for a in scenario.assumptions if a.key == key), None)


# ─────────────────────────── regions → located entities ───────────────────────────


async def test_bangladesh_resolves_to_the_suppliers_and_factories_located_there() -> None:
    s = await compile_(extract("SUP.delay", [("region", "Bangladesh")], delay_days=7))
    assert codes(s, "region") == {"BD"}
    assert any(m.type == "region" and m.label == "Bangladesh" for m in s.matched)
    assert codes(s, "supplier") >= {"TRIM-BD-01", "CMT-BD-01"} and codes(s, "factory") == {"F-BD-01", "F-BD-02"}
    assert not codes(s, "supplier") & {"SUP-VN-01", "CMT-TR-01", "TRIM-KH-01"}
    [p] = s.perturbations
    assert isinstance(p, LeadTimeShift) and set(p.target.ids) == BD_LINES  # both BD factories, nothing else
    assert assumption(s, "affected_receipts").prov == Provenance.FACT
    assert assumption(s, "unmatched_mentions") is None and assumption(s, "region_scope") is None


async def test_trim_delay_message_resolves_supplier_materials_and_region() -> None:
    """'Our trim supplier Meghna Trims says wadding and zips for the Bangladesh POs are delayed 7 days.'"""
    mentions = [("supplier", "Meghna Trims"), ("component", "wadding and zips"), ("region", "Bangladesh")]
    s = await compile_(extract("SUP.delay", mentions, delay_days=7))
    assert codes(s, "component") == {"CMP-WAD-01", "CMP-ZIP-01"}  # narrowed to Meghna's / Bangladesh's items
    assert codes(s, "supplier") >= {"TRIM-BD-01"} and codes(s, "factory") == {"F-BD-01", "F-BD-02"}
    [p] = s.perturbations
    assert set(p.target.ids) == BD_LINES and not set(p.target.ids) & TR_LINES
    assert codes(s, "po") == {"PO-BD-0001", "PO-BD-0002"}
    assert assumption(s, "unmatched_mentions") is None  # nothing is "not found"
    golden = {"TRIM-BD-01", "CMP-WAD-01", "CMP-ZIP-01", "F-BD-01", "F-BD-02", "PO-BD-0001", "PO-BD-0002"}
    assert golden <= {m.code for m in s.matched}
    ImpactRequest(data=PROBLEM, perturbations=list(s.perturbations))


async def test_wadding_and_zips_as_separate_mentions_walk_to_the_bd_po_lines() -> None:
    mentions = [("component", "wadding"), ("component", "Zips"), ("supplier", "TRIM-BD-01")]
    s = await compile_(extract("SUP.delay", mentions))
    assert codes(s, "component") == {"CMP-WAD-01", "CMP-ZIP-01"}
    assert set(s.perturbations[0].target.ids) == BD_LINES
    assert assumption(s, "unmatched_mentions") is None


async def test_a_material_without_context_keeps_every_matching_component() -> None:
    s = await compile_(extract("SUP.delay", [("component", "wadding")]))
    assert codes(s, "component") == {"CMP-WAD-01", "CMP-WAD-02", "CMP-WAD-03"}
    assert set(s.perturbations[0].target.ids) == BD_LINES | TR_LINES  # every factory using any wadding


async def test_a_region_narrows_a_material_to_the_items_sourced_there() -> None:
    s = await compile_(extract("SUP.delay", [("component", "zippers"), ("region", "Türkiye")]))
    assert codes(s, "component") == {"CMP-ZIP-03"}
    assert codes(s, "region") == {"TR"}
    assert set(s.perturbations[0].target.ids) == TR_LINES


async def test_vietnam_capacity_loss_targets_the_vietnamese_supplier_and_factory() -> None:
    s = await compile_(extract("SUP.capacity_loss", [("region", "Vietnam")]))
    [p] = s.perturbations
    assert isinstance(p, CapacityScale)
    assert set(p.target.kinds) == {"factory", "supplier"} and set(p.target.ids) == {"F-VN-01", "SUP-VN-01"}
    assert codes(s, "supplier") == {"SUP-VN-01"} and codes(s, "factory") == {"F-VN-01"}
    assert all(m.code in {"VN", "SUP-VN-01", "F-VN-01"} for m in s.matched)


async def test_a_storm_over_viet_nam_hits_its_ports_and_factories() -> None:
    s = await compile_(extract("NAT.storm", [("region", "Viet Nam")]))
    [p] = s.perturbations
    assert set(p.target.ids) >= {"VNSGN", "F-VN-01"}
    assert all(i.startswith("VN") or i == "F-VN-01" for i in p.target.ids)


async def test_eu_strike_targets_eu_ports_only() -> None:
    s = await compile_(extract("LOG.strike", [("region", "EU")]))
    [p] = s.perturbations
    assert p.target.kinds == ["port"] and {"NLRTM", "DEHAM", "PLGDN"} <= set(p.target.ids)
    assert not {i for i in p.target.ids if i[:2] in {"GB", "TR", "MA", "BD", "VN", "AE", "LK"}}


async def test_uk_port_closure_and_uk_demand_resolve_the_uk_locations() -> None:
    s = await compile_(extract("LOG.port_closure", [("region", "UK")]))
    ids = set(s.perturbations[0].target.ids)
    assert "GBFXT" in ids and all(i.startswith("GB") for i in ids)
    d = await compile_(extract("DEM.spike", [("region", "the UK")], demand_change_pct=20))
    [p] = d.perturbations
    assert isinstance(p, DemandScale) and (p.target.kinds, p.target.ids) == (["dc"], ["UK1"])  # country "UK" → GB


async def test_region_typed_as_something_else_still_resolves() -> None:
    caller = FakeCaller()
    s = await compile_(extract("SUP.delay", [("po", "the Bangladesh POs"), ("sku", "zips"), ("supplier", "Meghna")],
                               delay_days=7), caller=caller)
    assert codes(s, "region") == {"BD"} and codes(s, "component") == {"CMP-ZIP-01"}
    assert set(s.perturbations[0].target.ids) == BD_LINES
    assert assumption(s, "unmatched_mentions") is None
    assert ("search_entities", {"query": "the Bangladesh POs", "types": ["po"], "limit": 5}) in caller.calls


async def test_promote_retarget_keeps_the_region_scope() -> None:
    s = await compile_(extract("SUP.delay", [("region", "Bangladesh")], delay_days=7))
    newer = PROBLEM.model_copy(update={"po_lines": [pl for pl in PROBLEM.po_lines if pl.po_code != "PO-BD-0001"]})
    moved = retarget_scenario(s, newer, "Merxa")
    assert {r.split("#")[0] for r in moved.perturbations[0].target.ids} == {"PO-BD-0002"}


# ─────────────────────────── honesty: unresolved stays inference ───────────────────────────


async def test_unresolvable_mentions_stay_inference() -> None:
    s = await compile_(extract("SUP.delay", [("region", "Atlantis"), ("component", "unobtainium"),
                                             ("component", "wadding")]))
    miss = assumption(s, "unmatched_mentions")
    assert miss is not None and miss.prov == Provenance.INFERENCE
    assert miss.value_text == "Atlantis, unobtainium" and miss.label == "Not found in Merxa"
    assert codes(s, "component") == {"CMP-WAD-01", "CMP-WAD-02", "CMP-WAD-03"}


async def test_a_region_with_nothing_located_there_is_an_inference_not_a_miss() -> None:
    s = await compile_(extract("SUP.delay", [("region", "Brazil")], delay_days=7))
    assert codes(s, "region") == {"BR"} and s.perturbations == []
    assert assumption(s, "unmatched_mentions") is None
    note = assumption(s, "region_scope")
    assert note.prov == Provenance.INFERENCE
    assert note.value_text == "No supplier or factory in the Merxa data is located in Brazil"
    assert assumption(s, "affected_receipts").prov == Provenance.INFERENCE


async def test_without_a_snapshot_directory_regions_fall_back_to_ports_and_say_so() -> None:
    s = await compile_(extract("SUP.delay", [("region", "Bangladesh")], delay_days=7), with_directory=False)
    assert s.perturbations == [] and assumption(s, "unmatched_mentions") is None
    assert assumption(s, "region_scope").value_text == (
        "The Merxa data has no supplier or factory locations, so I cannot tell which are in Bangladesh")
    flood = await compile_(extract("NAT.flood", [("region", "Bangladesh")]), with_directory=False)
    assert "BDCGP" in flood.perturbations[0].target.ids  # UN/LOCODE country prefix


async def test_a_material_the_data_does_not_have_is_not_found() -> None:
    s = await compile_(extract("SUP.delay", [("component", "buttons")]))
    assert assumption(s, "unmatched_mentions").value_text == "buttons"


# ─────────────────────────── resolver units ───────────────────────────


@pytest.mark.parametrize(
    ("text", "code"),
    [("Bangladesh", "BD"), ("bangladeshi", "BD"), ("the Bangladesh POs", "BD"), ("BGD", "BD"), ("BD", "BD"),
     ("Vietnam", "VN"), ("Viet Nam", "VN"), ("VN", "VN"), ("Türkiye", "TR"), ("Turkiye", "TR"), ("Turkey", "TR"),
     ("turkish suppliers", "TR"), ("UK", "GB"), ("uk", "GB"), ("UK DCs", "GB"), ("United Kingdom", "GB"),
     ("EU", "EU"), ("the European Union", "EU"), ("South Asia", "SOUTH_ASIA"), ("near-shore", "NEAR_SHORE"),
     ("Latin America", "LATAM"), ("Netherlands", "NL"), ("Holland", "NL"),
     ("us", None), ("IT systems", None), ("can", None), ("Atlantis", None), ("", None), ("Red Sea", None)],
)
def test_gazetteer_resolution(text: str, code: str | None) -> None:
    area = get_gazetteer().resolve(text)
    assert (area.code if area else None) == code


def test_gazetteer_regions_and_country_values() -> None:
    gaz = get_gazetteer()
    eu = gaz.area("EU")
    assert eu is not None and {"DE", "NL", "PL", "FR"} <= set(eu.countries) and "GB" not in eu.countries
    assert gaz.countries_of("BD") == ("BD",) and gaz.countries_of("XK") == ("XK",) and gaz.countries_of("nope") == ()
    for value, iso2 in (("UK", "GB"), ("GB", "GB"), ("gb", "GB"), ("BGD", "BD"), ("Bangladesh", "BD"),
                        ("Türkiye", "TR"), (" vn ", "VN"), ("", None), (None, None), ("Narnia", None)):
        assert gaz.country_code(value) == iso2, value


def test_gazetteer_is_strict() -> None:
    ok = {"version": 1, "countries": {"AA": {"name": "Aland", "iso3": "AAA", "aliases": ["al"]}}}
    assert parse_gazetteer(ok).resolve("Aland") is not None
    with pytest.raises(TaxonomyError, match="names both"):
        parse_gazetteer({"version": 1, "countries": {"AA": {"name": "X", "iso3": "AAA"},
                                                     "BB": {"name": "Y", "iso3": "BBB", "aliases": ["x"]}}})
    with pytest.raises(TaxonomyError, match="unknown countries"):
        parse_gazetteer({**ok, "regions": {"R1": {"name": "R", "countries": ["ZZ"]}}})
    with pytest.raises(TaxonomyError, match="invalid gazetteer"):
        parse_gazetteer({"version": 1, "countries": {"aa": {"name": "X", "iso3": "AAA"}}})


@pytest.mark.parametrize(
    ("word", "single"),
    [("zips", "zip"), ("zippers", "zipper"), ("batteries", "battery"), ("boxes", "box"), ("labels", "label"),
     ("dress", "dress"), ("wadding", "wadding"), ("bus", "bus"), ("gas", "gas"), ("laces", "lace")],
)
def test_singular(word: str, single: str) -> None:
    assert singular(word) == single


def test_synonyms_find_materials() -> None:
    syn = get_synonyms()
    assert [m.term for m in syn.find("wadding and zips")] == ["wadding", "zip"]
    assert [m.term for m in syn.find("Zippers")] == ["zip"]
    assert [m.term for m in syn.find("shell fabric")] == ["shell fabric"]  # one material, not also "fabric"
    assert [m.term for m in syn.find("padding")] == ["wadding"]
    assert syn.find("unobtainium") == []
    assert normalize_term("Two-way Zips") == "two way zip"
    with pytest.raises(TaxonomyError, match="listed under both"):
        parse_synonyms({"version": 1, "materials": {"zip": {"aliases": ["zipper"]}, "zipper": {}}})
    with pytest.raises(TaxonomyError, match="invalid synonyms"):
        parse_synonyms({"version": 1, "materials": {"zip": {"code_tokens": ["z"]}}})


def test_component_resolution_by_code_name_and_segment() -> None:
    idx = place_index(PROBLEM, get_geo(), DIRECTORY)
    assert [r.code for r in resolve_components("cmp-zip-01", idx)] == ["CMP-ZIP-01"]
    assert [r.code for r in resolve_components("zips", idx)] == ["CMP-ZIP-01", "CMP-ZIP-02", "CMP-ZIP-03"]
    assert resolve_components("cmp", idx) == []  # a segment every component shares names nothing
    # a connector that exports component names: matched by name too ("insulation" is a wadding alias)
    named = SnapshotDirectory(locations=DIRECTORY.locations,
                              components=({"code": "TRM-0001", "name": "Polyester insulation 120 gsm"},
                                          {"code": "TRM-0002", "name": "Metal zip 70 cm"}))
    idx2 = place_index(None, get_geo(), named)
    assert [r.code for r in resolve_components("wadding", idx2)] == ["TRM-0001"]
    assert [(r.code, r.label) for r in resolve_components("zippers", idx2)] == [("TRM-0002", "Metal zip 70 cm")]
    assert [r.code for r in resolve_components("metal zip", idx2)] == ["TRM-0002"]


def test_component_resolution_by_exported_family() -> None:
    """Live Merxa snapshots carry a component's material name + kind in ``item.family`` (scn/1.0 has
    no item name): material words resolve through it and the family is the label shown."""
    live = SnapshotDirectory(locations=DIRECTORY.locations, components=(
        {"code": "CMP-SHL-01", "family": "Ripstop nylon shell fabric"},
        {"code": "CMP-FLC-01", "family": "Polyester teddy fleece (fleece fabric)"},
        {"code": "CMP-LBL-01", "family": "Woven care label"},
    ))
    idx = place_index(None, get_geo(), live)
    assert [(r.code, r.label) for r in resolve_components("ripstop", idx)] == [
        ("CMP-SHL-01", "Ripstop nylon shell fabric")]
    assert [r.code for r in resolve_components("teddy fleece", idx)] == ["CMP-FLC-01"]
    assert [r.code for r in resolve_components("fleece fabric", idx)] == ["CMP-FLC-01"]
    assert [r.code for r in resolve_components("care labels", idx)] == ["CMP-LBL-01"]


def test_static_region_resolution_and_members() -> None:
    idx = place_index(PROBLEM, get_geo(), DIRECTORY)
    ref = resolve_static(EntityMention(type="region", text="Bangladesh"), idx, get_geo())
    assert ref is not None and (ref.type, ref.code, ref.label) == ("region", "BD", "Bangladesh")
    assert {r.code for r in region_members("BD", ["supplier", "factory"], idx)} == {
        "TRIM-BD-01", "CMT-BD-01", "F-BD-01", "F-BD-02"}
    assert {r.code for r in region_members("SOUTHEAST_ASIA", ["factory"], idx)} == {"F-VN-01", "F-KH-01"}
    assert [r.code for r in region_members("GB", ["dc"], idx)] == ["UK1"]
    # chokepoints still win over regions ("Red Sea" is Bab-el-Mandeb), and a bare region name is not a port
    red = resolve_static(EntityMention(type="region", text="Red Sea"), idx, get_geo())
    assert red is not None and red.code == "BAB_EL_MANDEB"
    other = resolve_static(EntityMention(type="other", text="Vietnam"), idx, get_geo())
    assert other is not None and (other.type, other.code) == ("region", "VN")


# ─────────────────────────── snapshot directory read ───────────────────────────


class _Reader:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail

    async def fetch(self, sql: str, params: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params)))
        if self.fail:
            raise LookupError("boom")
        if "scn_location" in sql:
            return [{"kind": "supplier", "code": "TRIM-BD-01", "name": "Meghna Trims Ltd", "country": "BD"}]
        return [{"code": "CMP-WAD-01", "family": None}]


async def test_load_snapshot_directory_reads_locations_and_components() -> None:
    run = run_ctx()
    reader = _Reader()

    @asynccontextmanager
    async def factory(tenant: str) -> Any:
        assert tenant == run.tenant
        yield reader

    run.deps.scn.reader_factory = factory
    run.deps.scn.snapshot = SnapshotRef(id="snap-1", connector="merxa")
    got = await load_snapshot_directory(run, "snap-1")
    assert got is not None and got.locations[0]["code"] == "TRIM-BD-01" and got.components[0]["code"] == "CMP-WAD-01"
    assert all(p == {"t": run.tenant, "s": "snap-1"} for _sql, p in reader.calls) and len(reader.calls) == 2
    assert "kind IN ('supplier', 'factory', 'port', 'dc')" in reader.calls[0][0]
    assert await load_snapshot_directory(run, None) is None
    reader.fail = True
    assert await load_snapshot_directory(run, "snap-1") is None  # unreadable: regions fall back to ports


# ─────────────────────────── intake prompt ───────────────────────────


def test_intake_prompt_lists_taxonomy_keys_and_entity_types() -> None:
    kinds = [k for k, _ in ENTITY_TYPE_GUIDANCE]
    assert sorted(kinds) == sorted(get_args(EntityMentionType))  # every contract type, once
    block = intake_entity_types_block()
    for kind in ("chokepoint", "region", "port", "component"):
        assert f"- {kind}: " in block
    assert "Bangladesh" in block and "wadding" in block
    assert "(mentions: supplier, component, factory, po, sku, style, dc, region)" in intake_event_types_block()
    with bind_run(run_ctx()):
        prompt = intake_instruction(None)  # type: ignore[arg-type]
    assert "Taxonomy event types:" in prompt and "- SUP.delay: Supplier delay" in prompt
    assert "- LOG.chokepoint_closure: " in prompt and "Entity mention types:" in prompt
    assert "- region: " in prompt and "- component: " in prompt


# ─────────────────────────── review fixes ───────────────────────────


async def test_a_loose_region_reading_is_shown_as_an_inference() -> None:
    """A region found inside a mention typed as something else is an interpretation, not a fact:
    it still scopes the walk, but the card shows how the mention was read (and cannot auto-confirm)."""
    s = await compile_(extract("SUP.delay", [("po", "the Bangladesh POs"), ("sku", "zips")], delay_days=7))
    assert codes(s, "region") == {"BD"} and set(s.perturbations[0].target.ids) == BD_LINES
    note = assumption(s, "mention_readings")
    assert note is not None and note.prov == Provenance.INFERENCE
    assert note.value_text == "'the Bangladesh POs' as Bangladesh; 'zips' as a material (component items)"
    typed = await compile_(extract("SUP.delay", [("region", "Bangladesh")], delay_days=7))
    assert assumption(typed, "mention_readings") is None  # typed as a region: resolved as typed


@pytest.mark.parametrize("kind", ["style", "sku", "component"])
async def test_a_place_word_that_describes_a_product_is_not_a_region(kind: str) -> None:
    """'Turkish cotton towels' / 'Egyptian cotton' describe a product: they must not expand to every
    supplier and factory in Türkiye / Egypt (nor a DC in Germany for 'German-made parkas')."""
    s = await compile_(extract("SUP.delay", [(kind, "Turkish cotton towels")], delay_days=7))
    assert codes(s, "region") == set() and not codes(s, "supplier") & {"CMT-TR-01", "TRIM-TR-01"}
    assert not any(set(p.target.ids) & TR_LINES for p in s.perturbations)
    assert assumption(s, "unmatched_mentions").value_text == "Turkish cotton towels"
    d = await compile_(extract("DEM.spike", [(kind, "German-made parkas")], demand_change_pct=20))
    assert all("DE1" not in p.target.ids for p in d.perturbations) and codes(d, "region") == set()


async def test_a_product_mention_that_is_only_a_place_is_still_read_as_the_region() -> None:
    s = await compile_(extract("SUP.delay", [("style", "Bangladesh")], delay_days=7))
    assert codes(s, "region") == {"BD"} and set(s.perturbations[0].target.ids) == BD_LINES
    assert assumption(s, "mention_readings").value_text == "'Bangladesh' as Bangladesh"


async def test_one_mention_naming_two_countries_resolves_both() -> None:
    s = await compile_(extract("SUP.capacity_loss", [("region", "Vietnam and Cambodia")]))
    assert codes(s, "region") == {"VN", "KH"}
    [p] = s.perturbations
    assert set(p.target.ids) == {"SUP-VN-01", "F-VN-01", "TRIM-KH-01", "CMT-KH-01", "F-KH-01"}
    assert assumption(s, "region_scope") is None


@pytest.mark.parametrize(
    ("text", "codes_"),
    [("Vietnam and Cambodia", ["VN", "KH"]), ("Eastern Europe and Türkiye", ["CEE", "TR"]),
     ("South Asia", ["SOUTH_ASIA"]), ("the UK and EU DCs", ["GB", "EU"]), ("Bangladesh", ["BD"]),
     ("Great Britain (UK)", ["GB"]), ("IT systems in us", []), ("Atlantis", [])],
)
def test_gazetteer_resolve_all(text: str, codes_: list[str]) -> None:
    assert [a.code for a in get_gazetteer().resolve_all(text)] == codes_


def test_gazetteer_resolve_all_without_phrases_needs_the_whole_mention() -> None:
    gaz = get_gazetteer()
    assert [a.code for a in gaz.resolve_all("Egyptian cotton", phrase=False)] == []
    assert [a.code for a in gaz.resolve_all("Egyptian", phrase=False)] == ["EG"]
    assert [a.code for a in gaz.resolve_all("BGD", phrase=False)] == ["BD"]


def test_gazetteer_phrase_ties_go_to_the_lower_code() -> None:
    gaz = parse_gazetteer({"version": 1, "countries": {
        "BB": {"name": "Bbbbland", "iso3": "BBB"}, "AA": {"name": "Zzzzland", "iso3": "AAA"}}})
    hit = gaz.resolve("from Bbbbland to Zzzzland")  # equal-length names: the lower code wins
    assert hit is not None and hit.code == "AA"


def test_a_material_matches_even_when_every_component_shares_its_code_segment() -> None:
    """With wadding items only, WAD is a segment every component shares; a catalogued material
    still matches it (the shared-segment rule only guards words taken from the user's text)."""
    only_wadding = SnapshotDirectory(locations=(), components=({"code": "CMP-WAD-01"}, {"code": "CMP-WAD-02"}))
    idx = place_index(None, get_geo(), only_wadding)
    assert [r.code for r in resolve_components("wadding", idx)] == ["CMP-WAD-01", "CMP-WAD-02"]
    assert resolve_components("cmp", idx) == [] and resolve_components("wad", idx) == []


def test_region_guidance_excludes_product_descriptions() -> None:
    line = dict(ENTITY_TYPE_GUIDANCE)["region"]
    assert "one mention per region" in line and "Egyptian cotton" in line
