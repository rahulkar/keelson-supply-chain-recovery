"""Impact graph (``/v1/impact`` with ``include_graph``; ``impact_graph@1`` block data).

Grouped network, upstream to downstream (PO lines are grouped to POs, SKUs to styles,
stores never appear - the model is cluster level)::

    supplier -supplies-> component -supplies-> factory      (BOM; null factory = all)
    supplier -supplies-> factory                             (the PO line's own supplier)
    factory  -produces-> po -ships-> lane -ships-> dc        (po -ships-> dc without a lane)
    port/chokepoint -ships-> lane                            (lane origin and via points)
    dc -stocks-> style,  dc -serves-> cluster,  style -serves-> cluster

Status (most severe first):

* ``disrupted`` - directly targeted by a perturbation (PO lines / receipts mark their PO,
  SKUs their style);
* ``impacted`` - loses units: a DC / cluster / style with lost sales, or an upstream node
  carrying a disrupted receipt whose (DC, style) loses sales;
* ``at_risk`` - on a disrupted path without lost sales yet: carries disrupted receipts, or
  a protected minimum is breached (``first_breach_week`` set) without lost sales;
* ``ok``.

``units_at_risk``: lost units for DC / cluster / style nodes; disrupted (delayed or lost)
receipt units carried by upstream nodes. ``first_breach_week``: earliest short week of the
node (downstream) or of the (DC, style) pairs its disrupted receipts feed (upstream).

When the network exceeds :data:`MAX_GRAPH_NODES`, nodes are kept by status severity, then
units at risk, kind (upstream first) and id; edges to dropped nodes are removed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from keelson_contracts.optimizer import MAX_GRAPH_NODES, GraphEdge, GraphNode, ImpactGraph, NodeStatus

from keelson_opt.impact import SimResult
from keelson_opt.scn import Scenario

_RANK: dict[str, int] = {"disrupted": 0, "impacted": 1, "at_risk": 2, "ok": 3}
_KIND_ORDER = (
    "supplier",
    "component",
    "factory",
    "port",
    "chokepoint",
    "po",
    "lane",
    "dc",
    "style",
    "cluster",
)


@dataclass(slots=True)
class _Node:
    id: str
    kind: str
    label: str
    disrupted: bool = False
    lost: int = 0
    carried: int = 0
    """Disrupted receipt units through this (upstream) node."""
    feeds_loss: bool = False
    breach: int | None = None
    feeds: set[tuple[str, str]] = field(default_factory=set)


def nid(kind: str, code: str) -> str:
    return f"{kind}:{code}"


def build_graph(scn: Scenario, res: SimResult, max_nodes: int = MAX_GRAPH_NODES) -> tuple[ImpactGraph, list[str]]:
    nodes: dict[str, _Node] = {}
    edges: set[tuple[str, str, str]] = set()
    warnings: list[str] = []

    def node(kind: str, code: str, label: str | None = None) -> _Node:
        key = nid(kind, code)
        n = nodes.get(key)
        if n is None:
            n = nodes[key] = _Node(key, kind, label or code)
        return n

    def edge(src: _Node, dst: _Node, kind: str) -> None:
        if src.id != dst.id:
            edges.add((src.id, dst.id, kind))

    labels: dict[tuple[str, str], str] = {}
    ports: frozenset[str] = frozenset()
    if scn.data is not None:
        labels = {(loc.kind, loc.code): loc.name for loc in scn.data.locations if loc.name}
        ports = frozenset(loc.code for loc in scn.data.locations if loc.kind == "port")

    def lbl(kind: str, code: str) -> str:
        return labels.get((kind, code), code)

    # ── downstream: DCs, clusters, styles (results of the simulation) ──
    lost_cl: dict[str, int] = defaultdict(int)
    lost_style: dict[str, int] = defaultdict(int)
    lost_dc: dict[str, int] = defaultdict(int)
    lost_dc_style: dict[tuple[str, str], int] = defaultdict(int)
    breach_cl: dict[str, int] = {}
    breach_style: dict[str, int] = {}
    breach_dc_style: dict[tuple[str, str], int] = {}
    pairs: set[tuple[str, str]] = set()
    for (cl, sku), cs in res.series.items():
        dc, style = scn.cluster_dc[cl], scn.sku_style[sku]
        lost = sum(cs.lost)
        lost_cl[cl] += lost
        lost_style[style] += lost
        lost_dc[dc] += lost
        lost_dc_style[(dc, style)] += lost
        if any(cs.demand):
            pairs.add((style, cl))
        first = next((t for t, s in enumerate(cs.short) if s), None)
        if first is not None:
            breach_cl[cl] = min(breach_cl.get(cl, first), first)
            breach_style[style] = min(breach_style.get(style, first), first)
            breach_dc_style[(dc, style)] = min(breach_dc_style.get((dc, style), first), first)
    for dc in scn.dcs:
        n = node("dc", dc, lbl("dc", dc))
        n.lost = lost_dc[dc]
        n.breach = min((t for (d, _s), t in breach_dc_style.items() if d == dc), default=None)
        for cl in scn.dc_clusters[dc]:
            c = node("cluster", cl, lbl("cluster", cl))
            c.lost = lost_cl[cl]
            c.breach = breach_cl.get(cl)
            edge(n, c, "serves")
    styles_at_dc: set[tuple[str, str]] = set()
    for style, cl in sorted(pairs):
        s = node("style", style)
        s.lost = lost_style[style]
        s.breach = breach_style.get(style)
        edge(s, nodes[nid("cluster", cl)], "serves")
        styles_at_dc.add((scn.cluster_dc[cl], style))
    for dc, style in sorted(styles_at_dc):
        edge(nodes[nid("dc", dc)], nodes[nid("style", style)], "stocks")

    # ── upstream: POs, factories, suppliers, components, lanes, ports ──
    affected: dict[str, int] = defaultdict(int)
    for rl in scn.receipt_lines:
        if rl.affected:
            affected[rl.ref] += rl.qty
    po_dc: set[tuple[str, str]] = set()
    for rl in scn.receipt_lines:
        po = rl.ref.split("#", 1)[0]
        pol = scn.po_lines.get(rl.ref)
        style = scn.sku_style[rl.sku]
        chain: list[_Node] = [node("po", po)]
        lane = scn.inbound_lanes.get(pol.lane_code) if pol is not None and pol.lane_code else None
        if pol is not None:
            fac = node("factory", pol.factory_code, lbl("factory", pol.factory_code))
            sup = node("supplier", pol.supplier_code, lbl("supplier", pol.supplier_code))
            edge(sup, fac, "supplies")
            edge(fac, chain[0], "produces")
            chain += [fac, sup]
            for comp, csup, cfac in scn.bom_by_style.get(style, ()):
                if cfac is None or cfac == pol.factory_code:
                    cn = node("component", comp)
                    sn = node("supplier", csup, lbl("supplier", csup))
                    edge(sn, cn, "supplies")
                    edge(cn, fac, "supplies")
                    chain += [cn, sn]
        if lane is not None:
            ln = node("lane", lane.code)
            edge(chain[0], ln, "ships")
            edge(ln, nodes[nid("dc", rl.dc)], "ships")
            chain.append(ln)
            for i, point in enumerate((lane.origin, *lane.via)):
                kind = _point_kind(scn, point, ports, origin=i == 0)
                pn = node(kind, point, lbl("port", point))
                edge(pn, ln, "ships")
                chain.append(pn)
        else:
            po_dc.add((po, rl.dc))
        if rl.affected:
            for n in chain:
                n.carried += rl.qty
                n.feeds.add((rl.dc, style))
    for po, dc in sorted(po_dc):
        edge(nodes[nid("po", po)], nodes[nid("dc", dc)], "ships")

    # ── disrupted marks ──
    for kind, ids in scn.disrupted.items():
        for code in ids:
            target = {"po_line": "po", "receipt": "po", "sku": "style"}.get(kind, kind)
            if target == "po" and kind != "po":
                code = code.split("#", 1)[0]
            elif kind == "sku":
                code = scn.sku_style.get(code, code)
            for k in (target,) if target not in ("port", "chokepoint") else ("port", "chokepoint"):
                hit = nodes.get(nid(k, code))
                if hit is not None:
                    hit.disrupted = True

    for n in nodes.values():
        if n.feeds:
            n.feeds_loss = any(lost_dc_style.get(f, 0) > 0 for f in n.feeds)
            breaches = [breach_dc_style[f] for f in n.feeds if f in breach_dc_style]
            n.breach = min(breaches, default=None)

    out_nodes = [_finish(n, scn) for n in nodes.values()]
    if len(out_nodes) > max_nodes:
        out_nodes.sort(key=lambda g: (_RANK[g.status], -g.units_at_risk, _kind_rank(g.kind), g.id))
        warnings.append(f"impact graph truncated to {max_nodes} of {len(out_nodes)} nodes")
        out_nodes = out_nodes[:max_nodes]
    keep = {n.id for n in out_nodes}
    out_nodes.sort(key=lambda g: (_kind_rank(g.kind), g.id))
    out_edges = [
        GraphEdge(source=s, target=t, kind=k)  # type: ignore[arg-type]
        for s, t, k in sorted(edges)
        if s in keep and t in keep
    ]
    return ImpactGraph(nodes=out_nodes, edges=out_edges), warnings


def _kind_rank(kind: str) -> int:
    return _KIND_ORDER.index(kind) if kind in _KIND_ORDER else len(_KIND_ORDER)


def _point_kind(scn: Scenario, code: str, ports: frozenset[str], *, origin: bool) -> str:
    if origin or code in scn.disrupted.get("port", ()):
        return "port"
    if code in scn.disrupted.get("chokepoint", ()):
        return "chokepoint"
    return "port" if code in ports else "chokepoint"


def _finish(n: _Node, scn: Scenario) -> GraphNode:
    downstream = n.kind in ("dc", "cluster", "style")
    status: NodeStatus
    if n.disrupted:
        status = "disrupted"
    elif (downstream and n.lost > 0) or (not downstream and n.carried and n.feeds_loss):
        status = "impacted"
    elif (downstream and n.breach is not None) or (not downstream and n.carried):
        status = "at_risk"
    else:
        status = "ok"
    units = n.lost if downstream else n.carried
    return GraphNode(
        id=n.id,
        kind=n.kind,  # type: ignore[arg-type]
        label=n.label,
        status=status,
        units_at_risk=units,
        first_breach_week=scn.weeks[n.breach] if n.breach is not None else None,
    )
