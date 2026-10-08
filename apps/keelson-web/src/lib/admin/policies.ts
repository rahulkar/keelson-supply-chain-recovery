/**
 * Approval policy rules for the admin **Policies** page (M5, `docs/contracts/95` §1):
 * response shapes of `GET/PUT /api/admin/policies` and `POST /api/admin/policies/test`, a faithful
 * port of the engine's guard check (`core/policies.py` `guarded_changes`, `condition_change`,
 * `requirement_at_least`) so a tenant admin sees the lock before saving, CEL syntax hints over the
 * typed policy context, and sample contexts for the test bench. The engine stays the authority:
 * it compiles every rule on save (422 `policy_invalid`) and refuses guarded changes (403).
 */
import { policyContextSchema, type PolicyContext } from "@keelson/contracts";
import { z } from "zod";

// ───────────────────────────── shapes ─────────────────────────────

export const ROLE_NAME = /^[a-z][a-z0-9_-]*$/;
export const RULE_ID = /^[a-z][a-z0-9_-]*$/;
export const MAX_RULES = 50;
export const MAX_WHEN = 2000;
export const MAX_QUORUM = 10;

export const requirementSchema = z
  .object({ roles: z.array(z.string()), count: z.number().int(), exclude_initiator: z.boolean() })
  .loose();
export type Requirement = { roles: string[]; count: number; exclude_initiator: boolean };

export const policyRuleViewSchema = z
  .object({
    id: z.string(),
    description: z.string(),
    when: z.string(),
    require: z.union([z.enum(["auto", "refuse"]), requirementSchema]),
    priority: z.number().int(),
  })
  .loose();
export type PolicyRuleView = { id: string; description: string; when: string; require: Requirement | "auto" | "refuse"; priority: number };

export const policySetViewSchema = z.object({ version: z.string(), rules: z.array(policyRuleViewSchema) }).loose();
export type PolicySetView = { version: string; rules: PolicyRuleView[] };

export const policiesEnvelopeSchema = z
  .object({
    spec_version: z.string(),
    policy: policySetViewSchema,
    source: z.string().optional(),
    editable: z.boolean().optional(),
    evaluator_available: z.boolean().optional(),
    fallback: requirementSchema.optional().catch(undefined),
    defaults: policySetViewSchema.optional().catch(undefined),
  })
  .loose();
export type PoliciesEnvelope = z.infer<typeof policiesEnvelopeSchema>;

export const policiesSavedSchema = z
  .object({
    spec_version: z.string(),
    policy: policySetViewSchema,
    compiled: z.boolean().optional(),
    unchanged: z.boolean().optional(),
    guarded: z.array(z.object({ field: z.string(), reason: z.string() }).loose()).optional(),
  })
  .loose();
export type PoliciesSaved = z.infer<typeof policiesSavedSchema>;

export const policyTestResultSchema = z
  .object({
    policy_version: z.string().optional(),
    decision: z
      .object({
        outcome: z.enum(["auto", "human", "refuse"]),
        rule_id: z.string().nullable(),
        requirement: requirementSchema.nullable().optional(),
        reason: z.string().optional(),
        policy_version: z.string().optional(),
      })
      .loose(),
    approved_by: z.string().nullable().optional(),
    matched_rule: policyRuleViewSchema.nullable().optional(),
    floors: z.object({ must_refuse: z.boolean().optional(), auto_allowed: z.boolean().optional() }).loose().optional(),
  })
  .loose();
export type PolicyTestResult = z.infer<typeof policyTestResultSchema>;

export function toRule(r: z.infer<typeof policyRuleViewSchema>): PolicyRuleView {
  const req = r.require;
  return {
    id: r.id,
    description: r.description,
    when: r.when,
    priority: r.priority,
    require: typeof req === "string" ? req : { roles: [...req.roles], count: req.count, exclude_initiator: req.exclude_initiator },
  };
}

export function toPolicySet(p: z.infer<typeof policySetViewSchema>): PolicySetView {
  return { version: p.version, rules: p.rules.map(toRule) };
}

export type Outcome = "auto" | "refuse" | "human";

export function outcomeOf(rule: Pick<PolicyRuleView, "require">): Outcome {
  return typeof rule.require === "string" ? rule.require : "human";
}

