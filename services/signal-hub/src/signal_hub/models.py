"""Internal models between adapters, the quarantine and the store.

* :class:`SourceContext` — where a parse runs: wall-clock feed poll (``origin feed/news``,
  global) or a demo pack replay (``origin demo``, tenant-scoped, ``provenance simulated``, sim
  clock), or a tenant's connected system.
* :class:`SignalDraft` — a normalized observation before the store assigns its id/version.
* :class:`NewsItemDraft` — untrusted text (headline + snippet, or a supplier message) waiting
  for the guard + extractor.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from keelson_contracts.common import canonical_json
from keelson_contracts.signals import SOURCE_URL_PATTERN, DisruptionSignal

__all__ = [
    "NewsItemDraft",
    "SignalDraft",
    "SourceContext",
    "canonical_url",
    "clip",
    "safe_source_url",
]

_SOURCE_URL_RE: Final = re.compile(SOURCE_URL_PATTERN)
_TRACKING_PARAMS: Final = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid")
_WS_RE: Final = re.compile(r"\s+")


def clip(text: str | None, limit: int) -> str | None:
    """Whitespace-collapsed text cut to ``limit`` characters (with an ellipsis), None when empty."""
    if text is None:
        return None
    cleaned = _WS_RE.sub(" ", text).strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


def canonical_url(url: str | None) -> str | None:
    """Lowercase scheme/host, IDN host → punycode, no fragment/userinfo/tracking parameters.

    Returns None for anything that is not an absolute http(s) URL with a plain host."""
    if not url or not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        return None
    host = parts.hostname.rstrip(".")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    netloc = f"{host}:{port}" if port else host
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith(_TRACKING_PARAMS)])
    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def safe_source_url(url: str | None) -> str | None:
    """The canonical URL when it satisfies the contract's attribution pattern, else None."""
    canon = canonical_url(url)
    if canon is None or len(canon) > 2000 or not _SOURCE_URL_RE.match(canon):
        return None
    return canon


@dataclass(frozen=True, slots=True)
class SourceContext:
    """Parse context. ``pack_id`` set = demo replay (tenant-scoped, simulated)."""

    retrieved_at: dt.datetime
    tenant: str | None = None
    pack_id: str | None = None

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        if self.pack_id is not None and self.tenant is None:
            raise ValueError("demo replays are tenant-scoped")

    @property
    def demo(self) -> bool:
        return self.pack_id is not None

    def connector(self, base: str) -> str:
        return f"demo.{base}" if self.demo else base

    def key(self, native: str) -> str:
        """Dedupe key: tenant/pack-qualified for tenant-scoped data (ids stay unique per tenant)."""
        if self.demo:
            return f"{self.tenant}/{self.pack_id}/{native}"
        if self.tenant is not None:
            return f"{self.tenant}/{native}"
        return native

    def origin(self, default: str) -> str:
        return "demo" if self.demo else default

    def provenance(self, default: str) -> str:
        return "simulated" if self.demo else default

    def source_name(self, name: str) -> str:
        return f"{name} (demo replay)" if self.demo else name


@dataclass(slots=True)
class SignalDraft:
    tenant: str | None
    connector_id: str
    native_key: str
    origin: str
    kind: str
    subtype: str
    title: str
    summary: str | None
    raw_text: str | None
    geometry: dict[str, Any] | None
    location_refs: list[str]
    starts_at: dt.datetime
    ends_at: dt.datetime | None
    observed_at: dt.datetime
    expires_at: dt.datetime | None
    severity_bp: int
    severity_native: dict[str, Any]
    provenance: str
    extracted_by: str | None
    source_name: str
    source_url: str | None
    license: str
    authority_bp: int
    retrieved_at: dt.datetime
    native_id: str
    authoritative: bool = False
    """Anchor-capable (GDACS/USGS): becomes the anchor of its event."""
    pack_id: str | None = None

    def content_hash(self) -> str:
        """Hash of the observable content (not ``retrieved_at``/``observed_at``): a re-poll with
        the same hash is a duplicate; a different hash is a new version."""
        payload = {
            "kind": self.kind, "subtype": self.subtype, "title": self.title, "summary": self.summary,
            "raw_text": self.raw_text, "geometry": self.geometry, "location_refs": self.location_refs,
            "starts_at": self.starts_at, "ends_at": self.ends_at, "expires_at": self.expires_at,
            "severity_bp": self.severity_bp, "severity_native": self.severity_native,
            "provenance": self.provenance, "extracted_by": self.extracted_by, "source_url": self.source_url,
            "native_id": self.native_id, "license": self.license,
        }
        return hashlib.sha256(canonical_json(_jsonable(payload)).encode("utf-8")).hexdigest()

    def to_signal(
        self, *, signal_id: str, version: int, supersedes: str | None, confidence_bp: int, event_id: str | None
    ) -> DisruptionSignal:
        return DisruptionSignal.model_validate({
            "id": signal_id,
            "tenant": self.tenant,
            "origin": self.origin,
            "kind": self.kind,
            "subtype": self.subtype,
            "title": self.title,
            "summary": self.summary,
            "raw_text": self.raw_text,
            "geometry": self.geometry,
            "location_refs": self.location_refs,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "severity_bp": self.severity_bp,
            "severity_native": self.severity_native,
            "confidence_bp": confidence_bp,
            "provenance": self.provenance,
            "extracted_by": self.extracted_by,
            "source": {
                "connector_id": self.connector_id,
                "name": self.source_name,
                "url": self.source_url,
                "license": self.license,
                "authority_bp": self.authority_bp,
                "retrieved_at": self.retrieved_at,
                "native_id": self.native_id[:500],
            },
            "event_id": event_id,
            "version": version,
            "supersedes": supersedes,
        })


def _jsonable(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.UTC).isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, float):
        return repr(value)
    return value


@dataclass(slots=True)
class NewsItemDraft:
    """Untrusted text for the quarantine. ``context`` carries trusted structured facts (e.g. the
    supplier code of a Merxa supplier message) and the demo ``prebaked`` extraction, if any."""

    tenant: str | None
    connector_id: str
    native_key: str
    origin: str
    source_name: str
    license: str
    authority_bp: int
    url: str | None
    title: str
    snippet: str | None
    published_at: dt.datetime | None
    retrieved_at: dt.datetime
    context: dict[str, Any] = field(default_factory=dict)
    pack_id: str | None = None

    @property
    def text(self) -> str:
        """The untrusted text handed to the guard/extractor: headline + snippet only."""
        return self.title if not self.snippet else f"{self.title}\n{self.snippet}"
