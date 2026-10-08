"""Disruption taxonomy (``docs/contracts/90-m4-investigation.md`` §1): loader + validator.

``data/taxonomy/events.yaml`` maps every event type key of the intake enum
(:data:`keelson_contracts.common.EVENT_TYPE_KEYS`) to perturbation *templates* with priors
(p10/p50/p90), a duration prior and a time-to-recover (TTR) prior. ``frame_case`` compiles a
:class:`~keelson_contracts.scenario.DisruptionScenario` from the template (no scenario-specific
code; see :mod:`.framing`).

The file is platform data, so the loader **fails fast**: an unknown key anywhere, an event type
missing from or unknown to the contract enum, a family that does not match the key, a band with
``p10 > p50 > p90``, a value parameter that does not fit the op or a target kind outside the
selector kinds raise :class:`TaxonomyError` (a :class:`~keelson_engine.runtime.errors.RuntimeConfigError`).
Call :func:`check_taxonomy` at worker start-up; :func:`get_taxonomy` loads lazily (cached).

Look-up order: ``$TAXONOMY_DIR``, ``/app/taxonomy``, then ``<source tree>/data/taxonomy`` (the
same pattern as ``runtime/geo.py``; the image must copy ``data/taxonomy`` to ``/app/taxonomy``).

Two small deterministic dictionaries used by mention resolution (``framing.resolve_mentions``)
live here as well, so every piece of framing data is loaded and checked in one place:

* :class:`Gazetteer` (``data/geo/regions.json``): countries (ISO 3166-1 alpha-2 / alpha-3, names,
  aliases, demonyms) and multi-country regions ("EU", "South Asia") → member countries. A region
  mention resolves to the suppliers, factories, ports and DCs located there in the snapshot.
* :class:`Synonyms` (``data/taxonomy/synonyms.yaml``): material terms ("wadding", "zip") with
  aliases and conventional code segments, so "wadding and zips" resolves to component items.
"""

from __future__ import annotations

import functools
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import yaml
from keelson_contracts.common import EVENT_TYPE_KEYS, EventFamily, event_family_of
from keelson_contracts.llm import EntityMentionType
from keelson_contracts.scenario import SelectorKind
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..errors import RuntimeConfigError
from ..geo import normalize_name

__all__ = [
    "ENTITY_TYPE_GUIDANCE",
    "GAZETTEER_FILE",
    "OP_PARAM",
    "SYNONYMS_FILE",
    "TAXONOMY_FILE",
    "Band",
    "EventTemplate",
    "Gazetteer",
    "GeoArea",
    "Material",
    "PerturbationTemplate",
    "Taxonomy",
    "Synonyms",
    "TaxonomyError",
    "ValueSpec",
    "check_taxonomy",
    "get_gazetteer",
    "get_synonyms",
    "get_taxonomy",
    "intake_entity_types_block",
    "intake_event_types_block",
    "load_gazetteer",
    "load_synonyms",
    "load_taxonomy",
    "normalize_term",
    "parse_gazetteer",
    "parse_synonyms",
    "set_gazetteer",
    "set_synonyms",
    "set_taxonomy",
    "singular",
    "taxonomy_dirs",
]

TAXONOMY_FILE: Final = "events.yaml"

TemplateOp = Literal["capacity_scale", "lead_time_shift", "demand_scale"]
"""Ops a taxonomy template may emit (the scenario primitives the optimizer simulates)."""
ValueParam = Literal["delay_days", "factor_bp", "demand_change_pct"]
OP_PARAM: Final[Mapping[str, str]] = {
    "lead_time_shift": "delay_days",
    "capacity_scale": "factor_bp",
    "demand_scale": "demand_change_pct",
}
"""The value parameter each op carries (its primary / p50 value)."""
_PARAM_MIN: Final[Mapping[str, int]] = {"delay_days": 0, "factor_bp": 0, "demand_change_pct": -100}


