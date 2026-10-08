"""Independent review of the M7 contracts (``docs/contracts/110-m7-backoffice-generic.md``):
regression tests for the defects found in review.

* users API: realm roles are global in Keycloak, so a tenant-admin must not change a user who
  also belongs to another tenant (granting ``tenant-admin`` there = cross-tenant privilege
  escalation); ``actor_tenants`` / ``target_tenants`` are required arguments;
* four-eyes bypass: a tenant-admin granting an admin role (create, add, re-enable) is itself a
  guarded change (kind ``user_roles``), so nobody approves their own change with a second admin
  account they just created;
* stale apply: ``agent_spec`` / ``policy_set`` change requests must carry ``base_version``;
* change requests never carry credentials (payload keys / diff paths), and the one-time
  password-setup link stays out of ``repr`` (logs);
* the crawl only calls approved, trusted ``read`` tools (:func:`read_mapping_violations`): a
  read mapping pointing at a write tool would call it up to ``max_pages`` times without approval;
* Python ``$`` matches before a trailing newline: slugs, lookup names, date formats and template
  refs are matched with ``fullmatch``;
* Pro detection catches ``pro`` followed by a digit or separated by spaces;
* a custom specialist's SUBAGENT_STARTED display name is one trimmed line.
"""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import pytest
from pydantic import ValidationError

from keelson_contracts import agui
from keelson_contracts.admin import (
    CHANGE_REQUEST_KINDS,
    GUARD_REASON_CODES,
    VERSIONED_CHANGE_KINDS,
    AdminChangeRequest,
    PasswordSetup,
    user_change_guard_reasons,
    user_change_violations,
)
from keelson_contracts.agents import is_custom_specialist_slug, is_pro_model
from keelson_contracts.connectors import ContentTrust, Egress, Idempotency, ToolAccess, ToolClassification
from keelson_contracts.mapping import (
    ACTION_TEMPLATE_ROOTS,
    ExpressionError,
    ReadMapping,
    TemplateError,
    parse_expression,
    read_mapping_violations,
    template_refs,
)
from test_m7 import T0, _cr, mcp_lite_read_mapping

TA = ["tenant-admin"]
PA = ["platform-admin"]


# ─────────────────────────────── users: tenancy ─────────────────────────────


def test_tenant_admin_cannot_change_users_of_other_tenants() -> None:
    own = {"actor_tenants": ["northwind-test"], "target_tenants": ["northwind-test"]}
    assert user_change_violations(TA, target_roles=["planner"], new_roles=["planner", "tenant-admin"], **own) == []
    # the user also belongs to demo-retail: tenant-admin is a global realm role, so this would make
    # them tenant-admin of demo-retail as well
    shared = {"actor_tenants": ["northwind-test"], "target_tenants": ["northwind-test", "demo-retail"]}
    problems = user_change_violations(TA, target_roles=["planner"], new_roles=["planner", "tenant-admin"], **shared)
    assert problems and "demo-retail" in problems[0]
    assert user_change_violations(TA, target_roles=["planner"], enabled=False, **shared)
    # a user of another tenant only / of no tenant
    assert user_change_violations(TA, target_roles=["planner"], enabled=False,
                                  actor_tenants=["northwind-test"], target_tenants=["demo-retail"])
    assert user_change_violations(TA, target_roles=["planner"], enabled=False,
                                  actor_tenants=["northwind-test"], target_tenants=[])
    # an admin of both tenants may; a platform-admin always may
    assert user_change_violations(TA, target_roles=["planner"], enabled=False,
                                  actor_tenants=["demo-retail", "northwind-test"],
                                  target_tenants=["northwind-test", "demo-retail"]) == []
    assert user_change_violations(PA, target_roles=["planner"], new_roles=["tenant-admin"],
                                  actor_tenants=[], target_tenants=["northwind-test", "demo-retail"]) == []


def test_user_change_violations_requires_the_tenants() -> None:
    with pytest.raises(TypeError):
        user_change_violations(TA, target_roles=["planner"], new_roles=["approver"])  # type: ignore[call-arg]


# ─────────────────────────── users: four-eyes guard ─────────────────────────


