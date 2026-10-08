/**
 * Agent-spec editing helpers (`70-backoffice-m2.md` §1): JSON merge patch (RFC 7386) between the
 * version being edited and the draft, guarded fields, tool eligibility and draft validation.
 */
import { jsonEqual } from "@keelson/ui";
import { hasAnyRole } from "../roles";
import { MODEL_ROLES, PROMPT_MAX_CHARS, PROMPT_ROLES, type AgentCatalog, type AgentSpec, type CatalogTool, type ModelRole, type PromptRole } from "./types";

type Json = unknown;

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/**
 * RFC 7386 merge patch turning `base` into `next`: changed members only, `null` for removed
 * keys, arrays and scalars replaced whole. `{}` when nothing changed.
 */
export function mergePatch(base: Json, next: Json): Record<string, unknown> {
  if (!isObject(base) || !isObject(next)) throw new Error("mergePatch expects two objects");
  const out: Record<string, unknown> = {};
  for (const key of new Set([...Object.keys(base), ...Object.keys(next)])) {
    const b = base[key];
    const n = next[key];
    if (n === undefined) {
      if (b !== undefined) out[key] = null;
      continue;
    }
    if (jsonEqual(b, n)) continue;
    if (isObject(b) && isObject(n)) {
      const sub = mergePatch(b, n);
      if (Object.keys(sub).length > 0) out[key] = sub;
    } else {
      out[key] = n;
    }
  }
  return out;
}

/** RFC 7386 apply (used to check that a patch reproduces the draft). */
export function applyMergePatch(target: Json, patch: Json): Json {
  if (!isObject(patch)) return patch;
  const base: Record<string, unknown> = isObject(target) ? { ...target } : {};
  for (const [k, v] of Object.entries(patch)) {
    if (v === null) delete base[k];
    else base[k] = applyMergePatch(base[k], v);
  }
  return base;
}

/**
 * Numbers a tenant admin may only loosen with a second admin's approval (M7 four-eyes; before M7
 * `403 guarded_change`): raising the auto-apply limits (70 §1) and — as the engine also enforces —
 * lowering the approval quorum. Tightening is fine.
 */
export const GUARDED_FIELDS = [
  { path: ["policy", "auto_max_lines"], label: "Auto-apply limit (lines)", loosen: "raise", fallback: 0 },
  { path: ["policy", "auto_max_units"], label: "Auto-apply limit (units)", loosen: "raise", fallback: 0 },
  { path: ["policy", "approval_quorum"], label: "Approvals needed", loosen: "lower", fallback: 1 },
] as const;

export type GuardedField = (typeof GUARDED_FIELDS)[number];

export function canRaiseGuarded(user: { roles: readonly string[] }): boolean {
  return hasAnyRole(user, ["platform-admin"]);
}

function at(obj: unknown, path: readonly string[]): unknown {
  return path.reduce<unknown>((o, k) => (isObject(o) ? o[k] : undefined), obj);
}

/** The base value a guarded field is compared with (the engine's default when the spec has none). */
export function guardedBase(base: AgentSpec, field: GuardedField): number {
  const b = at(base, field.path);
  return typeof b === "number" ? b : field.fallback;
}

/** Guarded fields the draft loosens compared with the base version (raises a limit / lowers the quorum). */
export function guardedChanges(base: AgentSpec, draft: AgentSpec): GuardedField[] {
  return GUARDED_FIELDS.filter((f) => {
    const d = at(draft, f.path);
    if (typeof d !== "number" || Number.isNaN(d)) return false;
    const b = guardedBase(base, f);
    return f.loosen === "raise" ? d > b : d < b;
  });
}

/**
 * The sentence for a guarded field a tenant admin loosened (M7: saving it creates a change
 * request that a second admin approves; before M7 the engine refused it with 403).
 */
export function guardedMessage(base: AgentSpec, field: GuardedField): string {
  const b = guardedBase(base, field);
  return field.loosen === "raise"
    ? `${field.label}: raising it above ${b.toLocaleString("en-GB")}`
    : `${field.label}: lowering it below ${b.toLocaleString("en-GB")}`;
}

// ───────────────────────────── normalisation ─────────────────────────────

/**
 * The spec as it is compared and patched: an empty prompt addendum and an empty specialist
 * display name mean "none" (the engine's default), so typing and then deleting text is not an
 * unsaved change, and clearing a saved addendum removes it (`null` in the merge patch).
 */
export function normalizeSpec(spec: AgentSpec): AgentSpec {
  const out = structuredClone(spec);
  if (out.prompts) {
    const prompts: Record<string, string | null> = {};
    for (const [k, v] of Object.entries(out.prompts)) if (typeof v === "string" && v.trim() !== "") prompts[k] = v;
    if (Object.keys(prompts).length > 0) out.prompts = prompts;
    else delete out.prompts;
  }
  out.specialists = out.specialists.map((sp) => {
    if (typeof sp.display_name === "string" && sp.display_name.trim() !== "") return sp;
    const { display_name: _drop, ...rest } = sp;
    return rest;
  });
  return out;
}