class TaxonomyError(RuntimeConfigError):
    """The taxonomy file is missing or invalid (fail fast)."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Band(_Strict):
    """A p10/p50/p90 prior (same unit as the value it describes)."""

    p10: int
    p50: int
    p90: int

    @model_validator(mode="after")
    def _ordered(self) -> Band:
        if not self.p10 <= self.p50 <= self.p90:
            raise ValueError(f"band must satisfy p10 <= p50 <= p90 (got {self.p10}/{self.p50}/{self.p90})")
        return self

    def with_p50(self, p50: int) -> Band:
        """The band re-centred on a tenant p50 (p10/p90 widened so the order holds)."""
        return Band(p10=min(self.p10, p50), p50=p50, p90=max(self.p90, p50))


class ValueSpec(_Strict):
    """The primary value of a template: a ``prior`` band (an assumption) or a ``fixed`` value
    that defines the event itself (e.g. a closure is ``factor_bp: 0``)."""

    param: ValueParam
    prior: Band | None = None
    fixed: int | None = None

    @model_validator(mode="after")
    def _one(self) -> ValueSpec:
        if (self.prior is None) == (self.fixed is None):
            raise ValueError("value needs exactly one of prior or fixed")
        low = _PARAM_MIN[self.param]
        lowest = self.fixed if self.fixed is not None else self.prior.p10  # type: ignore[union-attr]
        if lowest < low:
            raise ValueError(f"{self.param} must be >= {low} (got {lowest})")
        return self


class PerturbationTemplate(_Strict):
    op: TemplateOp
    target_kinds: Annotated[tuple[SelectorKind, ...], Field(min_length=1)]
    resolve: Literal["direct", "receipts"] = "direct"
    default_scope: Literal["none", "all_clusters"] = "none"
    value: ValueSpec
    windowed: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> PerturbationTemplate:
        if len(set(self.target_kinds)) != len(self.target_kinds):
            raise ValueError("target_kinds must be unique")
        if self.value.param != OP_PARAM[self.op]:
            raise ValueError(f"{self.op} carries {OP_PARAM[self.op]}, not {self.value.param}")
        if self.resolve == "receipts" and self.op != "lead_time_shift":
            raise ValueError("resolve: receipts is only valid for lead_time_shift")
        if self.default_scope == "all_clusters" and self.op != "demand_scale":
            raise ValueError("default_scope: all_clusters is only valid for demand_scale")
        return self


class EventTemplate(_Strict):
    family: EventFamily
    label: Annotated[str, Field(min_length=1, max_length=80)]
    description: str
    mention_kinds: tuple[EntityMentionType, ...] = ()
    perturbations: tuple[PerturbationTemplate, ...] = ()
    duration_days: Band | None = None
    ttr_days: Band
    resolution: str | None = None

    @model_validator(mode="after")
    def _durations(self) -> EventTemplate:
        if self.duration_days is not None and self.duration_days.p10 < 1:
            raise ValueError("duration_days p10 must be >= 1")
        if self.ttr_days.p10 < 0:
            raise ValueError("ttr_days must be >= 0")
        return self

    @property
    def has_duration(self) -> bool:
        return self.duration_days is not None

    def params(self) -> set[str]:
        """Value parameters this event type uses."""
        return {p.value.param for p in self.perturbations}


class Taxonomy(_Strict):
    version: Annotated[int, Field(ge=1)]
    event_types: dict[str, EventTemplate]

    @model_validator(mode="after")
    def _matches_contract(self) -> Taxonomy:
        keys = set(self.event_types)
        expected = set(EVENT_TYPE_KEYS)
        if missing := sorted(expected - keys):
            raise ValueError(f"event types missing from the taxonomy: {missing}")
        if unknown := sorted(keys - expected):
            raise ValueError(f"event types not in the contract enum (EVENT_TYPE_KEYS): {unknown}")
        for key, tpl in self.event_types.items():
            if tpl.family != event_family_of(key):
                raise ValueError(f"{key}: family {tpl.family} does not match the key")
        if self.event_types["OTHER"].perturbations:
            raise ValueError("OTHER must not carry perturbations (investigation-only)")
        return self

    def template(self, event_type: str) -> EventTemplate:
        try:
            return self.event_types[event_type]
        except KeyError:
            raise TaxonomyError(f"unknown taxonomy event type {event_type!r}") from None

    def ordered(self) -> list[tuple[str, EventTemplate]]:
        """Event types in contract (enum) order."""
        return [(k, self.event_types[k]) for k in EVENT_TYPE_KEYS]


def parse_taxonomy(doc: Any, *, source: str = "<memory>") -> Taxonomy:
    """Validate a parsed taxonomy document (raises :class:`TaxonomyError`)."""
    if not isinstance(doc, Mapping):
        raise TaxonomyError(f"{source}: the taxonomy must be a mapping")
    try:
        return Taxonomy.model_validate(_lists_to_tuples(doc))
    except ValidationError as exc:
        raise TaxonomyError(f"{source}: invalid taxonomy: {exc}") from exc


def _lists_to_tuples(value: Any) -> Any:
    """YAML lists → tuples (the models are strict and frozen)."""
    if isinstance(value, Mapping):
        return {k: _lists_to_tuples(v) for k, v in value.items()}
    if isinstance(value, list):
        return tuple(_lists_to_tuples(v) for v in value)
    return value


def taxonomy_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("TAXONOMY_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(Path("/app/taxonomy"))
    for parent in Path(__file__).resolve().parents:
        dirs.append(parent / "data" / "taxonomy")
    return dirs


def load_taxonomy(path: Path | str | None = None) -> Taxonomy:
    """Load and validate ``events.yaml`` (``path`` or the first file found in :func:`taxonomy_dirs`)."""
    if path is None:
        found = next((d / TAXONOMY_FILE for d in taxonomy_dirs() if (d / TAXONOMY_FILE).is_file()), None)
        if found is None:
            raise TaxonomyError(f"taxonomy file {TAXONOMY_FILE} not found (searched {[str(d) for d in taxonomy_dirs()[:3]]})")
        path = found
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TaxonomyError(f"cannot read taxonomy {path}: {exc}") from exc
    return parse_taxonomy(doc, source=str(path))


@functools.cache
def _default() -> Taxonomy:
    return load_taxonomy()


_override: Taxonomy | None = None


def get_taxonomy() -> Taxonomy:
    return _override if _override is not None else _default()


def set_taxonomy(taxonomy: Taxonomy | None) -> None:
    """Install a taxonomy (tests / tenant packs later); ``None`` restores the file-backed default."""
    global _override
    _override = taxonomy




# ─────────────────────────── gazetteer (regions) ───────────────────────────

GAZETTEER_FILE: Final = "regions.json"
_SHORT = 3
"""Codes and aliases this short only match exactly, or in capitals inside a phrase ("UK DCs")."""
_CAPS_RE = re.compile(r"(?<![A-Za-z])[A-Z]{2,3}(?![A-Za-z])")


@dataclass(frozen=True)
class GeoArea:
    """A resolved region mention: one country (``code`` = ISO 3166-1 alpha-2) or a multi-country
    region (``code`` = the gazetteer key, e.g. ``EU``) with its member countries."""

    code: str
    name: str
    countries: tuple[str, ...]

    @property
    def is_country(self) -> bool:
        return self.countries == (self.code,)


@dataclass(frozen=True)
class Gazetteer:
    """Countries and multi-country regions (``data/geo/regions.json``), resolved deterministically."""

    areas: Mapping[str, GeoArea]
    names: Mapping[str, str]
    """normalised name / alias → area code (matched exactly, or as a phrase when longer than 3)."""
    codes: Mapping[str, str]
    """ISO-2 / ISO-3 / region code (upper case) → area code (exact, written in capitals)."""
    shorts: Mapping[str, str]
    """short aliases (≤ 3 letters, upper case without spaces, e.g. ``UK``) → area code."""

    @classmethod
    def empty(cls) -> Gazetteer:
        return cls(areas={}, names={}, codes={}, shorts={})

    def area(self, code: str) -> GeoArea | None:
        return self.areas.get(code)

    def countries_of(self, code: str) -> tuple[str, ...]:
        """Member countries of an area code (an ISO-2 code outside the gazetteer is itself)."""
        hit = self.areas.get(code)
        if hit is not None:
            return hit.countries
        return (code,) if re.fullmatch(r"[A-Z]{2}", code or "") else ()

    def country_code(self, value: Any) -> str | None:
        """A location's ``country`` value (ISO-2, ISO-3, a name or an alias such as ``UK``) as ISO-2."""
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip()
        up = raw.upper()
        code = (self.codes.get(up) or self.shorts.get(re.sub(r"[^A-Z]", "", up))
                or self.names.get(normalize_name(raw)))
        area = self.areas.get(code) if code else None
        if area is not None and area.is_country:
            return area.code
        return up if re.fullmatch(r"[A-Z]{2}", up) else None

    def resolve(self, text: str) -> GeoArea | None:
        """The country or region a mention names, or ``None``.

        1. the whole mention is a name / alias (case and accents ignored), or a code written in
           capitals (``VN``, ``BGD``, ``EU``);
        2. else a name / alias longer than three letters as a whole-word phrase inside the mention,
           or a short alias written in capitals ("the Bangladesh POs", "UK DCs"); the longest
           phrase wins, ties by code. ISO codes never match inside a phrase ("IT systems" is not
           Italy) and lower-case short words never do ("us" is not the United States)."""
        raw = (text or "").strip()
        exact = self._exact(raw)
        if exact is not None or not normalize_name(raw):
            return exact
        padded = f" {normalize_name(raw)} "
        best: tuple[int, str] | None = None
        for key, code in sorted(self.names.items()):
            if len(key) > _SHORT and f" {key} " in padded and (
                    best is None or len(key) > best[0] or (len(key) == best[0] and code < best[1])):
                best = (len(key), code)
        if best is None:
            for token in _CAPS_RE.findall(raw):
                short = self.shorts.get(token)
                if short is not None and (best is None or short < best[1]):
                    best = (len(token), short)
        return self.areas[best[1]] if best else None

    def _exact(self, raw: str) -> GeoArea | None:
        norm = normalize_name(raw)
        if not norm:
            return None
        if norm in self.names:
            return self.areas[self.names[norm]]
        if raw.isupper() and (code := self.codes.get(re.sub(r"[\s-]+", "_", raw))):
            return self.areas[code]
        return None

    def resolve_all(self, text: str, *, phrase: bool = True) -> list[GeoArea]:
        """Every country or region a mention names, in order of appearance ("Vietnam and Cambodia"
        → VN, KH; "Eastern Europe and Türkiye" → CEE, TR). The rules of :meth:`resolve`; each phrase
        is consumed once, longest first, so "South Asia" is not also "Asia". ``phrase=False``: only
        a mention that *is* a place (the whole text is a name, an alias or a code in capitals)."""
        raw = (text or "").strip()
        exact = self._exact(raw)
        if exact is not None:
            return [exact]
        norm = normalize_name(raw)
        if not phrase or not norm:
            return []
        rest = f" {norm} "
        hits: list[tuple[int, str]] = []
        for key, code in sorted(self.names.items(), key=lambda kc: (-len(kc[0]), kc[1], kc[0])):
            if len(key) <= _SHORT:
                continue
            needle = f" {key} "
            while (pos := rest.find(needle)) >= 0:
                hits.append((pos, code))
                rest = rest[:pos + 1] + " " * (len(needle) - 2) + rest[pos + len(needle) - 1:]
        out = [code for _pos, code in sorted(hits)]
        out += [self.shorts[t] for t in _CAPS_RE.findall(raw) if t in self.shorts]
        return [self.areas[c] for c in dict.fromkeys(out)]


