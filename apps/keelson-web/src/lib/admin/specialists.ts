/**
 * Custom specialists (`docs/contracts/110` §3): tenant-defined investigators saved into the
 * versioned AgentSpec (`custom_specialists`). The checks mirror contracts-py
 * `agents.CustomSpecialistSpec`, `custom_specialist_violations` and
 * `custom_specialists_violations`; the engine validates again on save (422) and has the last word.
 */
import { AGENT_DISPLAY_NAMES, agentsCustomSpecialistSchema, isProModel, type AgentsCustomSpecialist } from "@keelson/contracts";
import type { AgentCatalog, AgentSpec, CatalogTool } from "./types";

export type CustomSpecialist = AgentsCustomSpecialist;

export const MAX_CUSTOM_SPECIALISTS = 8;
export const MAX_SPECIALIST_TOOLS = 15;
export const MAX_SPECIALIST_KBS = 10;
export const MAX_SPECIALIST_CONNECTORS = 3;
export const MAX_ROLE_PROMPT_CHARS = 4000;
export const MAX_DESCRIPTION_CHARS = 300;
export const MAX_DISPLAY_NAME_CHARS = 60;
export const SLUG_PATTERN = /^[a-z][a-z0-9_]{2,39}$/;

/** Built-in agent roles a custom specialist may not shadow (contracts-py `RESERVED_SPECIALIST_SLUGS`). */
export const RESERVED_SLUGS: ReadonlySet<string> = new Set([
  ...Object.keys(AGENT_DISPLAY_NAMES),
  "answer",
  "answer_investigator",
  "answer_structurer",
  "investigator",
  "structurer",
  "guard",
  "extractor",
  "followup",
  "system",
  "user",
]);

const itemSchema = agentsCustomSpecialistSchema.strip();

/** The spec's saved custom specialists (malformed entries are skipped; the engine never saves them). */
export function customSpecialistsOf(spec: AgentSpec): CustomSpecialist[] {
  const raw = (spec as Record<string, unknown>)["custom_specialists"];
  if (!Array.isArray(raw)) return [];
  const out: CustomSpecialist[] = [];
  for (const item of raw) {
    const p = itemSchema.safeParse(item);
    if (p.success) out.push(p.data);
  }
  return out;
}

export function emptySpecialist(): CustomSpecialist {
  return { slug: "", display_name: "", description: "", role_prompt: "", model_alias: "", tools: [], kb_ids: [], max_tool_calls: 8, max_turns: 6, enabled: true };
}

/** "Supplier risk analyst" → "supplier_risk_analyst" (trimmed to the slug limits). */
export function slugFromName(name: string): string {
  let s = name
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_+/g, "_");
  if (s && !/^[a-z]/.test(s)) s = `s_${s}`;
  return s.slice(0, 40).replace(/_+$/, "");
}

/** Any control character (`allow` = characters that are fine, e.g. new line and tab in a prompt). */
function hasControl(text: string, allow = ""): boolean {
  for (const ch of text) {
    const c = ch.codePointAt(0)!;
    if ((c < 0x20 || c === 0x7f) && !allow.includes(ch)) return true;
  }
  return false;
}
const CONTROL = { test: (s: string) => hasControl(s) };
const PROMPT_CONTROL = { test: (s: string) => hasControl(s, "\n\t") };

export function slugProblem(slug: string): string | null {
  if (!SLUG_PATTERN.test(slug)) return "3–40 characters: lower-case letters, digits and underscores, starting with a letter.";
  if (RESERVED_SLUGS.has(slug)) return `"${slug}" is a built-in agent: choose another id.`;
  if (slug.endsWith("_investigator") || slug.endsWith("_structurer")) return "The id can't end in _investigator or _structurer.";
  if (slug.includes("__")) return "The id can't contain a double underscore.";
  return null;
}

/** A catalog tool's egress (top level or nested classification). */
export function egressOf(t: CatalogTool): string | undefined {
  return t.egress ?? (t.classification?.egress as string | undefined) ?? undefined;
}

export type PickerTool = {
  connector: string;
  connectorName: string;
  connectorActive: boolean | null;
  tool: CatalogTool;
  /** Why it can't be picked (`null` = it can). */
  blocked: string | null;
  /** Untrusted text: reached only through the quarantine (structured extraction, never raw text). */
  quarantined: boolean;
  external: boolean;
};