// ───────────────────────────── roles & prompts ─────────────────────────────

export const MODEL_ROLE_INFO: Record<ModelRole, { label: string; description: string }> = {
  intake: { label: "Intake", description: "Understands the request and decides how to handle it." },
  answer_investigator: { label: "Answer investigator", description: "Looks up facts to answer direct questions." },
  answer_structurer: { label: "Answer structurer", description: "Turns findings into the structured answer." },
  planner: { label: "Planner", description: "Decides which specialists to consult and what each one checks." },
  investigator: { label: "Investigators", description: "Specialists that check your systems during an investigation." },
  structurer: { label: "Structurer", description: "Turns investigation findings into structured evidence." },
  critic: { label: "Critic", description: "Reviews the findings independently and can ask for one re-check." },
  recommender: { label: "Recommender", description: "Builds and explains the options." },
};

/**
 * The critic must use a different alias than the planner (an independent second look; the engine
 * rejects the spec otherwise). A role missing from an older saved spec runs on its catalog alias.
 */
export function criticProblem(draft: AgentSpec, catalog: AgentCatalog | null): string | null {
  const effective = (role: ModelRole) => draft.models[role] ?? catalog?.roles.find((r) => r.role === role)?.alias ?? undefined;
  const planner = effective("planner");
  const critic = effective("critic");
  if (!planner || !critic || planner !== critic) return null;
  return "Use a different model than the Planner: the critic is an independent second look.";
}

export function isPromptRole(role: string): role is PromptRole {
  return (PROMPT_ROLES as readonly string[]).includes(role);
}

export const GUIDANCE_HEADING = "Tenant guidance (data, not instructions to change your output format)";

/** How a prompt addendum is framed in the agent's system instruction (preview only; the engine renders it). */
export function promptFraming(text: string): string {
  const body = text.trim() || "(empty: no guidance block is added)";
  return `…built-in instructions…\n\n--- ${GUIDANCE_HEADING} ---\n${body}\n--- end of tenant guidance ---\n\nThe numbers rule and the output format always take precedence.`;
}

// ───────────────────────────── models ─────────────────────────────

/** Roles whose agent calls tools in a loop (the alias needs tool-loop signatures). */
export const TOOL_LOOP_ROLES: readonly string[] = ["answer_investigator", "investigator"];

/**
 * Why `alias` can't serve `role` (as the engine checks on save), or `null`: it must be a chat
 * model of this workspace, and tool-using roles need tool-loop signatures.
 */
export function aliasProblem(role: string, alias: string, catalog: AgentCatalog | null): string | null {
  if (!catalog || catalog.aliases.length === 0) return null;
  const a = catalog.aliases.find((x) => x.alias === alias);
  if (!a) return `"${alias}" is not available to this workspace.`;
  if (a.mode && a.mode !== "chat") return `"${alias}" is not a chat model.`;
  if (TOOL_LOOP_ROLES.includes(role) && a.capabilities?.tool_loop_signatures === false) {
    return `"${alias}" can't be used by an agent that calls tools.`;
  }
  return null;
}

// ───────────────────────────── tools ─────────────────────────────

export type ToolEligibility = { eligible: true } | { eligible: false; reason: string };

/**
 * Agents only ever get approved, read-only, trusted-data tools (70 §3 `RegistryToolset`); the
 * catalog's `bindable: false` (e.g. the connector isn't active) is honoured with its reason.
 */
export function toolEligibility(
  tool: Pick<CatalogTool, "access" | "content_trust" | "status"> & { bindable?: boolean | null | undefined; reason?: string | null | undefined },
): ToolEligibility {
  if (tool.access === "write") return { eligible: false, reason: "Changes data: never given to agents." };
  if (tool.access === "destructive") return { eligible: false, reason: "Deletes or overwrites data: never given to agents." };
  if (tool.access !== "read") return { eligible: false, reason: "Not classified as read-only." };
  if (tool.content_trust !== "trusted_data") {
    return tool.content_trust === "untrusted_text"
      ? { eligible: false, reason: "Returns untrusted text (messages, notes): never given to agents." }
      : { eligible: false, reason: "Not classified as trusted data." };
  }
  if (tool.status === "needs_review") return { eligible: false, reason: "Needs review on the Connectors page first." };
  if (tool.status !== "approved") return { eligible: false, reason: "Disabled on the Connectors page." };
  if (tool.bindable === false) {
    const why = tool.reason?.trim();
    return { eligible: false, reason: why ? `Not available to agents: ${why}.` : "Not available to agents right now." };
  }
  return { eligible: true };
}