class _CountryDoc(_Strict):
    name: Annotated[str, Field(min_length=1)]
    iso3: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    aliases: tuple[str, ...] = ()


class _RegionDoc(_Strict):
    name: Annotated[str, Field(min_length=1)]
    aliases: tuple[str, ...] = ()
    countries: Annotated[tuple[Annotated[str, Field(pattern=r"^[A-Z]{2}$")], ...], Field(min_length=1)]


class _GazetteerDoc(_Strict):
    version: Annotated[int, Field(ge=1)]
    description: str = ""
    countries: dict[Annotated[str, Field(pattern=r"^[A-Z]{2}$")], _CountryDoc]
    regions: dict[Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]+$")], _RegionDoc] = Field(default_factory=dict)


def parse_gazetteer(doc: Any, *, source: str = "<memory>") -> Gazetteer:
    """Validate ``regions.json`` (raises :class:`TaxonomyError`): codes well-formed, region members
    known, and no name, alias or code claimed by two areas (a mention must resolve one way)."""
    if not isinstance(doc, Mapping):
        raise TaxonomyError(f"{source}: the gazetteer must be a mapping")
    try:
        parsed = _GazetteerDoc.model_validate(_lists_to_tuples(doc))
    except ValidationError as exc:
        raise TaxonomyError(f"{source}: invalid gazetteer: {exc}") from exc
    areas: dict[str, GeoArea] = {}
    names: dict[str, str] = {}
    codes: dict[str, str] = {}
    shorts: dict[str, str] = {}

    def claim(table: dict[str, str], key: str, code: str) -> None:
        held = table.get(key)
        if held is not None and held != code:
            raise TaxonomyError(f"{source}: {key!r} names both {held} and {code}")
        table[key] = code

    for iso2, country in sorted(parsed.countries.items()):
        areas[iso2] = GeoArea(code=iso2, name=country.name, countries=(iso2,))
        claim(codes, iso2, iso2)
        claim(codes, country.iso3, iso2)
    for code, region in sorted(parsed.regions.items()):
        if code in codes:
            raise TaxonomyError(f"{source}: region code {code} is also a country code")
        if unknown := sorted(set(region.countries) - set(parsed.countries)):
            raise TaxonomyError(f"{source}: region {code} lists unknown countries {unknown}")
        areas[code] = GeoArea(code=code, name=region.name, countries=tuple(sorted(set(region.countries))))
        claim(codes, code, code)
    entries: list[tuple[str, Any]] = [*sorted(parsed.countries.items()), *sorted(parsed.regions.items())]
    for code, entry in entries:
        for alias in (entry.name, *entry.aliases):
            key = normalize_name(alias)
            if not key:
                continue
            claim(names, key, code)
            compact = key.replace(" ", "").upper()
            if len(compact) <= _SHORT and compact.isalpha():
                claim(shorts, compact, code)
    return Gazetteer(areas=areas, names=names, codes=codes, shorts=shorts)


