"""``{{ ref }}`` templates of the sidecar config (op steps, keys, guide texts, change-feed keys).

Same surface syntax and rendering rule as the engine's action-mapping templates
(``keelson_contracts.mapping``), with the sidecar's own roots:

* ``ref := root ("." (ident | index))*`` — ``payload.lines``, ``item.sku``, ``before.qty``;
* a string that is **exactly one** placeholder takes the referenced value with its JSON type
  (``"{{payload.qty}}"`` → ``12``); any other string is text with each placeholder replaced by
  its text (integers in decimal, booleans ``true``/``false``); a ``null`` operand makes the whole
  string ``null`` (so a missing value never becomes part of a key);
* a missing key, an index out of range or indexing a scalar yields ``null`` — never an error.

There is no ``eval`` and no attribute access: values are plain JSON (dict / list / str / int /
bool / null) and a path can only index dict keys and list positions. Segments starting with
``__`` are refused at validation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

MAX_TEMPLATE_CHARS = 2000
MAX_DEPTH = 8

_SEGMENT_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*|0|[1-9][0-9]{0,8})$")
_EXACT_RE = re.compile(r"^\{\{\s*([^{}]*?)\s*\}\}$")


class TemplateError(ValueError):
    """A malformed template, or a placeholder this context does not allow."""


@dataclass(frozen=True, slots=True)
class Ref:
    root: str
    path: tuple[str | int, ...]

    def __str__(self) -> str:
        return ".".join([self.root, *(str(p) for p in self.path)])


def _placeholders(text: str) -> list[str]:
    """Inner texts of every ``{{ … }}`` (stray or nested braces are errors)."""
    out: list[str] = []
    i = 0
    while True:
        start = text.find("{{", i)
        stray = text.find("}}", i)
        if start < 0:
            if stray >= 0:
                raise TemplateError(f"'}}}}' without '{{{{' in {text[:80]!r}")
            return out
        if 0 <= stray < start:
            raise TemplateError(f"'}}}}' without '{{{{' in {text[:80]!r}")
        end = text.find("}}", start + 2)
        if end < 0:
            raise TemplateError(f"unterminated '{{{{' in {text[:80]!r}")
        inner = text[start + 2:end]
        if "{" in inner or "}" in inner:
            raise TemplateError(f"nested braces in {text[:80]!r}")
        out.append(inner.strip())
        i = end + 2


def parse_ref(text: str) -> Ref:
    parts = text.split(".")
    if not text or not all(_SEGMENT_RE.fullmatch(p) for p in parts):
        raise TemplateError(f"invalid placeholder {{{{{text[:60]}}}}}")
    if any(p.startswith("__") for p in parts):
        raise TemplateError("placeholder segments may not start with '__'")
    if parts[0].isdigit():
        raise TemplateError(f"placeholder {{{{{text[:60]}}}}} must start with a name")
    path: list[str | int] = [int(p) if p.isdigit() else p for p in parts[1:]]
    return Ref(parts[0], tuple(path))


def refs_of(template: Any) -> list[Ref]:
    """Every placeholder ref of a template (string / int / bool / null / list / dict of those)."""
    out: list[Ref] = []

    def walk(node: Any, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise TemplateError(f"template nested deeper than {MAX_DEPTH}")
        if node is None or isinstance(node, bool | int):
            return
        if isinstance(node, float):
            raise TemplateError("floats are not allowed (money is cents, ratios are basis points)")
        if isinstance(node, str):
            if len(node) > MAX_TEMPLATE_CHARS:
                raise TemplateError(f"template longer than {MAX_TEMPLATE_CHARS} characters")
            out.extend(parse_ref(p) for p in _placeholders(node))
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                if not isinstance(key, str) or "{{" in key or "}}" in key:
                    raise TemplateError("object keys are literal strings (no placeholders)")
                walk(value, depth + 1)
            return
        raise TemplateError(f"not a JSON template value: {type(node).__name__}")

    walk(template, 0)
    return out


@dataclass(frozen=True)
class RootRule:
    """What a root accepts: ``fields`` = allowed first path segments (``None`` = any, ``()`` = no
    path at all, e.g. ``{{target_ref}}``)."""

    fields: frozenset[str] | None


def check_refs(template: Any, roots: Mapping[str, RootRule], where: str) -> list[Ref]:
    """Validate ``template`` against the roots of its context; raises :class:`TemplateError`."""
    try:
        refs = refs_of(template)
    except TemplateError as exc:
        raise TemplateError(f"{where}: {exc}") from None
    for ref in refs:
        rule = roots.get(ref.root)
        if rule is None:
            allowed = ", ".join(sorted(roots)) or "none"
            raise TemplateError(f"{where}: unknown placeholder root {ref.root!r} (allowed here: {allowed})")
        if rule.fields is not None:
            if not rule.fields and ref.path:
                raise TemplateError(f"{where}: {{{{{ref.root}}}}} has no fields")
            if ref.path and str(ref.path[0]) not in rule.fields:
                raise TemplateError(
                    f"{where}: {ref.root} has no field {ref.path[0]!r} (fields: {', '.join(sorted(rule.fields))})"
                )
    return refs


def resolve(ref: Ref, scope: Mapping[str, Any]) -> Any:
    """Value of ``ref`` in ``scope`` (``None`` for anything missing)."""
    node: Any = scope.get(ref.root)
    for seg in ref.path:
        if isinstance(seg, int):
            if isinstance(node, list) and 0 <= seg < len(node):
                node = node[seg]
            elif isinstance(node, Mapping):
                node = node.get(str(seg))
            else:
                return None
        elif isinstance(node, Mapping):
            node = node.get(seg)
        else:
            return None
        if node is None:
            return None
    return node


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | str):
        return str(value)
    raise TemplateError(f"cannot place a {type(value).__name__} inside text")


def render(template: Any, scope: Mapping[str, Any]) -> Any:
    """Render a validated template (see the module docstring for the rules)."""
    if isinstance(template, str):
        exact = _EXACT_RE.match(template)
        if exact and "{{" not in exact.group(1):
            return resolve(parse_ref(exact.group(1).strip()), scope)
        if "{{" not in template:
            return template
        out: list[str] = []
        i = 0
        while True:
            start = template.find("{{", i)
            if start < 0:
                out.append(template[i:])
                return "".join(out)
            end = template.find("}}", start + 2)
            out.append(template[i:start])
            value = resolve(parse_ref(template[start + 2:end].strip()), scope)
            if value is None:
                return None
            out.append(_text(value))
            i = end + 2
    if isinstance(template, list):
        return [render(v, scope) for v in template]
    if isinstance(template, Mapping):
        return {k: render(v, scope) for k, v in template.items()}
    return template