def test_granting_an_admin_role_is_guarded_for_tenant_admins() -> None:
    assert "user_roles" in CHANGE_REQUEST_KINDS and "admin_role_granted" in GUARD_REASON_CODES
    # create a second tenant-admin (the sock-puppet approver)
    reasons = user_change_guard_reasons(TA, user="puppet", target_roles=[], new_roles=["tenant-admin"])
    assert [(r.code, r.field) for r in reasons] == [("admin_role_granted", "user:puppet/role:tenant-admin")]
    # add the role to an existing user
    assert user_change_guard_reasons(TA, user="nora", target_roles=["planner"], new_roles=["planner", "tenant-admin"])
    # re-enable a disabled admin
    assert user_change_guard_reasons(TA, user="sam", target_roles=["tenant-admin"], enabled=True,
                                     target_enabled=False)
    # not guarded: non-admin roles, removing admin, disabling, enabling an enabled admin, platform-admins
    assert user_change_guard_reasons(TA, user="nora", target_roles=["planner"], new_roles=["approver"]) == []
    assert user_change_guard_reasons(TA, user="sam", target_roles=["tenant-admin"], new_roles=["planner"]) == []
    assert user_change_guard_reasons(TA, user="sam", target_roles=["tenant-admin"], enabled=False,
                                     target_enabled=True) == []
    assert user_change_guard_reasons(TA, user="sam", target_roles=["tenant-admin"], enabled=True,
                                     target_enabled=True) == []
    assert user_change_guard_reasons(PA, user="first", target_roles=[], new_roles=["tenant-admin"]) == []


def test_user_roles_change_request_shape() -> None:
    reasons = user_change_guard_reasons(TA, user="puppet", target_roles=[], new_roles=["tenant-admin"])
    cr = AdminChangeRequest.model_validate(_cr(
        kind="user_roles", target="user:puppet", base_version=None,
        payload={"username": "puppet", "email": "p@northwind.test", "name": "P", "roles": ["tenant-admin"]},
        diff=[{"op": "add", "path": "/roles/0", "after": "tenant-admin"}],
        reasons=[r.model_dump() for r in reasons],
    ))
    assert cr.kind == "user_roles"
    with pytest.raises(ValidationError, match="do not apply"):
        AdminChangeRequest.model_validate(_cr(reasons=[r.model_dump() for r in reasons]))  # agent_spec kind


# ─────────────────────────── change requests: hygiene ───────────────────────


@pytest.mark.parametrize("kind", sorted(VERSIONED_CHANGE_KINDS))
def test_versioned_kinds_need_base_version(kind: str) -> None:
    with pytest.raises(ValidationError, match="base_version"):
        AdminChangeRequest.model_validate(_cr(kind=kind, base_version=None))
    assert AdminChangeRequest.model_validate(_cr(kind=kind)).base_version == "v3"


def test_kb_delete_needs_no_base_version() -> None:
    cr = _cr(kind="kb_delete", target="kb:0199", base_version=None, payload={},
             diff=[{"op": "remove", "path": "/kbs/0199", "before": {"name": "SOPs"}}],
             reasons=[{"code": "kb_deleted", "field": "kb:0199", "reason": "deletes a knowledge base"}])
    assert AdminChangeRequest.model_validate(cr).base_version is None


@pytest.mark.parametrize(
    "over",
    [
        {"payload": {"auth": {"kind": "static_header", "secret": "mk-live-123"}}},
        {"payload": {"auth": {"client_secret": "s3cr3t"}}},
        {"payload": {"headers": [{"Authorization": "Bearer x"}]}},
        {"payload": {"x": {"api-key": "k"}}},
        {"payload": {"webhook_secret": "w"}},
        {"diff": [{"op": "replace", "path": "/auth/client_secret", "before": "a", "after": "b"}]},
        {"diff": [{"op": "add", "path": "/auth", "after": {"password": "p"}}]},
    ],
)
def test_change_requests_never_carry_credentials(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="credentials"):
        AdminChangeRequest.model_validate(_cr(**over))


def test_credential_check_has_no_false_positives() -> None:
    ok = _cr(payload={"base_version": "v3", "auth": {"kind": "oauth_client", "token_url": "https://idp/token",
                                                    "client_id": "keelson", "header_name": "X-Api-Key"}},
             diff=[{"op": "replace", "path": "/auth/token_url", "before": "/api/token", "after": "/oauth/token"}])
    assert AdminChangeRequest.model_validate(ok)


def test_password_setup_link_is_not_in_repr() -> None:
    link = "https://auth.localhost/realms/keelson/login-actions/action-token?key=SECRET-ACTION-TOKEN"
    setup = PasswordSetup(link=link, link_expires_at=T0 + dt.timedelta(hours=12), instructions="Open once.")
    assert "SECRET-ACTION-TOKEN" not in repr(setup) and "SECRET-ACTION-TOKEN" not in str(setup)
    assert setup.model_dump()["link"] == link  # still on the wire, once