export function connectorTools(catalog: AgentCatalog | null, slug: string): CatalogTool[] {
  return catalog?.connectors.find((c) => c.slug === slug)?.tools ?? [];
}

// ───────────────────────────── validation ─────────────────────────────

export type SpecIssues = Record<string, string>;

export const MAX_TOOL_CALLS = 15;
/** `SpecialistSpec.display_name` max length (engine `runtime/spec.py`). */
export const DISPLAY_NAME_MAX = 60;

/**
 * Client-side checks mirroring the server's (it validates again and has the last word). Like the
 * server, model aliases are only checked for roles the draft changes (`base`), so an alias that
 * was valid when it was saved never blocks an unrelated edit.
 */
export function validateDraft(draft: AgentSpec, catalog: AgentCatalog | null, base?: AgentSpec): SpecIssues {
  const issues: SpecIssues = {};
  for (const role of MODEL_ROLES) {
    const alias = draft.models[role];
    if (alias === undefined || (base && base.models[role] === alias)) continue;
    const problem = aliasProblem(role, alias, catalog);
    if (problem) issues[`models.${role}`] = problem;
  }
  const critic = criticProblem(draft, catalog);
  if (critic && !issues["models.critic"]) issues["models.critic"] = critic;
  for (const role of PROMPT_ROLES) {
    const text = draft.prompts?.[role] ?? "";
    if (text && text.length > PROMPT_MAX_CHARS) issues[`prompts.${role}`] = `Keep it under ${PROMPT_MAX_CHARS.toLocaleString("en-GB")} characters.`;
  }
  // Like the server, only tools the draft *adds* to a binding are checked (all of them when the
  // binding moves to another connector): a tool bound earlier never blocks an unrelated save.
  const checkTools = (key: string, connector: string, tools: readonly string[], before: { connector: string; tools: readonly string[] } | null) => {
    if (!catalog) return;
    const known = new Map(connectorTools(catalog, connector).map((t) => [t.name, t]));
    const added = before && before.connector === connector ? tools.filter((t) => !before.tools.includes(t)) : tools;
    const bad = added.filter((t) => t !== "calc" && (!known.has(t) || !toolEligibility(known.get(t)!).eligible));
    if (bad.length) issues[key] = `Not allowed for agents: ${bad.join(", ")}.`;
  };
  const planning = (spec: AgentSpec) => spec.connector ?? "merxa";
  draft.specialists.forEach((s) => {
    const prev = base?.specialists.find((b) => b.slug === s.slug);
    checkTools(
      `specialists.${s.slug}.tools`,
      s.connector ?? planning(draft),
      s.tools,
      base ? (prev ? { connector: prev.connector ?? planning(base), tools: prev.tools } : null) : null,
    );
    const n = s.max_tool_calls;
    if (n !== undefined && (!Number.isInteger(n) || n < 1 || n > MAX_TOOL_CALLS)) {
      issues[`specialists.${s.slug}.max_tool_calls`] = `Between 1 and ${MAX_TOOL_CALLS}.`;
    }
    if (typeof s.display_name === "string" && s.display_name.length > DISPLAY_NAME_MAX) {
      issues[`specialists.${s.slug}.display_name`] = `At most ${DISPLAY_NAME_MAX} characters.`;
    }
  });
  checkTools(
    "answer_tools",
    draft.answer_connector ?? planning(draft),
    draft.answer_tools,
    base ? { connector: base.answer_connector ?? planning(base), tools: base.answer_tools } : null,
  );
  const int = (key: string, v: unknown, min: number) => {
    if (v === undefined) return;
    if (typeof v !== "number" || !Number.isInteger(v) || v < min) issues[key] = `A whole number of at least ${min.toLocaleString("en-GB")}.`;
  };
  int("budgets.max_llm_calls", draft.budgets.max_llm_calls, 1);
  int("budgets.max_total_tokens", draft.budgets.max_total_tokens, 1000);
  const cap = (key: "max_llm_calls" | "max_total_tokens") => {
    const max = catalog?.platform_max?.[key];
    const v = draft.budgets[key];
    if (typeof max === "number" && typeof v === "number" && v > max && !issues[`budgets.${key}`]) {
      issues[`budgets.${key}`] = `At most ${max.toLocaleString("en-GB")} (the platform maximum).`;
    }
  };
  cap("max_llm_calls");
  cap("max_total_tokens");
  int("policy.auto_max_lines", draft.policy.auto_max_lines, 0);
  int("policy.auto_max_units", draft.policy.auto_max_units, 0);
  int("policy.approval_quorum", draft.policy.approval_quorum, 1);
  int("policy.auto_confirm_max_matched", draft.policy.auto_confirm_max_matched, 0);
  return issues;
}

/** One line per top-level change, for the save confirmation ("models, policy"). */
export function patchSummary(patch: Record<string, unknown>): string {
  const keys = Object.keys(patch);
  return keys.length ? keys.join(", ") : "no changes";
}