/**
 * Every tool of every connector (incl. the Signal Hub) for the picker. Custom specialists get
 * approved **read** tools only; `untrusted_text` tools are allowed and always wrapped in the
 * quarantine; a connector that is not active can't be used.
 */
export function pickerTools(catalog: AgentCatalog): PickerTool[] {
  const out: PickerTool[] = [];
  for (const c of catalog.connectors) {
    const status = typeof (c as Record<string, unknown>)["status"] === "string" ? String((c as Record<string, unknown>)["status"]) : null;
    const active = status === null ? null : status === "active";
    for (const t of c.tools) {
      let blocked: string | null = null;
      if (t.access === "write") blocked = "Changes data: never given to agents.";
      else if (t.access === "destructive") blocked = "Deletes or overwrites data: never given to agents.";
      else if (t.access !== "read") blocked = "Not classified as read-only.";
      else if (t.status === "needs_review") blocked = "Needs review on the Connectors page first.";
      else if (t.status !== "approved") blocked = "Disabled on the Connectors page.";
      else if (active === false) blocked = "The connector isn't active.";
      out.push({
        connector: c.slug,
        connectorName: c.name || c.slug,
        connectorActive: active,
        tool: t,
        blocked,
        quarantined: t.content_trust === "untrusted_text",
        external: egressOf(t) === "external",
      });
    }
  }
  return out;
}

export type AliasOption = { value: string; label: string; disabled: boolean; reason: string | null };

/** Chat aliases of the workspace; Pro models are listed but can't be picked (cost rule, enforced by the API too). */
export function aliasOptions(catalog: AgentCatalog): AliasOption[] {
  return catalog.aliases.map((a) => {
    const provider = (a as Record<string, unknown>)["provider_model"];
    const pro = isProModel(a.alias) || (typeof provider === "string" && isProModel(provider));
    const notChat = a.mode !== null && a.mode !== "chat";
    const noTools = a.capabilities?.tool_loop_signatures === false;
    const reason = pro ? "Pro models aren't allowed" : notChat ? "not a chat model" : noTools ? "can't call tools" : null;
    return { value: a.alias, label: reason ? `${a.alias}: ${reason}` : a.mode ? `${a.alias} (${a.mode})` : a.alias, disabled: reason !== null, reason };
  });
}

export type SpecialistIssues = Partial<Record<"slug" | "display_name" | "description" | "role_prompt" | "model_alias" | "tools" | "kb_ids" | "max_tool_calls" | "max_turns" | "list", string>>;

/**
 * Field problems of one draft against the saved list (`others` = every other custom specialist),
 * the built-in specialists' slugs, the catalog and the knowledge bases. Only what the engine
 * would refuse; empty = can be saved.
 */
