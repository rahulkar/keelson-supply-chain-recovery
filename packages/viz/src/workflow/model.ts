/**
 * The engine's workflow topology (`docs/contracts/50-engine-workflow.md` "M4 graph", the
 * authoritative one) as a read-only graph for the admin Agents page (`docs/contracts/110` §3):
 * the fixed code nodes plus the tenant's specialists (built-in and custom) fanned out under
 * `investigate`. Pure data — `WorkflowGraph` draws it, `WorkflowOutline` lists it.
 */
import type { StageId } from "@keelson/contracts";

export type WorkflowNodeKind = "entry" | "llm" | "fn" | "gate" | "specialist";

export type WorkflowNode = {
  id: string;
  label: string;
  kind: WorkflowNodeKind;
  /** Stage chip the node reports (`metadata.sc.stage`); `null` for nodes outside a stage. */
  stage: StageId | null;
  /** Off: a disabled specialist or a stage switched off in the agent settings. */
  enabled: boolean;
  /** One short line under the label ("Custom · reads merxa-lite (3 tools)"). */
  detail: string | null;
  /** `answer` (direct questions), `investigation`, `remediation`. */
  lane: "answer" | "investigation" | "remediation";
  custom?: boolean | undefined;
};

export type WorkflowEdge = { source: string; target: string; label?: string | undefined; dashed?: boolean | undefined };

export type WorkflowModel = { nodes: WorkflowNode[]; edges: WorkflowEdge[] };

export type WorkflowSpecialist = {
  slug: string;
  label: string;
  /** Tenant-defined (`custom_specialists`) rather than built in. */
  custom: boolean;
  enabled: boolean;
  connectors: readonly string[];
  tools: number;
  knowledgeBases?: number | undefined;
};

export type WorkflowInput = {
  specialists: readonly WorkflowSpecialist[];
  /** Stage toggles from the agent settings (`false` = switched off); unknown keys are ignored. */
  stages?: Readonly<Record<string, boolean>> | null | undefined;
};

type Fixed = Omit<WorkflowNode, "enabled" | "detail"> & { detail?: string | undefined; toggle?: string | undefined };

/** The fixed nodes in pipeline order (ids are the engine's node names). */
export const FIXED_NODES: readonly Fixed[] = [
  { id: "start", label: "Message or action", kind: "entry", stage: null, lane: "investigation" },
  { id: "intake", label: "Intake", kind: "llm", stage: "understanding", lane: "investigation", detail: "Understands the request" },
  { id: "answer_investigator", label: "Answer investigator", kind: "llm", stage: "checking_systems", lane: "answer", detail: "Reads facts for direct questions" },
  { id: "answer_structurer", label: "Answer structurer", kind: "llm", stage: "checking_systems", lane: "answer" },
  { id: "compose_answer", label: "Compose answer", kind: "fn", stage: "ready_for_review", lane: "answer", detail: "Numbers rule applied" },
  { id: "frame_case", label: "Frame the case", kind: "fn", stage: "understanding", lane: "investigation", detail: "Matches records, builds the scenario" },
  { id: "quick_exposure", label: "Quick exposure", kind: "fn", stage: "measuring_impact", lane: "investigation", detail: "Optimizer: first impact estimate" },
  { id: "planner", label: "Planner", kind: "llm", stage: "understanding", lane: "investigation", detail: "Chooses the specialists to consult" },
  { id: "approach_gate", label: "Approach card", kind: "gate", stage: "understanding", lane: "investigation", detail: "Confirm, edit or what-if" },
  { id: "investigate", label: "Investigate", kind: "fn", stage: "checking_systems", lane: "investigation", detail: "Runs the planned checks in parallel" },
  { id: "verify_numbers", label: "Verify numbers", kind: "fn", stage: "checking_systems", lane: "investigation", detail: "Every number must trace to evidence" },
  { id: "critic", label: "Critic", kind: "llm", stage: "checking_systems", lane: "investigation", detail: "Independent second look", toggle: "critic" },
  { id: "refine_scenario", label: "Refine scenario", kind: "fn", stage: "measuring_impact", lane: "investigation" },
  { id: "impact", label: "Impact", kind: "fn", stage: "measuring_impact", lane: "investigation", detail: "Optimizer" },
  { id: "simulate", label: "Simulate options", kind: "fn", stage: "building_options", lane: "investigation", detail: "Optimizer" },
  { id: "recommend", label: "Strategist", kind: "llm", stage: "building_options", lane: "investigation", detail: "Explains the options" },
  { id: "compose", label: "Compose", kind: "fn", stage: "ready_for_review", lane: "investigation", detail: "Numbers rule applied" },
  { id: "compile_changeset", label: "Compile changes", kind: "fn", stage: "preparing_changes", lane: "remediation" },
  { id: "dry_run", label: "Dry run", kind: "fn", stage: "preparing_changes", lane: "remediation", detail: "Validated, never applied" },
  { id: "approval_gate", label: "Approval", kind: "gate", stage: "awaiting_approval", lane: "remediation", detail: "Policy decides who approves" },
  { id: "execute", label: "Apply", kind: "fn", stage: "applying", lane: "remediation" },
  { id: "reconcile", label: "Read back and verify", kind: "fn", stage: "verified", lane: "remediation" },
];

