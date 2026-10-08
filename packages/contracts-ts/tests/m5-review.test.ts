import { describe, expect, it } from "vitest";
import { approvalCardPropsSchema, parseBlockProps, remediationChecklistPropsSchema } from "../src";

// Review regressions for the M5 contracts (mirror packages/contracts-py/tests/test_m5_review.py).

const HASH = "d1bbcbc4cb6b0706ba28795e350e70f90c2564efa920a0cdadbc1028667b83bc";

const step = (deep_link: string | null) => ({
  seq: 1,
  title: "Create transfer order DE1 → NL1",
  instructions_md: "Open **Transfer orders → New** and save.",
  deep_link,
  expected: { entity_type: "transfer_order", entity_ref: null, match: { from_dc: "DE1" } },
  status: "pending",
  verified_at: null,
  evidence_id: null,
});
const checklist = (deep_link: string | null) => ({
  checklist_id: "chk_case1_v1_opt2",
  connector: "merxa",
  steps: [step(deep_link)],
  progress: { done: 0, total: 1 },
});

describe("remediation_checklist@1 deep links are safe to render as href", () => {
  it.each([
    "http://merxa.localhost/transfer-orders/new?from=DE1&to=NL1&lines=SKU-1:120",
    "https://merxa.localhost:8443/po-lines/PO-1/2?edit=qty",
    "/guide/allocations/ALLOC-7",
    "/",
  ])("accepts %s", (link) => {
    expect(remediationChecklistPropsSchema.safeParse(checklist(link)).success).toBe(true);
  });

  it.each([
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "//evil.example/x",
    "/\\evil.example",
    "http://user@merxa.localhost/x",
    "http://merxa.localhost/a b",
    "http://merxa.localhost/a\nb",
    " http://merxa.localhost/",
    "ftp://merxa.localhost/x",
    "",
  ])("rejects %j", (link) => {
    expect(remediationChecklistPropsSchema.safeParse(checklist(link)).success).toBe(false);
    expect(parseBlockProps({ kind: "remediation_checklist", v: 1, props: checklist(link) }).ok).toBe(false);
  });

  it("allows a step without a link", () => {
    expect(remediationChecklistPropsSchema.safeParse(checklist(null)).success).toBe(true);
  });
});

describe("approval_card@1 rejection reason is bounded", () => {
  const card = {
    interrupt_id: "approval-cs_01-1",
    change_set_id: "cs_01",
    change_set_hash: HASH,
    plan_version: 1,
    summary: { lines: 1, units: 520, value_cents: 1_300_000 },
    reason: "Includes a new purchase order",
    required_roles: ["approver"],
    connect: { status: "active", connect_url: null },
    buttons: ["approve", "reject", "revise"],
  };
  const rejection = (reason: string | null) => ({
    sub: "u-lena", name: "Lena Vogel", role: "compliance", at: "2026-10-07T10:05:00Z", reason,
  });

  it("accepts up to 500 characters and rejects empty or longer notes", () => {
    expect(approvalCardPropsSchema.safeParse({ ...card, rejection: rejection("x".repeat(500)) }).success).toBe(true);
    expect(approvalCardPropsSchema.safeParse({ ...card, rejection: rejection(null) }).success).toBe(true);
    expect(approvalCardPropsSchema.safeParse({ ...card, rejection: rejection("") }).success).toBe(false);
    expect(approvalCardPropsSchema.safeParse({ ...card, rejection: rejection("x".repeat(501)) }).success).toBe(false);
  });
});
