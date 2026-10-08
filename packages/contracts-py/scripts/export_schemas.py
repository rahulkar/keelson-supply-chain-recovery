"""Export JSON Schemas for the web app into ``packages/contracts-ts/schemas``.

Usage (inside the uv environment)::

    uv run python scripts/export_schemas.py            # write schemas
    uv run python scripts/export_schemas.py --check    # exit 1 if files are stale
    uv run python scripts/export_schemas.py --out DIR  # write elsewhere

Output is deterministic: keys sorted, 2-space indent, UTF-8, LF line endings and a
trailing newline. Stale ``*.schema.json`` files in the output directory are
removed (``--check`` reports them). ``index.json`` lists every schema.

Every file is self-contained so that simple converters work (the web app uses
json-schema-to-zod 2.8.x, which does not resolve ``$ref``):

* ``$defs`` are inlined (none of the exported schemas is recursive; a cycle raises);
* ``oneOf`` (Pydantic's rendering of discriminated unions) is emitted as ``anyOf``.
  This is equivalent because every branch pins its discriminator with ``const``,
  and it lets converters produce a typed ``z.union`` instead of an untyped refine.

Fixed-length tuples (e.g. ``[lon_e6, lat_e6]``) stay standard 2020-12 ``prefixItems``;
the web generator (``packages/contracts-ts/scripts/gen-zod.mjs``) maps them to
``z.tuple``. Block ``data_ref`` payloads are exported as ``block.<kind>.v<n>.data``.
M5 adds the admin/API shapes ``policy.*`` (rules, rule set, context, decision, quorum) and
``demo.reset-*``. M6 adds the Signal Hub shapes ``signals.*`` (signal, event, exposure, alert,
alert detail/status update/rule, scenario pack, tenant network). M7 adds the back-office shapes
``admin.*`` (change requests, users), ``agents.*`` (custom specialist, playground), ``mapping.*``
(tier-A read/action mappings, dry-run), ``scn.snapshot-label``, ``commit.ledger-entry`` and
``connectors.capabilities``. M8 adds the Evals UI shapes ``evals.*`` (status, runs, run detail /
create / events, case results, compare, trends, thresholds, runner suite).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter

import keelson_contracts
from keelson_contracts import admin, agents, agui, blocks, commit, connectors, demo, llm, mapping, policy, runs, scn, signals
from keelson_contracts import evals

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
DEFAULT_OUT = Path(__file__).resolve().parents[2] / "contracts-ts" / "schemas"


def _model(model: type[BaseModel]) -> Callable[[], dict[str, Any]]:
    return lambda: model.model_json_schema(mode="validation")


def _adapter(adapter: TypeAdapter[Any], title: str) -> Callable[[], dict[str, Any]]:
    def build() -> dict[str, Any]:
        schema = adapter.json_schema(mode="validation")
        schema.setdefault("title", title)
        return schema

    return build


def schema_builders() -> dict[str, tuple[str, Callable[[], dict[str, Any]]]]:
    """``name -> (python source, builder)`` for every exported schema."""
    out: dict[str, tuple[str, Callable[[], dict[str, Any]]]] = {
        "block.envelope": (
            "keelson_contracts.blocks.BlockEnvelope",
            _adapter(blocks.BLOCK_ENVELOPE_ADAPTER, "BlockEnvelope"),
        ),
        "block.provenance": (
            "keelson_contracts.blocks.BlockProvenance",
            _model(blocks.BlockProvenance),
        ),
    }
    for key, props_model in blocks.BLOCK_PROPS_REGISTRY.items():
        kind, version = key.split("@")
        out[f"block.{kind}.v{version}.props"] = (
            f"keelson_contracts.blocks.{props_model.__name__}",
            _model(props_model),
        )
    for key, data_model in blocks.BLOCK_DATA_REGISTRY.items():
        kind, version = key.split("@")
        out[f"block.{kind}.v{version}.data"] = (
            f"{data_model.__module__}.{data_model.__name__}",
            _model(data_model),
        )
    out["agui.sc-metadata"] = ("keelson_contracts.agui.ScMetadata", _model(agui.ScMetadata))
    out["agui.interrupt"] = ("keelson_contracts.agui.InterruptSpec", _model(agui.InterruptSpec))
    out["run.request"] = ("keelson_contracts.runs.RunRequest", _model(runs.RunRequest))
    m5_models: tuple[tuple[str, type[BaseModel]], ...] = (
        ("policy.rule", policy.PolicyRule),
        ("policy.set", policy.PolicySet),
        ("policy.context", policy.PolicyContext),
        ("policy.decision", policy.PolicyDecision),
        ("policy.quorum", policy.QuorumView),
        ("demo.reset-request", demo.DemoResetRequest),
        ("demo.reset-result", demo.DemoResetResult),
    )
    for name, m5_model in m5_models:
        out[name] = (f"{m5_model.__module__}.{m5_model.__name__}", _model(m5_model))
    m6_models: tuple[tuple[str, type[BaseModel]], ...] = (
        ("signals.signal", signals.DisruptionSignal),
        ("signals.event", signals.DisruptionEvent),
        ("signals.exposure", signals.Exposure),
        ("signals.exposure-summary", signals.ExposureSummary),
        ("signals.alert", signals.Alert),
        ("signals.alert-detail", signals.AlertDetail),
        ("signals.alert-status-update", signals.AlertStatusUpdate),
        ("signals.alert-rule", signals.AlertRule),
        ("signals.scenario-pack", signals.ScenarioPack),
        ("signals.tenant-network", signals.TenantNetwork),
    )
    for name, m6_model in m6_models:
        out[name] = (f"{m6_model.__module__}.{m6_model.__name__}", _model(m6_model))
    m7_models: tuple[tuple[str, type[BaseModel]], ...] = (
        ("admin.change-request", admin.AdminChangeRequest),
        ("admin.change-request-decision", admin.ChangeRequestDecision),
        ("admin.user", admin.AdminUser),
        ("admin.user-create", admin.AdminUserCreate),
        ("admin.user-created", admin.AdminUserCreated),
        ("admin.user-patch", admin.AdminUserPatch),
        ("agents.custom-specialist", agents.CustomSpecialistSpec),
        ("agents.playground-request", agents.PlaygroundRequest),
        ("agents.playground-result", agents.PlaygroundResult),
        ("mapping.read", mapping.ReadMapping),
        ("mapping.action", mapping.ActionMapping),
        ("mapping.dry-run-request", mapping.ActionDryRunRequest),
        ("mapping.dry-run-result", mapping.ActionDryRunResult),
        ("scn.snapshot-label", scn.SnapshotLabel),
        ("commit.ledger-entry", commit.CommitLedgerEntry),
        ("connectors.capabilities", connectors.ConnectorCapabilities),
    )
    for name, m7_model in m7_models:
        out[name] = (f"{m7_model.__module__}.{m7_model.__name__}", _model(m7_model))
    m8_evals_models: tuple[tuple[str, type[BaseModel]], ...] = (
        ("evals.status", evals.EvalsStatus),
        ("evals.run", evals.EvalRun),
        ("evals.run-detail", evals.EvalRunDetail),
        ("evals.run-create", evals.EvalRunCreate),
        ("evals.run-event", evals.EvalRunEvent),
        ("evals.case-result", evals.EvalCaseResult),
        ("evals.compare", evals.EvalCompare),
        ("evals.trends", evals.EvalTrends),
        ("evals.threshold", evals.EvalThreshold),
        ("evals.suite", evals.EvalSuiteInfo),
    )
    for name, m8_model in m8_evals_models:
        out[name] = (f"{m8_model.__module__}.{m8_model.__name__}", _model(m8_model))
    for model in llm.LLM_OUTPUT_MODELS:
        out[f"llm.{_kebab(model.__name__)}"] = (
            f"keelson_contracts.llm.{model.__name__}",
            _model(model),
        )
    return out


def _kebab(name: str) -> str:
    chars: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            chars.append("-")
        chars.append(ch.lower())
    return "".join(chars)


def _resolve_ref(ref: str, defs: Mapping[str, Any]) -> Any:
    prefix = "#/$defs/"
    if not ref.startswith(prefix) or ref[len(prefix):] not in defs:
        raise ValueError(f"unsupported or dangling $ref {ref!r}")
    return defs[ref[len(prefix):]]


def self_contained(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Inline local ``$defs`` references and turn ``oneOf`` into ``anyOf``."""
    defs: Mapping[str, Any] = schema.get("$defs", {})

    def walk(node: Any, stack: tuple[str, ...]) -> Any:
        if isinstance(node, list):
            return [walk(item, stack) for item in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref = node["$ref"]
            if ref in stack:
                raise ValueError(f"recursive schema via {ref!r} cannot be inlined")
            target = walk(_resolve_ref(ref, defs), (*stack, ref))
            siblings = {k: walk(v, stack) for k, v in node.items() if k != "$ref"}
            return {**target, **siblings}
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key == "$defs":
                continue
            out["anyOf" if key == "oneOf" else key] = walk(value, stack)
        return out

    result = walk(schema, ())
    assert isinstance(result, dict)
    return result


def _dump(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def render_all() -> dict[str, str]:
    """File name -> exact file content for every schema plus ``index.json``."""
    files: dict[str, str] = {}
    index: list[dict[str, str]] = []
    for name, (source, build) in sorted(schema_builders().items()):
        schema = self_contained(build())
        schema["$schema"] = JSON_SCHEMA_DIALECT
        schema["x-keelson-source"] = source
        file_name = f"{name}.schema.json"
        files[file_name] = _dump(schema)
        index.append({"name": name, "file": file_name, "source": source, "title": schema["title"]})
    files["index.json"] = _dump(
        {"package": "keelson-contracts", "version": keelson_contracts.__version__, "schemas": index}
    )
    return files


def write(out_dir: Path) -> list[str]:
    """Write all files; delete stale managed files. Returns the names written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files = render_all()
    for stale in out_dir.glob("*.schema.json"):
        if stale.name not in files:
            stale.unlink()
    for name, content in files.items():
        (out_dir / name).write_bytes(content.encode("utf-8"))
    return sorted(files)


def check(out_dir: Path) -> list[str]:
    """Differences between generated output and ``out_dir`` (empty = up to date)."""
    files = render_all()
    problems: list[str] = []
    for name, content in files.items():
        path = out_dir / name
        if not path.exists():
            problems.append(f"missing: {name}")
        elif path.read_bytes() != content.encode("utf-8"):
            problems.append(f"stale: {name}")
    for existing in out_dir.glob("*.schema.json"):
        if existing.name not in files:
            problems.append(f"unexpected: {existing.name}")
    return sorted(problems)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument("--check", action="store_true", help="verify instead of writing")
    args = parser.parse_args(argv)
    if args.check:
        problems = check(args.out)
        for p in problems:
            print(p, file=sys.stderr)
        print(f"{'stale' if problems else 'up to date'}: {args.out}")
        return 1 if problems else 0
    names = write(args.out)
    print(f"wrote {len(names)} files to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