def _geo_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("GEO_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(Path("/app/geo"))
    for parent in Path(__file__).resolve().parents:
        dirs.append(parent / "data" / "geo")
    return dirs


def load_gazetteer(path: Path | str | None = None) -> Gazetteer:
    """Load and validate ``regions.json`` (``path``, else ``$GEO_DIR``, ``/app/geo``, ``<source tree>/data/geo``)."""
    if path is None:
        found = next((d / GAZETTEER_FILE for d in _geo_dirs() if (d / GAZETTEER_FILE).is_file()), None)
        if found is None:
            raise TaxonomyError(f"gazetteer {GAZETTEER_FILE} not found (searched {[str(d) for d in _geo_dirs()[:3]]})")
        path = found
    path = Path(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TaxonomyError(f"cannot read gazetteer {path}: {exc}") from exc
    return parse_gazetteer(doc, source=str(path))


@functools.cache
def _default_gazetteer() -> Gazetteer:
    return load_gazetteer()


_gazetteer_override: Gazetteer | None = None


def get_gazetteer() -> Gazetteer:
    return _gazetteer_override if _gazetteer_override is not None else _default_gazetteer()


def set_gazetteer(gazetteer: Gazetteer | None) -> None:
    """Install a gazetteer (tests); ``None`` restores the file-backed default."""
    global _gazetteer_override
    _gazetteer_override = gazetteer


# ─────────────────────────── material synonyms ───────────────────────────

SYNONYMS_FILE: Final = "synonyms.yaml"
_KEEP_S = ("ss", "us", "is")


def singular(word: str) -> str:
    """Simple English singular of one lower-case word ("zips" → "zip", "batteries" → "battery",
    "boxes" → "box"); words of three letters or fewer and "-ss/-us/-is" words stay as they are."""
    if len(word) <= _SHORT:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("sses", "shes", "ches", "xes", "zzes")):
        return word[:-2]
    if word.endswith("s") and not word.endswith(_KEEP_S):
        return word[:-1]
    return word


def normalize_term(text: str) -> str:
    """:func:`normalize_name` with every word singular ("Two-way Zips" → "two way zip")."""
    return " ".join(singular(w) for w in normalize_name(text).split())


@dataclass(frozen=True)
class Material:
    term: str
    keys: tuple[str, ...]
    """The term and its aliases, normalised (:func:`normalize_term`)."""
    code_tokens: tuple[str, ...]


class _MaterialDoc(_Strict):
    aliases: tuple[str, ...] = ()
    code_tokens: tuple[Annotated[str, Field(pattern=r"^[A-Z0-9]{2,8}$")], ...] = ()


class _SynonymsDoc(_Strict):
    version: Annotated[int, Field(ge=1)]
    materials: dict[str, _MaterialDoc]


@dataclass(frozen=True)
class Synonyms:
    """Material terms → aliases + conventional item-code segments (``data/taxonomy/synonyms.yaml``)."""

    materials: tuple[Material, ...] = ()

    def find(self, text: str) -> list[Material]:
        """Materials named in a mention, longest phrase first; each phrase is consumed once, so
        "shell fabric" is one material, not also "fabric" ("wadding and zips" → wadding, zip)."""
        rest = f" {normalize_term(text)} "
        keyed = sorted(((k, m) for m in self.materials for k in m.keys), key=lambda km: (-len(km[0]), km[0]))
        found: list[Material] = []
        for key, material in keyed:
            if f" {key} " in rest:
                rest = rest.replace(f" {key} ", "  ")
                if material not in found:
                    found.append(material)
        return found


def parse_synonyms(doc: Any, *, source: str = "<memory>") -> Synonyms:
    """Validate ``synonyms.yaml`` (raises :class:`TaxonomyError`); a term or alias may belong to one
    material only."""
    if not isinstance(doc, Mapping):
        raise TaxonomyError(f"{source}: the synonyms file must be a mapping")
    try:
        parsed = _SynonymsDoc.model_validate(_lists_to_tuples(doc))
    except ValidationError as exc:
        raise TaxonomyError(f"{source}: invalid synonyms: {exc}") from exc
    owner: dict[str, str] = {}
    materials: list[Material] = []
    for term, entry in sorted(parsed.materials.items()):
        keys = tuple(dict.fromkeys(k for k in (normalize_term(term), *(normalize_term(a) for a in entry.aliases)) if k))
        if not keys:
            raise TaxonomyError(f"{source}: material {term!r} has no usable name")
        for key in keys:
            if owner.setdefault(key, term) != term:
                raise TaxonomyError(f"{source}: {key!r} is listed under both {owner[key]!r} and {term!r}")
        materials.append(Material(term=keys[0], keys=keys, code_tokens=entry.code_tokens))
    return Synonyms(materials=tuple(materials))


def load_synonyms(path: Path | str | None = None) -> Synonyms:
    """Load and validate ``synonyms.yaml`` (``path`` or the first file found in :func:`taxonomy_dirs`)."""
    if path is None:
        found = next((d / SYNONYMS_FILE for d in taxonomy_dirs() if (d / SYNONYMS_FILE).is_file()), None)
        if found is None:
            raise TaxonomyError(f"synonyms file {SYNONYMS_FILE} not found "
                                f"(searched {[str(d) for d in taxonomy_dirs()[:3]]})")
        path = found
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TaxonomyError(f"cannot read synonyms {path}: {exc}") from exc
    return parse_synonyms(doc, source=str(path))


@functools.cache
def _default_synonyms() -> Synonyms:
    return load_synonyms()


_synonyms_override: Synonyms | None = None


def get_synonyms() -> Synonyms:
    return _synonyms_override if _synonyms_override is not None else _default_synonyms()


def set_synonyms(synonyms: Synonyms | None) -> None:
    """Install material synonyms (tests); ``None`` restores the file-backed default."""
    global _synonyms_override
    _synonyms_override = synonyms


# ─────────────────────────── start-up check + intake prompt ───────────────────────────


def check_taxonomy() -> Taxonomy:
    """Start-up check: the taxonomy loads and matches the contract enum, and the chokepoint
    catalogue (``data/geo/chokepoints.json``), the gazetteer (``data/geo/regions.json``) and the
    material synonyms (``data/taxonomy/synonyms.yaml``) parse. Raises :class:`TaxonomyError`."""
    from ..geo import get_geo  # noqa: PLC0415

    taxonomy = get_taxonomy()
    try:
        geo = get_geo()
    except ValueError as exc:
        raise TaxonomyError(f"invalid chokepoint catalogue: {exc}") from exc
    if not geo.chokepoints:
        raise TaxonomyError("chokepoint catalogue (data/geo/chokepoints.json) not found or empty")
    if not get_gazetteer().areas:
        raise TaxonomyError("gazetteer (data/geo/regions.json) is empty")
    get_synonyms()
    return taxonomy


ENTITY_TYPE_GUIDANCE: Final[tuple[tuple[str, str], ...]] = (
    ("supplier", "a supplier or vendor, by name or code"),
    ("factory", "a factory, mill or production unit"),
    ("component", "a material or component (fabric, wadding, zips, trims, labels) as written, even without a code; "
                  "one mention per material"),
    ("po", "a purchase order number"),
    ("sku", "a SKU code"),
    ("style", "a style, product or range"),
    ("dc", "a distribution centre or warehouse"),
    ("store", "a store"),
    ("port", "a seaport or airport, by name or UN/LOCODE (Hamburg, BDCGP)"),
    ("chokepoint", "a canal, strait or sea passage (Suez Canal, Red Sea, Strait of Hormuz)"),
    ("region", "a country or multi-country region where things are located or sourced, including when it "
               "qualifies something else (Bangladesh in 'the Bangladesh POs', Vietnam, Türkiye, UK, EU, South "
               "Asia); one mention per region; not a place word that only describes a product (Egyptian cotton, "
               "French terry)"),
    ("lane", "a shipping lane code"),
    ("other", "anything else that names a place or thing"),
)
"""Mention types for the intake prompt with one line of guidance each (every ``EntityMentionType``
must be listed; checked by the tests)."""


def intake_event_types_block(taxonomy: Taxonomy | None = None) -> str:
    """One line per event type for the intake instruction (``prompts.intake_instruction``):
    ``<key>: <label> - <description>`` plus the mention types it uses, in contract order."""
    tax = taxonomy or get_taxonomy()
    lines = []
    for key, tpl in tax.ordered():
        kinds = f" (mentions: {', '.join(tpl.mention_kinds)})" if tpl.mention_kinds else ""
        lines.append(f"- {key}: {tpl.label} - {tpl.description}{kinds}")
    return "\n".join(lines)


def intake_entity_types_block() -> str:
    """The entity-mention types for the intake instruction, one guidance line each."""
    return "Entity mention types:\n" + "\n".join(f"- {kind}: {text}" for kind, text in ENTITY_TYPE_GUIDANCE)
