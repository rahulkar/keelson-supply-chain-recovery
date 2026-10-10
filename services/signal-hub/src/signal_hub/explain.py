"""Membership explanations (``docs/contracts/123`` §5) — one deterministic sentence per decision.

Built **only** from the method, integers, validated place codes, the gazetteer's curated names
(:meth:`~signal_hub.gazetteer.Gazetteer.display_name`) and fixed source labels derived from
connector ids. Never from a headline, snippet, extracted mention or source-supplied name, so an
explanation can be shown anywhere without quarantine (it is not untrusted text).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from keelson_contracts.signals import LOCAL_SIMILARITY_PROFILE, MAX_MEMBERSHIP_EXPLANATION
from signal_hub.gazetteer import Gazetteer

__all__ = ["METHOD_LABELS", "explain", "source_label"]

METHOD_LABELS: Final[dict[str, str]] = {
    "anchor": "official report",
    "same_hazard": "same hazard",
    "founded": "first report",
    "attach_geo": "nearby",
    "attach_ref": "same place",
    "attach_vector": "similar description",
    "near_duplicate": "near-duplicate",
    "absorbed": "absorbed",
    "merged_vector": "merged",
}
_OFFICIAL: Final[dict[str, str]] = {
    "gdacs": "GDACS", "usgs": "USGS",
    # M8 authoritative feeds (124): anchors like GDACS / USGS
    "nhc": "NHC", "copernicus_ems": "Copernicus EMS", "ofac": "OFAC",
}
_MAX_PLACES: Final = 3


def source_label(connector_id: str | None) -> str:
    """A fixed label for an authoritative connector (``gdacs`` / ``demo.gdacs`` → ``GDACS``); ``""`` when
    the connector has none (the sentences then just say "official report")."""
    base = (connector_id or "").removeprefix("demo.")
    return _OFFICIAL.get(base, "")


def _official(connector_id: str | None) -> str:
    """``official GDACS`` (or just ``official`` without a fixed label)."""
    label = source_label(connector_id)
    return f"official {label}" if label else "official"


def _pct(bp: int | None) -> str:
    return f"{((bp or 0) + 50) // 100} %"


def _profile(profile: str | None) -> str:
    if profile in (None, LOCAL_SIMILARITY_PROFILE):
        return "local term profile"
    return f"remote profile {profile}"


def _places(refs: Sequence[str], gaz: Gazetteer | None) -> str:
    shown = []
    for ref in refs[:_MAX_PLACES]:
        name = gaz.display_name(ref) if gaz is not None else None
        shown.append(f"{name} [{ref}]" if name else ref)
    more = len(refs) - len(shown)
    return ", ".join(shown) + (f" and {more} more" if more > 0 else "")


def _within(hours: int) -> str:
    return f"reported within {hours} h of the event"


def explain(
    method: str,
    *,
    gaz: Gazetteer | None = None,
    distance_km: int | None = None,
    shared_refs: Sequence[str] = (),
    similarity_bp: int | None = None,
    similarity_profile: str | None = None,
    hamming_bits: int | None = None,
    prior_method: str | None = None,
    anchor_connector: str | None = None,
    window_hours: int = 72,
    same_hazard_hours: int = 6,
) -> str:
    """The explanation sentence of one membership (<= 400 characters)."""
    if method == "anchor":
        official = _official(anchor_connector)
        text = f"{official[:1].upper()}{official[1:]} report that founded this event."
    elif method == "same_hazard":
        label = source_label(anchor_connector)
        text = (f"Second official report{f' ({label})' if label else ''} of the same hazard, "
                f"{distance_km if distance_km is not None else '?'} km from the first report and starting within "
                f"{same_hazard_hours} h.")
    elif method == "founded":
        text = "First report of this event: no open event matched it when it arrived."
    elif method == "attach_ref":
        text = f"Names the same place as the event: {_places(list(shared_refs), gaz)}; {_within(window_hours)}."
    elif method == "attach_geo":
        text = (f"Located {distance_km if distance_km is not None else '?'} km from the event footprint "
                f"(limit 300 km); {_within(window_hours)}.")
    elif method == "attach_vector":
        where = (f"{distance_km} km from the event footprint" if distance_km is not None
                 else "with no mapped location in common")
        text = (f"Describes a similar disruption (similarity {_pct(similarity_bp)}, {_profile(similarity_profile)}), "
                f"{where}; {_within(window_hours)}; no shared place code.")
    elif method == "near_duplicate":
        text = (f"Near-duplicate of another report in this event ({hamming_bits if hamming_bits is not None else '?'} "
                f"of 64 fingerprint bits differ): counted once for corroboration.")
    elif method == "absorbed":
        text = (f"Moved here when the {_official(anchor_connector)} report of this event arrived; "
                f"first grouped as: {METHOD_LABELS.get(prior_method or '', 'first report')}.")
    elif method == "merged_vector":
        text = (f"Moved here from a similar event (similarity {_pct(similarity_bp)}, {_profile(similarity_profile)}); "
                f"first grouped as: {METHOD_LABELS.get(prior_method or '', 'first report')}.")
    else:
        raise ValueError(f"unknown membership method {method!r}")
    return text[:MAX_MEMBERSHIP_EXPLANATION]