# ─────────────────────────────── crawl tools ────────────────────────────────


def _cls(access: ToolAccess = ToolAccess.READ, trust: ContentTrust = ContentTrust.TRUSTED_DATA) -> ToolClassification:
    return ToolClassification(access=access, content_trust=trust, egress=Egress.INTERNAL,
                              idempotency=Idempotency.NONE if access is ToolAccess.READ else Idempotency.ENFORCED)


def test_read_mapping_violations_only_read_tools() -> None:
    m = ReadMapping.model_validate(mcp_lite_read_mapping())
    assert read_mapping_violations(m, tools={"list_items": _cls()}) == []
    assert read_mapping_violations(m, tools={})  # not approved
    write = read_mapping_violations(m, tools={"list_items": _cls(ToolAccess.WRITE)})
    assert write and all("write tool" in p for p in write)
    assert any(p.startswith("lookups.po_supplier") for p in write)  # source lookups are checked too
    untrusted = read_mapping_violations(m, tools={"list_items": _cls(trust=ContentTrust.UNTRUSTED_TEXT)})
    assert untrusted and all("untrusted text" in p for p in untrusted)
    # one bad lookup source among good entity sources
    data = copy.deepcopy(mcp_lite_read_mapping())
    data["lookups"]["po_supplier"]["source"]["tool"] = "update_item"
    bad = read_mapping_violations(ReadMapping.model_validate(data),
                                  tools={"list_items": _cls(), "update_item": _cls(ToolAccess.DESTRUCTIVE)})
    assert bad == ["lookups.po_supplier: tool 'update_item' is a destructive tool (a crawl only calls read tools)"]


# ───────────────────────── anchoring ($ before newline) ──────────────────────


def test_trailing_newline_is_never_accepted() -> None:
    assert not is_custom_specialist_slug("supplier_risk\n")
    assert is_custom_specialist_slug("supplier_risk")
    with pytest.raises(TemplateError, match="invalid placeholder"):
        template_refs({"a": "{{payload.lines\n.0}}"}, roots=ACTION_TEMPLATE_ROOTS)
    with pytest.raises(ValueError, match="custom specialist slug"):
        agui.subagent_started(_sc(), subagent_run_id="r1", name="supplier_risk\n", description="S", custom=True)


def test_lookup_and_date_constants_fully_match() -> None:
    # (control characters inside string literals are refused earlier; fullmatch is the second line)
    from keelson_contracts.mapping import _date_format_ok

    assert _date_format_ok("DD/MM/YYYY")
    assert not _date_format_ok("DD/MM/YYYY\n")
    with pytest.raises(ExpressionError):
        parse_expression("lookup('abc\n', $.x)")


# ───────────────────────────────── Pro models ───────────────────────────────


@pytest.mark.parametrize(
    ("name", "pro"),
    [
        ("gemini-1.5-pro002", True), ("Gemini 2.5 Pro", True), ("gemini-pro2", True), ("o1pro", True),
        ("prod-fast", False), ("improve", False), ("proto-fast", False), ("gemini/gemini-3.8-flash", False),
        ("gemini/gemini-3.5-flash-lite", False), ("chat_pro", True), ("PRO-1", True),
    ],
)
def test_is_pro_model_more_spellings(name: str, pro: bool) -> None:
    assert is_pro_model(name) is pro


# ──────────────────────────── SUBAGENT_STARTED name ─────────────────────────


def _sc() -> agui.ScMetadata:
    return agui.ScMetadata(tenant_id="northwind-test", case_id="c1", stage="checking_systems", prov="inference",
                           label="Supplier risk analyst started")


@pytest.mark.parametrize("description", ["Supplier\nrisk", " Supplier risk", "Supplier risk ", "S\x07"])
def test_custom_subagent_display_name_is_one_trimmed_line(description: str) -> None:
    with pytest.raises(ValueError, match="one trimmed line"):
        agui.subagent_started(_sc(), subagent_run_id="r1", name="supplier_risk", description=description,
                              custom=True)
    ev = agui.subagent_started(_sc(), subagent_run_id="r1", name="supplier_risk", description="Supplier risk",
                               custom=True)
    assert ev["description"] == "Supplier risk"
