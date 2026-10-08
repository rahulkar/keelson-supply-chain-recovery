"""Template packs (``data/packs/<pack>/pack.yaml``, mounted at ``PACKS_DIR``).

Shape (``docs/contracts/70-backoffice-m2.md`` §4)::

    name: value-fashion-eu
    description: …
    connectors: [ …same shape as infra/tenants.yaml connectors… ]   # platform: true → active
    spec_overrides: { …AgentSpec fields (merge patch on the built-in default)… }
    budgets: { budget_eur: 50, rpm_limit: 120, tpm_limit: 400000 }
    mappings: { connector: merxa-lite, read: mappings/read.yaml, action: mappings/action.yaml }  # M7, optional

``mappings`` (M7 tier A, ``110`` §4): the read / action mapping files (YAML or JSON, relative to
the pack directory) of a connector the admin registers in the wizard — packs never carry
credentials, so the connector itself is not in the pack. :func:`load_pack_mappings` reads them;
``POST /api/admin/connectors/{slug}/mappings/from-pack {pack}`` installs them as new mapping
versions (validated like any mapping ``PUT``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from keelson_engine.config import get_settings

__all__ = ["Pack", "PackError", "list_packs", "load_pack", "load_pack_mappings", "packs_dir"]

PACK_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
WEBHOOK_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,40}_WEBHOOK_SECRET$")
MAPPING_FILE_RE = re.compile(r"[a-z0-9_-]{1,40}(/[a-z0-9_-]{1,40}){0,2}\.(yaml|yml|json)")
MAX_MAPPING_FILE_BYTES = 512 * 1024
_BUDGET_KEYS = ("budget_eur", "rpm_limit", "tpm_limit", "budget_duration")


class PackError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Pack:
    name: str
    description: str
    connectors: list[dict[str, Any]] = field(default_factory=list)
    spec_overrides: dict[str, Any] = field(default_factory=dict)
    budgets: dict[str, Any] = field(default_factory=dict)
    mappings: dict[str, str] = field(default_factory=dict)
    """M7: ``{connector?, read?, action?}`` (file paths relative to the pack directory)."""
    path: Path | None = None
    """The pack directory (set by :func:`load_pack`)."""

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "connectors": [
                {"slug": c["slug"], "name": c.get("name") or c["slug"],
                 "platform": bool(c.get("platform") or (c.get("auth") or {}).get("reader_client"))}
                for c in self.connectors
            ],
            "budgets": self.budgets,
            "spec_overrides": sorted(self.spec_overrides),
            "mappings": [k for k in ("read", "action") if k in self.mappings],
            "mapping_connector": self.mappings.get("connector"),
        }


def packs_dir() -> Path:
    configured = Path(get_settings().packs_dir)
    if configured.is_dir():
        return configured
    source = Path(__file__).resolve().parents[5] / "data" / "packs"
    return source if source.is_dir() else configured


def _validate_mappings(name: str, raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - {"connector", "read", "action"}:
        raise PackError(f"pack {name}: mappings may only contain connector, read and action")
    out: dict[str, str] = {}
    if raw.get("connector") is not None:
        if not PACK_RE.match(str(raw["connector"])):
            raise PackError(f"pack {name}: mappings.connector is not a connector slug")
        out["connector"] = str(raw["connector"])
    for kind in ("read", "action"):
        value = raw.get(kind)
        if value is None:
            continue
        if not isinstance(value, str) or not MAPPING_FILE_RE.fullmatch(value):
            raise PackError(f"pack {name}: mappings.{kind} must be a relative .yaml/.json path inside the pack")
        out[kind] = value
    return out


def _validate(name: str, data: Any, path: Path | None = None) -> Pack:
    if not isinstance(data, dict):
        raise PackError(f"pack {name}: pack.yaml must be a mapping")
    if data.get("name") != name:
        raise PackError(f"pack {name}: 'name' must equal the directory name")
    connectors = data.get("connectors") or []
    if not isinstance(connectors, list):
        raise PackError(f"pack {name}: connectors must be a list")
    for c in connectors:
        if not isinstance(c, dict) or not c.get("slug") or not c.get("mcp_url"):
            raise PackError(f"pack {name}: every connector needs slug and mcp_url")
        if not PACK_RE.match(str(c["slug"])):
            raise PackError(f"pack {name}: invalid connector slug {c['slug']!r}")
        auth = c.get("auth") or {}
        if (c.get("platform") or auth.get("reader_client")) and not c.get("issuer"):
            raise PackError(f"pack {name}: platform connector {c['slug']} needs an issuer")
        if auth.get("secret") or auth.get("client_secret"):
            raise PackError(f"pack {name}: packs must not contain secrets")
        ref = c.get("webhook_secret_ref") or c.get("webhook_secret_env")
        if ref is not None and not WEBHOOK_REF_RE.match(str(ref)):
            raise PackError(f"pack {name}: {c['slug']}: webhook_secret_env must name a *_WEBHOOK_SECRET variable")
    overrides = data.get("spec_overrides") or {}
    if not isinstance(overrides, dict):
        raise PackError(f"pack {name}: spec_overrides must be a mapping")
    budgets = data.get("budgets") or {}
    if not isinstance(budgets, dict) or any(k not in _BUDGET_KEYS for k in budgets):
        raise PackError(f"pack {name}: budgets may only contain {', '.join(_BUDGET_KEYS)}")
    return Pack(name=name, description=str(data.get("description") or ""), connectors=list(connectors),
                spec_overrides=dict(overrides), budgets=dict(budgets),
                mappings=_validate_mappings(name, data.get("mappings")), path=path)


def load_pack(name: str, directory: Path | None = None) -> Pack:
    if not isinstance(name, str) or not PACK_RE.match(name):
        raise PackError("invalid pack name")
    path = (directory or packs_dir()) / name / "pack.yaml"
    if not path.is_file():
        raise PackError(f"unknown pack {name!r}")
    return _validate(name, yaml.safe_load(path.read_text(encoding="utf-8")), path.parent)


def load_pack_mappings(pack: Pack) -> dict[str, dict[str, Any]]:
    """The pack's mapping documents ``{"read"?: {...}, "action"?: {...}}`` (parsed YAML/JSON, not
    yet validated against the contract: the mapping ``PUT`` does that). Files must stay inside the
    pack directory (no symlink escapes) and are at most 512 KiB."""
    if pack.path is None:
        raise PackError(f"pack {pack.name}: location unknown")
    base = pack.path.resolve()
    out: dict[str, dict[str, Any]] = {}
    for kind in ("read", "action"):
        rel = pack.mappings.get(kind)
        if rel is None:
            continue
        file = (base / rel).resolve()
        if not file.is_relative_to(base) or not file.is_file():
            raise PackError(f"pack {pack.name}: mapping file {rel!r} not found")
        if file.stat().st_size > MAX_MAPPING_FILE_BYTES:
            raise PackError(f"pack {pack.name}: mapping file {rel!r} is too large")
        doc = yaml.safe_load(file.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise PackError(f"pack {pack.name}: mapping file {rel!r} must hold an object")
        if doc.get("kind", kind) != kind:
            raise PackError(f"pack {pack.name}: {rel!r} is not a {kind} mapping")
        out[kind] = doc
    return out


def list_packs(directory: Path | None = None) -> list[Pack]:
    root = directory or packs_dir()
    if not root.is_dir():
        return []
    return [load_pack(p.name, root) for p in sorted(root.iterdir())
            if p.is_dir() and PACK_RE.match(p.name) and (p / "pack.yaml").is_file()]