/** Evaluation order: ascending priority (first match wins). */
export function ordered<T extends { priority: number }>(rules: readonly T[]): T[] {
  return [...rules].sort((a, b) => a.priority - b.priority);
}

const ROLE_WORDS: Record<string, string> = {
  approver: "Approver",
  compliance: "Compliance",
  planner: "Planner",
  "tenant-admin": "Tenant admin",
  "platform-admin": "Platform admin",
};

function roleWord(r: string): string {
  return ROLE_WORDS[r] ?? r;
}

/** "Refuse (never commit)", "Apply automatically", "2 people: Approver and Compliance (not the initiator)". */
export function requirementText(require: PolicyRuleView["require"]): string {
  if (require === "refuse") return "Refuse (no change set is compiled)";
  if (require === "auto") return "Apply automatically";
  const roles = require.roles.map(roleWord);
  const list = roles.length <= 1 ? (roles[0] ?? "") : `${roles.slice(0, -1).join(", ")} ${require.count >= roles.length ? "and" : "or"} ${roles.at(-1)}`;
  const who = require.count === 1 ? `1 person: ${list}` : `${require.count} different people: ${list}`;
  return require.exclude_initiator ? `${who} (not the person who started it)` : who;
}

// ───────────────────────────── validation ─────────────────────────────

export type RuleProblem = { ruleIndex: number | null; field: string; message: string };

/** Client checks mirroring contracts `PolicyRule` / `PolicySet` (the engine re-checks and compiles). */
export function validateRules(rules: readonly PolicyRuleView[]): RuleProblem[] {
  const out: RuleProblem[] = [];
  if (rules.length === 0) out.push({ ruleIndex: null, field: "rules", message: "Keep at least one rule." });
  if (rules.length > MAX_RULES) out.push({ ruleIndex: null, field: "rules", message: `At most ${MAX_RULES} rules.` });
  const ids = new Map<string, number>();
  const priorities = new Map<number, number>();
  rules.forEach((r, i) => {
    if (!RULE_ID.test(r.id) || r.id.length > 64) out.push({ ruleIndex: i, field: "id", message: "Lower-case letters, digits, - and _ (start with a letter, at most 64)." });
    if (ids.has(r.id)) out.push({ ruleIndex: i, field: "id", message: `Another rule is already called "${r.id}".` });
    ids.set(r.id, i);
    if (!Number.isInteger(r.priority) || r.priority < 0) out.push({ ruleIndex: i, field: "priority", message: "A whole number, 0 or more." });
    if (priorities.has(r.priority)) out.push({ ruleIndex: i, field: "priority", message: `Priority ${r.priority} is used by another rule.` });
    priorities.set(r.priority, i);
    if (!r.description.trim()) out.push({ ruleIndex: i, field: "description", message: "Describe the rule in a sentence." });
    if (!r.when.trim()) out.push({ ruleIndex: i, field: "when", message: "Enter a CEL condition (true for always)." });
    if (r.when.length > MAX_WHEN) out.push({ ruleIndex: i, field: "when", message: `At most ${MAX_WHEN} characters.` });
    if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(r.when)) out.push({ ruleIndex: i, field: "when", message: "Remove control characters." });
    if (typeof r.require !== "string") {
      const q = r.require;
      if (q.roles.length < 1 || q.roles.length > MAX_QUORUM) out.push({ ruleIndex: i, field: "require", message: "Pick 1 to 10 roles." });
      if (new Set(q.roles).size !== q.roles.length) out.push({ ruleIndex: i, field: "require", message: "Each role once." });
      if (q.roles.some((role) => !ROLE_NAME.test(role) || role.length > 64)) out.push({ ruleIndex: i, field: "require", message: "Role names are lower-case slugs." });
      if (!Number.isInteger(q.count) || q.count < 1 || q.count > MAX_QUORUM) out.push({ ruleIndex: i, field: "require", message: "1 to 10 people." });
    }
  });
  return out;
}

// ───────────────────────────── guards (port of core/policies.py) ─────────────────────────────

const TOKEN_RE = /"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|\d+|[A-Za-z_][A-Za-z0-9_.]*|<=|>=|==|!=|&&|\|\||\S/g;
const CMP = new Set(["<", "<=", ">", ">="]);
const FLIP: Record<string, string> = { "<": ">", "<=": ">=", ">": "<", ">=": "<=" };
const DIGITS = /^\d+$/;

export type ConditionChange = "same" | "wider" | "narrower" | "unknown";