const CHAIN: readonly (readonly [string, string, string?])[] = [
  ["start", "intake"],
  ["intake", "answer_investigator", "Question"],
  ["answer_investigator", "answer_structurer"],
  ["answer_structurer", "compose_answer"],
  ["intake", "frame_case", "New disruption"],
  ["frame_case", "quick_exposure"],
  ["quick_exposure", "planner"],
  ["planner", "approach_gate"],
  ["approach_gate", "investigate", "Confirmed"],
  ["verify_numbers", "critic"],
  ["critic", "refine_scenario"],
  ["refine_scenario", "impact"],
  ["impact", "simulate"],
  ["simulate", "recommend"],
  ["recommend", "compose"],
  ["compile_changeset", "dry_run"],
  ["dry_run", "approval_gate"],
  ["approval_gate", "execute", "Approved"],
  ["execute", "reconcile"],
];

export const WORKFLOW_KIND_LABELS: Record<WorkflowNodeKind, string> = {
  entry: "Start",
  llm: "AI agent",
  fn: "Deterministic step",
  gate: "Waits for a person",
  specialist: "Specialist",
};

function specialistDetail(s: WorkflowSpecialist): string {
  const parts: string[] = [s.custom ? "Custom" : "Built in"];
  if (s.connectors.length) parts.push(`reads ${s.connectors.join(", ")} (${s.tools} ${s.tools === 1 ? "tool" : "tools"})`);
  else if (s.tools) parts.push(`${s.tools} ${s.tools === 1 ? "tool" : "tools"}`);
  if (s.knowledgeBases) parts.push(`${s.knowledgeBases} knowledge ${s.knowledgeBases === 1 ? "base" : "bases"}`);
  return parts.join(" · ");
}

/** Node id of a specialist (never collides with a fixed node: fixed ids have no `spec:` prefix). */
export function specialistNodeId(slug: string): string {
  return `spec:${slug}`;
}

/**
 * The graph for one tenant: fixed nodes, every specialist between `investigate` and
 * `verify_numbers` (disabled ones dimmed with a dashed edge), and the remediation path entered
 * from `compose` by "Apply changes for me". A stage toggled off is dimmed too.
 */
export function buildWorkflowModel(input: WorkflowInput): WorkflowModel {
  const stages = input.stages ?? {};
  const nodes: WorkflowNode[] = FIXED_NODES.map(({ toggle, ...n }) => ({
    ...n,
    detail: n.detail ?? null,
    enabled: toggle === undefined || stages[toggle] !== false,
  }));
  const edges: WorkflowEdge[] = CHAIN.map(([source, target, label]) => ({ source, target, ...(label ? { label } : {}) }));
  const seen = new Set<string>();
  for (const s of input.specialists) {
    if (seen.has(s.slug)) continue;
    seen.add(s.slug);
    const id = specialistNodeId(s.slug);
    nodes.push({ id, label: s.label || s.slug, kind: "specialist", stage: "checking_systems", lane: "investigation", enabled: s.enabled, detail: specialistDetail(s), custom: s.custom });
    edges.push({ source: "investigate", target: id, dashed: !s.enabled }, { source: id, target: "verify_numbers", dashed: !s.enabled });
  }
  if (seen.size === 0) edges.push({ source: "investigate", target: "verify_numbers" });
  edges.push({ source: "compose", target: "compile_changeset", label: "Apply changes", dashed: true });
  return { nodes, edges };
}

/** "Planner, AI agent" / "Supplier risk analyst, Specialist, custom, disabled" for screen readers. */
export function workflowNodeAriaLabel(n: WorkflowNode): string {
  const parts = [n.label, WORKFLOW_KIND_LABELS[n.kind]];
  if (n.custom) parts.push("custom");
  if (!n.enabled) parts.push(n.kind === "specialist" ? "disabled" : "switched off");
  if (n.detail) parts.push(n.detail);
  return parts.join(", ");
}
