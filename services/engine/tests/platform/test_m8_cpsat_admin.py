"""M8 planning solver settings in the agent spec (``docs/contracts/125-m8-optimizer-cpsat.md`` §7):
the integer stage and the sourcing terms are commercial tuning (applied directly, stored only when
they differ from the default), while removing a factory from the compliance block list is a guarded
change - a tenant-admin's PUT becomes a four-eyes change request, a platform-admin's is applied."""

from __future__ import annotations

from typing import Any

import pytest

from e1_browser import Browser
from e1_litellm import FakeLiteLLM
from test_admin_agents import SPEC, _put, merxa_tools, people  # noqa: F401 - fixtures

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _alt(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["planning"]["alt_sourcing"]


async def test_planning_solver_settings_and_the_blocked_factory_guard(
    people: dict[str, Browser],  # noqa: F811
    gateway_ready: FakeLiteLLM,
    merxa_tools: None,  # noqa: F811
) -> None:
    sam, ava = people["sam"], people["ava"]  # tenant-admin, platform-admin
    base = (await sam.get(SPEC)).json()
    assert "solver_integer_stage" not in base["spec"]["planning"]
    assert "order_multiple_units" not in _alt(base["spec"])
    # blocking a factory only tightens: applied
    r = await _put(sam, base["version"], {"planning": {"alt_sourcing": {"blocked_factories": ["F-MA-01", "F-TR-01"]}}})
    assert r.status_code == 201, r.text
    v = r.json()["version"]
    # the solver and the sourcing terms are commercial tuning: applied
    tuning = {"solver_integer_stage": "cpsat",
              "alt_sourcing": {"order_multiple_units": 6, "moq_basis": "style", "share_factory_capacity": True}}
    r = await _put(sam, v, {"planning": tuning})
    assert r.status_code == 201, r.text
    spec = r.json()["spec"]
    assert spec["planning"]["solver_integer_stage"] == "cpsat"
    assert {k: _alt(spec)[k] for k in ("order_multiple_units", "moq_basis", "share_factory_capacity")} == {
        "order_multiple_units": 6, "moq_basis": "style", "share_factory_capacity": True}
    v = r.json()["version"]
    # invalid terms are refused before anything else
    bad = await _put(sam, v, {"planning": {"alt_sourcing": {"order_multiple_units": 0}}})
    assert bad.status_code == 422, bad.text
    # removing a blocked factory relaxes compliance: a pending change request, nothing applied
    r = await _put(sam, v, {"planning": {"alt_sourcing": {"blocked_factories": ["F-TR-01"]}}})
    assert r.status_code == 202 and r.json()["kind"] == "agent_spec" and r.json()["status"] == "pending", r.text
    assert [(x["code"], x["field"]) for x in r.json()["reasons"]] == [
        ("compliance_block_removed", "planning.alt_sourcing.blocked_factories")]
    assert (await sam.get(SPEC)).json()["version"] == v
    # the platform admin applies it directly; back to the defaults drops the keys again
    r = await _put(ava, v, {"planning": {"alt_sourcing": {"blocked_factories": ["F-TR-01"]}}})
    assert r.status_code == 201, r.text
    r = await _put(sam, r.json()["version"], {"planning": {"solver_integer_stage": "repair",
                                                           "alt_sourcing": {"order_multiple_units": 1}}})
    assert r.status_code == 201, r.text
    assert "solver_integer_stage" not in r.json()["spec"]["planning"]
    assert "order_multiple_units" not in _alt(r.json()["spec"])