/** Engine `condition_change`: how a condition changed when only numbers in comparisons differ. */
export function conditionChange(old: string, next: string): ConditionChange {
  const a: string[] = old.match(TOKEN_RE) ?? [];
  const b: string[] = next.match(TOKEN_RE) ?? [];
  if (a.length === b.length && a.every((t, i) => t === b[i])) return "same";
  if (a.length !== b.length) return "unknown";
  const directions = new Set<"wider" | "narrower">();
  for (let i = 0; i < a.length; i++) {
    const x = a[i]!;
    const y = b[i]!;
    if (x === y) continue;
    if (!(DIGITS.test(x) && DIGITS.test(y))) return "unknown";
    if (a.includes("!")) return "unknown";
    const prev = i > 0 ? a[i - 1]! : "";
    const nxt = i + 1 < a.length ? a[i + 1]! : "";
    let op: string;
    if (CMP.has(prev)) op = prev;
    else if (CMP.has(nxt)) op = FLIP[nxt]!;
    else return "unknown";
    if ((i >= 2 && a[i - 1] === "-") || ["-", "+", "*", "/", "%"].includes(prev)) return "unknown";
    const grew = BigInt(y) > BigInt(x);
    directions.add(grew === (op === "<" || op === "<=") ? "wider" : "narrower");
  }
  if (directions.size !== 1) return "unknown";
  return directions.has("wider") ? "wider" : "narrower";
}

function everyRole(r: Requirement): boolean {
  return r.count >= r.roles.length;
}

/** Engine `requirement_at_least`: `a` is at least as strict as `b`. */
export function requirementAtLeast(a: Requirement, b: Requirement): boolean {
  if (a.count < b.count || (b.exclude_initiator && !a.exclude_initiator)) return false;
  const aRoles = new Set(a.roles);
  const bRoles = new Set(b.roles);
  if (everyRole(b)) return everyRole(a) && [...bRoles].every((r) => aRoles.has(r));
  return [...aRoles].every((r) => bRoles.has(r));
}

export type GuardedChange = { field: string; ruleId: string; reason: string };

/**
 * Engine `guarded_changes`: edits only a platform admin may save — anything that could make more
 * change sets auto-approved or fewer refused / approved by fewer or different people.
 */
export function guardedChanges(old: readonly PolicyRuleView[], next: readonly PolicyRuleView[]): GuardedChange[] {
  const out: GuardedChange[] = [];
  const oldBy = new Map(old.map((r) => [r.id, r]));
  const newBy = new Map(next.map((r) => [r.id, r]));
  const add = (ruleId: string, reason: string) => out.push({ field: `policy.rules.${ruleId}`, ruleId, reason });

  const newlyPreceded = (r: PolicyRuleView): PolicyRuleView[] => {
    const before = oldBy.get(r.id);
    const found: PolicyRuleView[] = [];
    for (const s of next) {
      const sOld = oldBy.get(s.id);
      if (s.id === r.id || !sOld || r.priority >= s.priority) continue;
      if (before && before.priority < sOld.priority) continue; // it already came first
      found.push(s);
    }
    return found;
  };

  for (const o of old) {
    if (!newBy.has(o.id) && outcomeOf(o) !== "auto") add(o.id, outcomeOf(o) === "refuse" ? "removes a refuse rule" : "removes an approval rule");
  }
  for (const r of next) {
    const o = oldBy.get(r.id);
    const change: ConditionChange = o ? conditionChange(o.when, r.when) : "unknown";
    const ro = outcomeOf(r);
    if (ro === "auto") {
      if (!o || outcomeOf(o) !== "auto") add(r.id, "adds an automatic-approval rule");
      else if (change === "wider" || change === "unknown") add(r.id, "raises the automatic-approval caps");
      if (newlyPreceded(r).length > 0) add(r.id, "moves an automatic-approval rule ahead of other rules");
      continue;
    }
    if (o) {
      const oo = outcomeOf(o);
      if (oo === "refuse" && ro !== "refuse") add(r.id, "turns a refuse rule into an approval rule");
      else if (oo === ro && (change === "narrower" || change === "unknown")) add(r.id, "narrows when the rule applies");
      else if (oo === "human" && ro === "human" && typeof o.require !== "string" && typeof r.require !== "string" && !requirementAtLeast(r.require, o.require)) {
        add(r.id, "weakens the approval requirement");
      }
    }
    if (ro === "human" && typeof r.require !== "string") {
      const req = r.require;
      if (o && outcomeOf(o) === "human" && change === "wider") {
        // a wider condition takes more change sets away from the stricter rules after it
        for (const s of next) {
          if (s.id === r.id || s.priority <= r.priority) continue;
          if (outcomeOf(s) === "refuse" || (typeof s.require !== "string" && !requirementAtLeast(req, s.require))) {
            add(r.id, `widens a rule that takes precedence over the stricter rule ${s.id}`);
            break;
          }
        }
      }
      for (const s of newlyPreceded(r)) {
        if (outcomeOf(s) === "refuse") add(r.id, `would take precedence over the refuse rule ${s.id}`);
        else if (typeof s.require !== "string" && !requirementAtLeast(r.require, s.require)) add(r.id, `would take precedence over the stricter rule ${s.id}`);
      }
    }
  }
  return out;
}

