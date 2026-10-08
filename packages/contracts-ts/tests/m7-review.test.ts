// Regression tests for the M7 contracts review (mirrors packages/contracts-py/tests/test_m7_review.py).
import { describe, expect, it } from "vitest";
import { CHANGE_REQUEST_KINDS, GUARD_REASON_CODES, adminChangeRequestSchema, isProModel } from "../src";

describe("M7 review: Pro detection matches contracts-py", () => {
  // the same table as test_m7_review.py::test_is_pro_model_more_spellings + test_m7.py::test_is_pro_model
  const cases: Array<[string, boolean]> = [
    ["gemini-2.5-pro", true],
    ["gemini/gemini-3-pro-preview", true],
    ["vertex_ai/gemini-pro", true],
    ["pro-default", true],
    ["PRO", true],
    ["gemini-1.5-pro002", true],
    ["Gemini 2.5 Pro", true],
    ["gemini-pro2", true],
    ["o1pro", true],
    ["chat_pro", true],
    ["PRO-1", true],
    ["gemini-2.5-flash", false],
    ["prod-fast", false],
    ["fast-default", false],
    ["reasoning-default", false],
    ["improve", false],
    ["proto-fast", false],
    ["gemini-flash-lite", false],
    ["gemini/gemini-3.8-flash", false],
    ["gemini/gemini-3.5-flash-lite", false],
  ];
  it.each(cases)("%s -> %s", (name, pro) => {
    expect(isProModel(name)).toBe(pro);
  });
});

describe("M7 review: granting an admin role is a guarded change", () => {
  it("knows the user_roles kind and its reason code", () => {
    expect(CHANGE_REQUEST_KINDS).toContain("user_roles");
    expect(GUARD_REASON_CODES).toContain("admin_role_granted");
  });

  it("parses a pending user_roles change request", () => {
    const cr = {
      id: "0199c1a2-7b3c-7d4e-8f50-0123456789ab",
      tenant: "northwind-test",
      kind: "user_roles",
      target: "user:puppet",
      payload: { username: "puppet", email: "p@northwind.test", name: "P", roles: ["tenant-admin"] },
      base_version: null,
      diff: [{ op: "add", path: "/roles/0", after: "tenant-admin" }],
      reasons: [{ code: "admin_role_granted", field: "user:puppet/role:tenant-admin", reason: "grants the tenant-admin role" }],
      requested_by: { sub: "sub-tina", username: "tina", name: "Tina Admin" },
      requested_at: "2026-10-08T09:00:00Z",
      status: "pending",
      approver: null,
      decided_at: null,
      decision_reason: null,
      expires_at: "2026-10-11T09:00:00Z",
    };
    expect(adminChangeRequestSchema.parse(cr).kind).toBe("user_roles");
  });
});
