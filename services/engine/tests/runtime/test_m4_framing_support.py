"""Shared fixtures for the M4 framing / refine / follow-up tests (no tests in here).

* :func:`full_problem` — a full-shaped ``ProblemData`` v2 built from the same master data as
  :class:`fakes.merxa_full.FullMerxaStub` (4 DCs, 18 clusters, 180 SKUs, DC stock, cluster demand
  W41–W49) plus every M3 list, with the Act 2 network of ``90`` §8: the Vietnamese knit lanes
  ``SGN-HAM-SEA`` / ``SGN-GDN-SEA`` route **via SUEZ** and fall back to the catalogued Cape
  variants ``SGN-HAM-CAPE`` / ``SGN-GDN-CAPE`` (+12 days), as composed by ``problem_scn``
  (``SGN-HAM-SEA+HAM-DE1-RD`` …).
* :class:`FakeCaller` — a ``ToolCaller`` with canned ``get_entity`` / ``search_entities`` /
  ``get_bom_where_used`` answers that records every call.
* :func:`run_ctx` / :func:`run_node` — a bound :class:`RunContext` and a one-node ADK workflow, to
  drive a node function (or a FunctionNode) with FakeLlm turns.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from fakes.core import FakeCore, FakeRepo
from fakes.merxa_full import CLUSTERS, DCS, SIZES, STYLES
from google.adk.apps.app import App
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.workflow import START, FunctionNode, Workflow
from google.genai import types
from keelson_contracts.optimizer import ProblemData
from keelson_contracts.runs import Actor

from keelson_engine.runtime.context import RunContext, bind_run
from keelson_engine.runtime.errors import McpToolError
from keelson_engine.runtime.spec import AgentSpec, PlanningDefaults

TENANT = "demo-retail"
AS_OF = dt.date(2026, 10, 5)
PRIYA = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}

DC_POINTS = {"UK1": (0.3529, 51.4935), "NL1": (6.1724, 51.3704), "DE1": (11.0299, 50.9848), "PL1": (16.9252, 52.4064)}
PORT_ROWS = (
    ("BDCGP", "Chittagong", 91.80, 22.3089), ("LKCMB", "Colombo", 79.8428, 6.9497),
    ("VNSGN", "Ho Chi Minh City (Cat Lai)", 106.788, 10.7626), ("GBFXT", "Felixstowe", 1.3301, 51.9563),
    ("NLRTM", "Rotterdam", 4.1453, 51.9496), ("DEHAM", "Hamburg", 9.9663, 53.5395),
    ("PLGDN", "Gdańsk", 18.6763, 54.4013), ("AEJEA", "Jebel Ali", 55.0618, 25.0115),
    ("TRIST", "Istanbul (Ambarli)", 28.6833, 40.9667), ("MATNG", "Tanger Med", -5.5, 35.8833),
)
FACTORIES = (("F-BD-01", "CMT-BD-01", 90.4, 23.8), ("F-VN-01", "SUP-VN-01", 106.6, 10.9),
             ("F-TR-01", "CMT-TR-01", 29.0, 41.0), ("F-MA-01", "CMT-MA-01", -5.8, 35.7))
# code, origin, dest_dc, mode, transit, via, alt   (composed like problem_scn.compose_inbound_lanes)
INBOUND = (
    ("CGP-FXT-SEA+FXT-UK1-RD", "BDCGP", "UK1", "sea", 33, ["LKCMB", "CAPE", "GBFXT"], "CGP-UK1-SA"),
    ("CGP-RTM-SEA+RTM-NL1-RD", "BDCGP", "NL1", "sea", 33, ["LKCMB", "CAPE", "NLRTM"], "CGP-NL1-SA"),
    ("CGP-UK1-SA", "BDCGP", "UK1", "sea_air", 16, ["AEJEA"], None),
    ("CGP-NL1-SA", "BDCGP", "NL1", "sea_air", 16, ["AEJEA"], None),
    ("SGN-HAM-SEA+HAM-DE1-RD", "VNSGN", "DE1", "sea", 36, ["SUEZ", "DEHAM"], "SGN-HAM-CAPE+HAM-DE1-RD"),
    ("SGN-GDN-SEA+GDN-PL1-RD", "VNSGN", "PL1", "sea", 37, ["SUEZ", "PLGDN"], "SGN-GDN-CAPE+GDN-PL1-RD"),
    ("SGN-HAM-CAPE+HAM-DE1-RD", "VNSGN", "DE1", "sea", 48, ["CAPE", "DEHAM"], None),
    ("SGN-GDN-CAPE+GDN-PL1-RD", "VNSGN", "PL1", "sea", 49, ["CAPE", "PLGDN"], None),
    ("SGN-DE1-SA", "VNSGN", "DE1", "sea_air", 15, ["AEJEA"], None),
    ("IST-DE1-RD", "TRIST", "DE1", "road", 5, [], None),
    ("TNG-NL1-RD", "MATNG", "NL1", "road", 5, [], None),
)
VN_KNIT_POS = ("PO-VN-0101", "PO-VN-0102")
BD_POS = ("PO-BD-0001", "PO-BD-0002")


def skus() -> list[dict[str, Any]]:
    return [{"code": f"{style}-{size}", "style_code": style, "retail_price_cents": price, "unit_cost_cents": cost}
            for style, _fam, price, cost in STYLES for size in SIZES]


def full_problem(*, weeks: int = 9) -> ProblemData:
    """Full-shaped ProblemData v2 (see the module docstring)."""
    sku_rows = skus()
    knit = [s["code"] for s in sku_rows if s["style_code"].startswith("KNT-")]
    puffer = [s["code"] for s in sku_rows if s["style_code"].startswith("PUF-")]
    week_codes = [f"2026-W{41 + i}" for i in range(weeks)]
    inventory = [{"location_type": "dc", "location_code": dc, "sku": s["code"], "on_hand": 400 + (i * 37) % 600,
                  "allocated": (i * 11) % 60} for dc in DCS for i, s in enumerate(sku_rows)]
    demand = [{"cluster_code": c, "sku": s["code"], "iso_week": w, "units": 2 + (i + j) % 7}
              for i, (c, _dc) in enumerate(CLUSTERS) for j, s in enumerate(sku_rows) for w in week_codes]
    po_lines: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    eta_vn = {"PO-VN-0101": (dt.date(2026, 11, 11), "DE1", "SGN-HAM-SEA+HAM-DE1-RD"),
              "PO-VN-0102": (dt.date(2026, 11, 19), "PL1", "SGN-GDN-SEA+GDN-PL1-RD")}
    for po, (eta, dc, lane) in eta_vn.items():
        for n, sku in enumerate(knit[:12], start=1):
            ref = f"{po}#{n}"
            po_lines.append({"ref": ref, "po_code": po, "supplier_code": "SUP-VN-01", "factory_code": "F-VN-01",
                             "dest_dc": dc, "sku": sku, "qty": 300 + 10 * n, "eta": eta.isoformat(),
                             "ex_factory": (eta - dt.timedelta(days=40)).isoformat(), "mode": "sea", "lane_code": lane})
            receipts.append({"ref": ref, "dc_code": dc, "sku": sku, "qty": 300 + 10 * n, "eta": eta.isoformat()})
    for k, po in enumerate(BD_POS):
        eta = dt.date(2026, 11, 4) + dt.timedelta(days=7 * k)
        dc = ("UK1", "NL1")[k]
        lane = ("CGP-FXT-SEA+FXT-UK1-RD", "CGP-RTM-SEA+RTM-NL1-RD")[k]
        for n, sku in enumerate(puffer[:6], start=1):
            ref = f"{po}#{n}"
            po_lines.append({"ref": ref, "po_code": po, "supplier_code": "CMT-BD-01", "factory_code": "F-BD-01",
                             "dest_dc": dc, "sku": sku, "qty": 500, "eta": eta.isoformat(),
                             "ex_factory": (eta - dt.timedelta(days=35)).isoformat(), "mode": "sea", "lane_code": lane})
            receipts.append({"ref": ref, "dc_code": dc, "sku": sku, "qty": 500, "eta": eta.isoformat()})
    bom = [{"style_code": f"PUF-{i:02d}", "component_code": "CMP-ZIP-01", "supplier_code": "TRIM-BD-01",
            "factory_code": None} for i in range(1, 16)]
    bom += [{"style_code": f"KNT-{i:02d}", "component_code": "CMP-YARN-01", "supplier_code": "SUP-VN-01",
             "factory_code": "F-VN-01"} for i in range(1, 16)]
    locations = [{"code": dc, "kind": "dc", "name": f"DC {dc}", "lon_e6": int(lon * 1e6), "lat_e6": int(lat * 1e6)}
                 for dc, (lon, lat) in DC_POINTS.items()]
    locations += [{"code": code, "kind": "port", "name": name, "lon_e6": int(lon * 1e6), "lat_e6": int(lat * 1e6)}
                  for code, name, lon, lat in PORT_ROWS]
    locations += [{"code": f, "kind": "factory", "name": f"Factory {f}", "lon_e6": int(lon * 1e6),
                   "lat_e6": int(lat * 1e6), "parent": sup} for f, sup, lon, lat in FACTORIES]
    inbound = [{"code": c, "origin": o, "dest_dc": d, "mode": m, "transit_days": t, "cost_per_unit_cents": 40 + t,
                "weekly_capacity_units": 20000, "via": via, "alt_lane_code": alt}
               for c, o, d, m, t, via, alt in INBOUND]
    alt_sources = [{"supplier_code": "CMT-TR-01", "factory_code": "F-TR-01", "style_codes": [f"KNT-{i:02d}" for i in range(1, 16)],
                    "lead_time_days": 21, "unit_cost_cents": 1100, "moq_units": 300, "weekly_capacity_units": 3000,
                    "approved": True, "compliance_ok": True, "lane_code": "IST-DE1-RD"}]
    return ProblemData.model_validate({
        "tenant": TENANT, "as_of": AS_OF.isoformat(), "horizon": {"start_week": "2026-W41", "weeks": weeks},
        "dcs": [{"code": dc} for dc in DCS], "clusters": [{"code": c, "dc_code": dc} for c, dc in CLUSTERS],
        "skus": sku_rows, "inventory": inventory, "receipts": receipts, "demand": demand,
        "lanes": [{"from_dc": "DE1", "to_dc": "NL1", "transit_days": 2, "cost_per_unit_cents": 35, "max_units_wk": 20000},
                  {"from_dc": "PL1", "to_dc": "DE1", "transit_days": 2, "cost_per_unit_cents": 30, "max_units_wk": 15000}],
        "locations": locations, "po_lines": po_lines, "bom": bom, "inbound_lanes": inbound, "alt_sources": alt_sources,
    })


def po_line_dicts(problem: ProblemData) -> list[dict[str, Any]]:
    """The ``bundle.po_lines`` shape (``{po_code, line_no, factory_code, …}``)."""
    return [{"po_code": pl.po_code, "line_no": int(pl.ref.rsplit("#", 1)[1]), "factory_code": pl.factory_code,
             "supplier_code": pl.supplier_code} for pl in problem.po_lines]


ENTITIES = {
    ("supplier", "TRIM-BD-01"): "Meghna Trims",
    ("supplier", "SUP-VN-01"): "Saigon Knit Co.",
    ("factory", "F-VN-01"): "Saigon Knit factory",
    ("po", "PO-BD-0001"): "PO-BD-0001",
    ("style", "KNT-03"): "Knit crew KNT-03",
    ("dc", "UK1"): "Thurrock",
}


class FakeCaller:
    """``ToolCaller`` over a tiny entity table; records every call."""

    def __init__(self, bom_lines: Mapping[str, Sequence[str]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bom_lines = dict(bom_lines or {})

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool == "get_entity":
            key = (args["type"], args["code"])
            if key not in ENTITIES:
                raise McpToolError("not_found: no such entity")
            return {"type": key[0], "code": key[1], "data": {"name": ENTITIES[key]}}
        if tool == "search_entities":
            q = str(args["query"]).lower()
            items = [{"type": t, "code": c, "label": n} for (t, c), n in sorted(ENTITIES.items())
                     if t in (args.get("types") or [t]) and (q in n.lower() or q in c.lower())]
            return {"items": items[: int(args.get("limit") or 5)]}
        if tool == "get_bom_where_used":
            refs = self.bom_lines.get(str(args.get("supplier_code")), [])
            return {"open_po_lines": [{"po_code": r.split("#")[0], "line_no": int(r.split("#")[1])} for r in refs]}
        raise AssertionError(f"unexpected tool {tool}")


def spec(**overrides: Any) -> AgentSpec:
    return AgentSpec(tenant=TENANT, planning=PlanningDefaults(as_of=AS_OF), **overrides)


def run_ctx(core: FakeCore | None = None, *, case_mode: str = "actual", run_id: str | None = None,
            agent_spec: AgentSpec | None = None, case_id: str = "case-1") -> RunContext:
    core = core or FakeCore()
    if case_id not in core.repo.cases:
        core.repo.add_case(case_id, mode=case_mode)
    rid = run_id or str(uuid.uuid4())
    return RunContext(
        tenant=TENANT, case_id=case_id, run_id=rid, kind="chat", actor=Actor.model_validate(PRIYA), payload={},
        spec=agent_spec or spec(), deps=core.deps(), invocation_id=f"e-{rid}", case_title="Case",
        case_mode=case_mode, history=[],
    )


async def run_node(node: Any, node_input: Any, run: RunContext, *, name: str = "node",
                   state: Mapping[str, Any] | None = None) -> tuple[list[Any], Any, dict[str, Any]]:
    """Drive one node (an async-generator function or a FunctionNode) in a one-node workflow.

    Returns ``(events, final output, final session state)``; the final output is the last event
    carrying ``output`` that is not a partial UI event."""
    fn = node if isinstance(node, FunctionNode) else FunctionNode(func=node, name=name)
    wf = Workflow(name="m4", edges=[(START, fn)])
    sessions = InMemorySessionService()
    runner = Runner(app=App(name="t_m4", root_agent=wf), session_service=sessions)
    session_id = str(uuid.uuid4())
    await sessions.create_session(app_name="t_m4", user_id="u", session_id=session_id, state=dict(state or {}))
    text = node_input if isinstance(node_input, str) else json.dumps(node_input)
    events: list[Any] = []
    output: Any = None
    with bind_run(run):
        async for event in runner.run_async(user_id="u", session_id=session_id,
                                            new_message=types.Content(role="user", parts=[types.Part(text=text)])):
            assert not event.error_code, f"{event.error_code}: {event.error_message}"
            events.append(event)
            if event.output is not None and not event.partial:
                output = event.output
    final = await sessions.get_session(app_name="t_m4", user_id="u", session_id=session_id)
    await runner.close()
    return events, output, dict(final.state if final else {})


def routes(events: Sequence[Any]) -> list[str]:
    """Routes the node emitted (``Event(route=…)`` lands in ``event.actions.route``)."""
    out = []
    for e in events:
        route = getattr(getattr(e, "actions", None), "route", None)
        if route:
            out.append(str(route))
    return out


def progress_labels(events: Sequence[Any]) -> list[str]:
    return [e.custom_metadata["sc_progress"]["label"] for e in events
            if e.partial and e.custom_metadata and "sc_progress" in e.custom_metadata]


class TaskRepo(FakeRepo):
    """FakeRepo + ``list_investigation_tasks`` (the M4 read the dirty set uses) + ``latest_evidence``."""

    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[dict[str, Any]] = []

    async def list_investigation_tasks(self, *, tenant: str, case_id: str) -> list[dict[str, Any]]:
        return [dict(t) for t in self.tasks]

    async def latest_evidence(self, *, tenant: str, case_id: str, tool: str) -> dict[str, Any] | None:
        rows = [e for e in self.evidence.values() if e["tool"] == tool and e["case_id"] == case_id]
        return dict(rows[-1]) if rows else None