// ───────────────────────────── CEL hints ─────────────────────────────

/** The typed policy context (`PolicyContext`) a condition can read. */
export const CEL_FIELDS: Record<string, Record<string, { type: string; hint: string }>> = {
  cs: {
    lines: { type: "int", hint: "Change-set lines" },
    units: { type: "int", hint: "Units moved" },
    value_cents: { type: "int", hint: "Value in cents" },
    ops: { type: "list<string>", hint: 'Operations, e.g. "transfer_order.create"' },
    connectors: { type: "list<string>", hint: 'Systems changed, e.g. "merxa"' },
    incremental_cost_cents: { type: "int", hint: "Extra cost in cents" },
    lost_units_after: { type: "int", hint: "Units still lost after the plan" },
    levers: { type: "list<string>", hint: "Levers the plan uses" },
  },
  case: {
    mode: { type: "string", hint: '"actual" or "what_if"' },
    origin: { type: "string", hint: '"chat", "signal" or "webhook"' },
    tainted: { type: "bool", hint: "Untrusted input influenced the plan" },
    unconfirmed: { type: "int", hint: "Values still to confirm" },
  },
  initiator: {
    roles: { type: "list<string>", hint: 'Roles of the person who started it, e.g. "planner"' },
  },
};

const ENUM_VALUES: Record<string, readonly string[]> = { "case.mode": ["actual", "what_if"], "case.origin": ["chat", "signal", "webhook"] };
const MACROS = ["exists", "all", "exists_one", "map", "filter"];
const KEYWORDS = new Set(["true", "false", "null", "in"]);
const FUNCTIONS = new Set(["size", "has", "int", "uint", "double", "string", "bool", "matches", "startsWith", "endsWith", "contains", ...MACROS]);