export function specialistIssues(
  d: CustomSpecialist,
  ctx: { others: readonly CustomSpecialist[]; builtInSlugs: readonly string[]; catalog: AgentCatalog | null; kbIds: readonly string[] | null; isNew: boolean; savedAlias?: string | undefined },
): SpecialistIssues {
  const issues: SpecialistIssues = {};
  const slug = slugProblem(d.slug);
  if (slug) issues.slug = slug;
  else if (ctx.isNew && (ctx.others.some((o) => o.slug === d.slug) || ctx.builtInSlugs.includes(d.slug))) issues.slug = "Another specialist already uses this id.";

  const name = d.display_name;
  if (!name.trim()) issues.display_name = "Enter a display name.";
  else if (name.length > MAX_DISPLAY_NAME_CHARS) issues.display_name = `At most ${MAX_DISPLAY_NAME_CHARS} characters.`;
  else if (CONTROL.test(name) || name !== name.trim()) issues.display_name = "One line, without leading or trailing spaces.";
  else if (ctx.others.some((o) => o.display_name.toLocaleLowerCase() === name.toLocaleLowerCase())) issues.display_name = "Another specialist already has this name.";

  if (!d.description.trim()) issues.description = "Say what it checks: the planner reads this to decide when to ask it.";
  else if (d.description.length > MAX_DESCRIPTION_CHARS) issues.description = `At most ${MAX_DESCRIPTION_CHARS} characters.`;
  else if (CONTROL.test(d.description) || d.description !== d.description.trim()) issues.description = "One line, without leading or trailing spaces.";

  if (!d.role_prompt.trim()) issues.role_prompt = "Write the specialist's instructions.";
  else if (d.role_prompt.length > MAX_ROLE_PROMPT_CHARS) issues.role_prompt = `Keep it under ${MAX_ROLE_PROMPT_CHARS.toLocaleString("en-GB")} characters.`;
  else if (PROMPT_CONTROL.test(d.role_prompt)) issues.role_prompt = "Remove control characters (only new lines and tabs are allowed).";

  if (!d.model_alias) issues.model_alias = "Choose a model.";
  else if (isProModel(d.model_alias)) issues.model_alias = "Pro models aren't allowed for custom specialists: choose a Flash or Flash-Lite alias.";
  else if (ctx.catalog && ctx.catalog.gateway_available !== false && d.model_alias !== ctx.savedAlias) {
    const opt = aliasOptions(ctx.catalog).find((o) => o.value === d.model_alias);
    if (!opt) issues.model_alias = `"${d.model_alias}" is not available to this workspace.`;
    else if (opt.disabled) issues.model_alias = `"${d.model_alias}": ${opt.reason}.`;
  }

  const tools = d.tools ?? [];
  const kbs = d.kb_ids ?? [];
  if (tools.length === 0 && kbs.length === 0) issues.tools = "Give it at least one tool or knowledge base.";
  else if (tools.length > MAX_SPECIALIST_TOOLS) issues.tools = `At most ${MAX_SPECIALIST_TOOLS} tools.`;
  else if (new Set(tools.map((t) => `${t.connector}/${t.tool}`)).size !== tools.length) issues.tools = "Each tool once.";
  else if (new Set(tools.map((t) => t.connector)).size > MAX_SPECIALIST_CONNECTORS) issues.tools = `Tools from at most ${MAX_SPECIALIST_CONNECTORS} connectors.`;
  else if (ctx.catalog) {
    const byKey = new Map(pickerTools(ctx.catalog).map((p) => [`${p.connector}/${p.tool.name}`, p]));
    const bad = tools.filter((t) => {
      const p = byKey.get(`${t.connector}/${t.tool}`);
      return !p || p.blocked !== null;
    });
    if (bad.length) issues.tools = `Not allowed: ${bad.map((t) => `${t.connector}/${t.tool}`).join(", ")}.`;
    else {
      const picked = tools.map((t) => byKey.get(`${t.connector}/${t.tool}`)!);
      if (picked.some((p) => p.quarantined) && picked.some((p) => p.external)) {
        issues.tools = "Untrusted-text tools can't be combined with tools that send data outside.";
      }
    }
  }

  if (kbs.length > MAX_SPECIALIST_KBS) issues.kb_ids = `At most ${MAX_SPECIALIST_KBS} knowledge bases.`;
  else if (ctx.kbIds) {
    const missing = kbs.filter((k) => !ctx.kbIds!.includes(k));
    if (missing.length) issues.kb_ids = "A chosen knowledge base no longer exists: remove it.";
  }

  if (!Number.isInteger(d.max_tool_calls) || d.max_tool_calls < 1 || d.max_tool_calls > MAX_SPECIALIST_TOOLS) issues.max_tool_calls = `Between 1 and ${MAX_SPECIALIST_TOOLS}.`;
  if (!Number.isInteger(d.max_turns) || d.max_turns < 1 || d.max_turns > 10) issues.max_turns = "Between 1 and 10.";
  if (ctx.isNew && ctx.others.length >= MAX_CUSTOM_SPECIALISTS) issues.list = `At most ${MAX_CUSTOM_SPECIALISTS} custom specialists.`;
  return issues;
}

/** The draft as saved: trimmed text, contract field order. */
export function normalizeSpecialist(d: CustomSpecialist): CustomSpecialist {
  return {
    slug: d.slug,
    display_name: d.display_name.trim(),
    description: d.description.trim(),
    role_prompt: d.role_prompt,
    model_alias: d.model_alias,
    tools: [...(d.tools ?? [])],
    kb_ids: [...(d.kb_ids ?? [])],
    max_tool_calls: d.max_tool_calls,
    max_turns: d.max_turns,
    enabled: d.enabled,
  };
}

/** The whole list after adding / replacing / removing one (for the spec's merge patch: arrays are replaced whole). */
export function withSpecialist(list: readonly CustomSpecialist[], next: CustomSpecialist | null, slug: string): CustomSpecialist[] {
  const rest = list.filter((s) => s.slug !== slug);
  if (!next) return rest;
  const i = list.findIndex((s) => s.slug === slug);
  const out = [...rest];
  out.splice(i < 0 ? out.length : i, 0, normalizeSpecialist(next));
  return out;
}
