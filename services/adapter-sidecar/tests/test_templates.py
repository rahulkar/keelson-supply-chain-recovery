"""``{{ ref }}`` templates: typing of exact placeholders, text rendering, missing values, safety."""

from __future__ import annotations

import pytest

from adapter_sidecar.templates import RootRule, TemplateError, check_refs, parse_ref, refs_of, render, resolve

SCOPE = {"payload": {"qty": 12, "lines": [{"sku": "A", "qty": 3}, {"sku": "B", "qty": 4}], "flag": True},
         "target_ref": "AL-0001", "expected_version": None, "item": {"sku": "A", "qty": 3}, "index": 1}


def test_exact_placeholder_keeps_type() -> None:
    assert render("{{payload.qty}}", SCOPE) == 12
    assert render("{{ payload.lines }}", SCOPE) == SCOPE["payload"]["lines"]
    assert render("{{payload.flag}}", SCOPE) is True
    assert render("{{index}}", SCOPE) == 1


def test_mixed_placeholders_render_text() -> None:
    assert render("{{item.sku}} × {{item.qty}}", SCOPE) == "A × 3"
    assert render("change_set:{{target_ref}}", SCOPE) == "change_set:AL-0001"
    assert render("flag={{payload.flag}}", SCOPE) == "flag=true"
    assert render("plain", SCOPE) == "plain"
    assert render({"a": ["{{payload.qty}}", 5, None]}, SCOPE) == {"a": [12, 5, None]}


def test_missing_path_is_null() -> None:
    assert render("{{payload.nope}}", SCOPE) is None
    assert render("{{payload.lines.7.sku}}", SCOPE) is None
    assert render("{{payload.qty.deep}}", SCOPE) is None
    assert render("v{{expected_version}}", SCOPE) is None  # a null operand nulls the whole text
    assert resolve(parse_ref("payload.lines.1.sku"), SCOPE) == "B"


def test_containers_cannot_be_text() -> None:
    with pytest.raises(TemplateError):
        render("lines: {{payload.lines}}", SCOPE)


def test_validation_rules() -> None:
    roots = {"payload": RootRule(None), "target_ref": RootRule(frozenset()), "before": RootRule(frozenset({"qty"}))}
    assert [str(r) for r in check_refs("{{payload.a.0}} {{before.qty}}", roots, "t")] == ["payload.a.0", "before.qty"]
    for bad, msg in (("{{secret}}", "unknown placeholder root"), ("{{target_ref.x}}", "has no fields"),
                     ("{{before.sku}}", "has no field 'sku'"), ("{{payload.__class__}}", "__"),
                     ("{{payload..x}}", "invalid placeholder"), ("{{0.x}}", "must start with a name"),
                     ("{{payload", "unterminated"), ("x}}", "without"), ("{{a{{b}}}}", "nested"),
                     ("{{payload.a-b}}", "invalid placeholder")):
        with pytest.raises(TemplateError, match=msg):
            check_refs(bad, roots, "t")
    with pytest.raises(TemplateError, match="floats"):
        refs_of({"a": 1.5})
    with pytest.raises(TemplateError, match="literal"):
        refs_of({"{{payload.k}}": 1})


def test_no_attribute_access() -> None:
    class Obj:
        secret = "x"

    # a path can only index dicts and lists; objects never expose attributes
    assert resolve(parse_ref("payload.secret"), {"payload": Obj()}) is None
    with pytest.raises(TemplateError):
        parse_ref("payload.__dict__")