/** Non-blocking hints for a CEL condition (syntax slips and unknown fields). The engine compiles it on save. */
export function celHints(expr: string): string[] {
  const hints: string[] = [];
  const text = expr.trim();
  if (!text) return ["Enter a condition, e.g. cs.lines <= 10 (or true to always match)."];
  // strings
  const stripped = text.replace(/"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, '""');
  if (/["']/.test(stripped.replace(/""/g, ""))) hints.push("A string is not closed: check the quotes.");
  // brackets
  const pairs: Record<string, string> = { ")": "(", "]": "[" };
  const stack: string[] = [];
  for (const ch of stripped) {
    if (ch === "(" || ch === "[") stack.push(ch);
    else if (ch === ")" || ch === "]") {
      if (stack.pop() !== pairs[ch]) {
        hints.push(`Unbalanced "${ch}".`);
        stack.length = 0;
        break;
      }
    }
  }
  if (stack.length > 0) hints.push(`"${stack.at(-1)}" is not closed.`);
  // operators
  if (/(^|[^=!<>])=($|[^=])/.test(stripped)) hints.push("Use == to compare (a single = is not an operator).");
  if (/(^|[^&])&($|[^&])/.test(stripped) || /(^|[^|])\|($|[^|])/.test(stripped)) hints.push("Use && and || to combine conditions.");
  if (/\b(and|or|not)\b/.test(stripped)) hints.push("CEL uses &&, || and ! instead of and / or / not.");
  // names
  const bound = new Set<string>();
  for (const m of stripped.matchAll(/\.(?:exists_one|exists|all|map|filter)\(\s*([A-Za-z_]\w*)\s*,/g)) bound.add(m[1]!);
  for (const m of stripped.matchAll(/(^|[^\w.])([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)(\s*\()?/g)) {
    const name = m[2]!;
    const call = !!m[3];
    const [root, field, ...rest] = name.split(".");
    if (!root) continue;
    if (root in CEL_FIELDS) {
      if (!field) hints.push(`${root} alone is an object: read a field, e.g. ${root}.${Object.keys(CEL_FIELDS[root]!)[0]}.`);
      else if (!(field in CEL_FIELDS[root]!)) hints.push(`Unknown field ${root}.${field}. Known: ${Object.keys(CEL_FIELDS[root]!).map((f) => `${root}.${f}`).join(", ")}.`);
      else if (rest.length > 0 && !MACROS.includes(rest[0]!) && !FUNCTIONS.has(rest[0]!)) hints.push(`${root}.${field} has no field ${rest[0]}.`);
      continue;
    }
    if (KEYWORDS.has(root) || bound.has(root) || (call && FUNCTIONS.has(root))) continue;
    if (call) hints.push(`Unknown function ${root}().`);
    else hints.push(`Unknown name ${root}: conditions read cs., case. or initiator.`);
  }
  for (const [path, values] of Object.entries(ENUM_VALUES)) {
    const re = new RegExp(`${path.replace(".", "\\.")}\\s*[!=]=\\s*"([^"]*)"`, "g");
    for (const m of text.matchAll(re)) {
      if (!values.includes(m[1]!)) hints.push(`${path} is one of ${values.map((v) => `"${v}"`).join(", ")} (not "${m[1]}").`);
    }
  }
  return [...new Set(hints)];
}

// ───────────────────────────── test bench ─────────────────────────────

export const CONTEXT_PRESETS: { id: string; label: string; context: PolicyContext }[] = [
  {
    id: "small-transfer",
    label: "Small transfer by a planner",
    context: {
      cs: { lines: 4, units: 320, value_cents: 0, ops: ["transfer_order.create"], connectors: ["merxa"], incremental_cost_cents: 120000, lost_units_after: 0, levers: ["reallocate_inventory"] },
      case: { mode: "actual", origin: "chat", tainted: false, unconfirmed: 0 },
      initiator: { roles: ["planner"] },
    },
  },
  {
    id: "new-po",
    label: "Plan that creates a purchase order",
    context: {
      cs: { lines: 6, units: 2400, value_cents: 3120000, ops: ["transfer_order.create", "po.create"], connectors: ["merxa"], incremental_cost_cents: 420000, lost_units_after: 300, levers: ["expedite_po"] },
      case: { mode: "actual", origin: "chat", tainted: false, unconfirmed: 0 },
      initiator: { roles: ["planner"] },
    },
  },
  {
    id: "webhook-replan",
    label: "Re-plan from a webhook",
    context: {
      cs: { lines: 3, units: 900, value_cents: 0, ops: ["transfer_order.create", "capacity_booking.cancel"], connectors: ["merxa"], incremental_cost_cents: -330000, lost_units_after: 0, levers: ["reallocate_inventory"] },
      case: { mode: "actual", origin: "webhook", tainted: false, unconfirmed: 0 },
      initiator: { roles: ["planner"] },
    },
  },
  {
    id: "what-if",
    label: "What-if case",
    context: {
      cs: { lines: 2, units: 100, value_cents: 0, ops: ["transfer_order.create"], connectors: ["merxa"], incremental_cost_cents: 0, lost_units_after: 0, levers: [] },
      case: { mode: "what_if", origin: "chat", tainted: false, unconfirmed: 0 },
      initiator: { roles: ["planner"] },
    },
  },
];

/** Validate a test-bench context before sending it (contracts `PolicyContext`, strict). */
export function checkContext(ctx: unknown): { ok: true; context: PolicyContext } | { ok: false; message: string } {
  const parsed = policyContextSchema.safeParse(ctx);
  if (parsed.success) return { ok: true, context: parsed.data };
  const first = parsed.error.issues[0];
  return { ok: false, message: first ? `${first.path.join(".") || "context"}: ${first.message}` : "The context is not valid." };
}

/** A comma / space separated list ("po.create, transfer_order.create") → unique non-empty items. */
export function parseList(text: string): string[] {
  return [...new Set(text.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean))];
}
