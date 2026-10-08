"use client";

import type { PolicyContext } from "@keelson/contracts";
import { Badge, Button, Checkbox, Field, IconButton, Select, TextInput, Textarea, toast, type BadgeTone } from "@keelson/ui";
import { ArrowDown, ArrowUp, FlaskConical, Pencil, Plus, Trash2, UsersRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useId, useMemo, useState, type ReactNode } from "react";
import { isApiError } from "@/lib/api/client";
import { adminError } from "@/lib/admin/format";
import {
  CEL_FIELDS,
  CONTEXT_PRESETS,
  MAX_WHEN,
  celHints,
  checkContext,
  guardedChanges,
  ordered,
  outcomeOf,
  parseList,
  requirementText,
  toPolicySet,
  validateRules,
  type GuardedChange,
  type Outcome,
  type PolicyRuleView,
  type PolicyTestResult,
  type Requirement,
} from "@/lib/admin/policies";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { hasAnyRole } from "@/lib/roles";
import { AdminPageHeader, AdminSection, InlineError, ResourceView } from "./common";
import { ScoringSection } from "./ScoringSection";
import { SecondAdminBanner, announceChangeRequest } from "./SecondAdmin";

const OUTCOME_META: Record<Outcome, { label: string; tone: BadgeTone }> = {
  refuse: { label: "Refuse", tone: "danger" },
  auto: { label: "Automatic", tone: "warning" },
  human: { label: "Approval", tone: "accent" },
};

const KNOWN_ROLES = ["approver", "compliance", "planner", "tenant-admin"] as const;
const ROLE_LABELS: Record<string, string> = { approver: "Approver", compliance: "Compliance", planner: "Planner", "tenant-admin": "Tenant admin" };

const DEFAULT_REQUIREMENT: Requirement = { roles: ["approver"], count: 1, exclude_initiator: false };

function nextRuleId(rules: readonly PolicyRuleView[]): string {
  let n = rules.length + 1;
  while (rules.some((r) => r.id === `rule-${n}`)) n += 1;
  return `rule-${n}`;
}

/** CEL condition editor: monospace text, character count, live syntax hints (non-blocking). */
function CelEditor({ value, onChange, error }: { value: string; onChange: (v: string) => void; error?: string | null | undefined }) {
  const hints = useMemo(() => celHints(value), [value]);
  const hintsId = useId();
  return (
    <Field
      label="Condition (CEL)"
      description="Read cs., case. and initiator. fields; combine with &&, || and !. The first rule whose condition is true decides."
      error={error ?? null}
    >
      <Textarea value={value} onChange={onChange} rows={3} maxLength={MAX_WHEN} className="font-mono" spellCheck={false} aria-describedby={hintsId} />
      <div id={hintsId} aria-live="polite" className="text-caption">
        {hints.length > 0 ? (
          <ul className="flex flex-col gap-0.5 text-warning" data-cel-hints="">
            {hints.map((h) => (
              <li key={h}>Hint: {h}</li>
            ))}
          </ul>
        ) : (
          <span className="text-fg-subtle">No syntax problems spotted. The server checks it again when you save.</span>
        )}
      </div>
    </Field>
  );
}

function RuleEditor({
  rule,
  problems,
  onChange,
  onDone,
}: {
  rule: PolicyRuleView;
  problems: Record<string, string>;
  onChange: (next: PolicyRuleView) => void;
  onDone: () => void;
}) {
  const outcome = outcomeOf(rule);
  const req = typeof rule.require === "string" ? null : rule.require;
  const roles = [...new Set([...KNOWN_ROLES, ...(req?.roles ?? [])])];
  const setReq = (patch: Partial<Requirement>) => onChange({ ...rule, require: { ...(req ?? DEFAULT_REQUIREMENT), ...patch } });
  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-surface-2 p-3" data-rule-editor={rule.id}>
      <div className="grid gap-3 sm:grid-cols-[1fr_8rem]">
        <Field label="Rule id" error={problems["id"] ?? null} description="Receipts name it: policy:<id>@<version>.">
          <TextInput value={rule.id} mono onChange={(e) => onChange({ ...rule, id: e.target.value })} />
        </Field>
        <Field label="Priority" error={problems["priority"] ?? null} description="Lower runs first.">
          <TextInput
            type="number"
            min={0}
            step={1}
            value={String(rule.priority)}
            onChange={(e) => onChange({ ...rule, priority: e.target.value === "" ? -1 : Number(e.target.value) })}
          />
        </Field>
      </div>
      <Field label="Description" error={problems["description"] ?? null}>
        <TextInput value={rule.description} onChange={(e) => onChange({ ...rule, description: e.target.value })} />
      </Field>
      <CelEditor value={rule.when} onChange={(when) => onChange({ ...rule, when })} error={problems["when"]} />
      <Field label="When it matches" error={problems["require"] ?? null}>
        <Select
          value={outcome}
          onChange={(v) => onChange({ ...rule, require: v === "human" ? (req ?? DEFAULT_REQUIREMENT) : (v as "auto" | "refuse") })}
          options={[
            { value: "human", label: "Needs approval by people" },
            { value: "auto", label: "Apply automatically" },
            { value: "refuse", label: "Refuse (never commit)" },
          ]}
        />
      </Field>
      {req ? (
        <fieldset className="flex flex-col gap-2">
          <legend className="text-ui font-medium text-fg">Who approves</legend>
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {roles.map((role) => (
              <Checkbox
                key={role}
                checked={req.roles.includes(role)}
                onCheckedChange={(on) => setReq({ roles: on ? [...req.roles, role] : req.roles.filter((r) => r !== role) })}
                label={ROLE_LABELS[role] ?? role}
              />
            ))}
          </div>
          <div className="grid gap-3 sm:grid-cols-[10rem_1fr] sm:items-end">
            <Field label="Different people">
              <TextInput type="number" min={1} max={10} step={1} value={String(req.count)} onChange={(e) => setReq({ count: Number(e.target.value) })} />
            </Field>
            <Checkbox
              checked={req.exclude_initiator}
              onCheckedChange={(on) => setReq({ exclude_initiator: on })}
              label="The person who started the change can't approve it"
            />
          </div>
        </fieldset>
      ) : null}
      <div>
        <Button size="sm" variant="secondary" onClick={onDone}>
          Done
        </Button>
      </div>
    </div>
  );
}

function RuleItem({
  rule,
  position,
  count,
  editable,
  editing,
  matched,
  guarded,
  problems,
  onEdit,
  onMove,
  onDelete,
  editor,
}: {
  rule: PolicyRuleView;
  position: number;
  count: number;
  editable: boolean;
  editing: boolean;
  matched: boolean;
  guarded: GuardedChange[];
  problems: string[];
  onEdit: () => void;
  onMove: (delta: -1 | 1) => void;
  onDelete: () => void;
  editor: ReactNode;
}) {
  const meta = OUTCOME_META[outcomeOf(rule)];
  return (
    <li
      data-rule-id={rule.id}
      data-matched={matched ? "" : undefined}
      className={matched ? "rounded-md border-2 border-accent p-3" : "rounded-md border border-border p-3"}
    >
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1 basis-72">
          <p className="flex flex-wrap items-center gap-2 text-ui">
            <span className="font-semibold text-fg tabular-nums">{position}.</span>
            <span className="font-mono text-fg">{rule.id}</span>
            <Badge size="sm" tone={meta.tone}>
              {meta.label}
            </Badge>
            <span className="text-caption text-fg-muted">priority {rule.priority}</span>
            {matched ? (
              <Badge size="sm" tone="accent">
                Matched in the test
              </Badge>
            ) : null}
          </p>
          <p className="mt-1 text-ui text-fg">{rule.description}</p>
          <pre className="mt-1.5 overflow-x-auto rounded-sm bg-surface-2 px-2 py-1.5 font-mono text-caption whitespace-pre-wrap text-fg">{rule.when}</pre>
          <p className="mt-1 text-caption text-fg-muted">Then: {requirementText(rule.require)}</p>
          {guarded.map((g) => (
            <p key={g.reason} className="mt-1 flex items-center gap-1 text-caption text-fg-muted" data-guarded="">
              <UsersRound aria-hidden="true" className="size-3.5" />
              Needs a second admin: {g.reason}
            </p>
          ))}
          {problems.length > 0 && !editing ? (
            <ul className="mt-1 text-caption text-danger">
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          ) : null}
        </div>
        {editable ? (
          <div className="flex items-center gap-1">
            <IconButton size="sm" label={`Move ${rule.id} up`} icon={<ArrowUp className="size-4" />} disabled={position === 1} onClick={() => onMove(-1)} />
            <IconButton size="sm" label={`Move ${rule.id} down`} icon={<ArrowDown className="size-4" />} disabled={position === count} onClick={() => onMove(1)} />
            <IconButton size="sm" label={`Edit ${rule.id}`} icon={<Pencil className="size-4" />} aria-expanded={editing} onClick={onEdit} />
            <IconButton size="sm" label={`Delete ${rule.id}`} icon={<Trash2 className="size-4" />} disabled={count <= 1} onClick={onDelete} />
          </div>
        ) : null}
      </div>
      {editing ? <div className="mt-3">{editor}</div> : null}
    </li>
  );
}

type ContextDraft = {
  lines: string;
  units: string;
  value_cents: string;
  incremental_cost_cents: string;
  lost_units_after: string;
  ops: string;
  connectors: string;
  levers: string;
  mode: string;
  origin: string;
  tainted: boolean;
  unconfirmed: string;
  roles: string;
};

function toDraft(ctx: PolicyContext): ContextDraft {
  return {
    lines: String(ctx.cs.lines),
    units: String(ctx.cs.units),
    value_cents: String(ctx.cs.value_cents),
    incremental_cost_cents: String(ctx.cs.incremental_cost_cents),
    lost_units_after: String(ctx.cs.lost_units_after),
    ops: ctx.cs.ops.join(", "),
    connectors: ctx.cs.connectors.join(", "),
    levers: ctx.cs.levers.join(", "),
    mode: ctx.case.mode,
    origin: ctx.case.origin,
    tainted: ctx.case.tainted,
    unconfirmed: String(ctx.case.unconfirmed),
    roles: ctx.initiator.roles.join(", "),
  };
}

function fromDraft(d: ContextDraft): unknown {
  const num = (s: string) => (s.trim() === "" ? Number.NaN : Number(s));
  return {
    cs: {
      lines: num(d.lines),
      units: num(d.units),
      value_cents: num(d.value_cents),
      incremental_cost_cents: num(d.incremental_cost_cents),
      lost_units_after: num(d.lost_units_after),
      ops: parseList(d.ops),
      connectors: parseList(d.connectors),
      levers: parseList(d.levers),
    },
    case: { mode: d.mode, origin: d.origin, tainted: d.tainted, unconfirmed: num(d.unconfirmed) },
    initiator: { roles: parseList(d.roles) },
  };
}

const OUTCOME_TEXT: Record<Outcome, string> = { auto: "Applied automatically", human: "Needs approval", refuse: "Refused" };

function TestResult({ result }: { result: PolicyTestResult }) {
  const d = result.decision;
  return (
    <div role="status" className="flex flex-col gap-1.5 rounded-md border border-border bg-surface-2 px-3 py-2.5 text-ui" data-test-result={d.outcome}>
      <p className="flex flex-wrap items-center gap-2 font-medium text-fg">
        <Badge tone={OUTCOME_META[d.outcome].tone}>{OUTCOME_TEXT[d.outcome]}</Badge>
        {d.rule_id ? (
          <span>
            by rule <span className="font-mono">{d.rule_id}</span>
          </span>
        ) : (
          <span>by a safety limit or the fallback (no rule matched)</span>
        )}
      </p>
      {d.requirement ? <p className="text-fg-muted">Who: {requirementText(d.requirement as Requirement)}</p> : null}
      {d.reason ? <p className="text-fg-muted">Why: {d.reason}</p> : null}
      {result.approved_by ? (
        <p className="text-fg-muted">
          Receipt: <span className="font-mono">{result.approved_by}</span>
        </p>
      ) : null}
      {result.floors?.must_refuse ? <p className="text-fg-muted">Safety limit: what-if cases and unconfirmed values are always refused.</p> : null}
      {result.floors && result.floors.auto_allowed === false && !result.floors.must_refuse ? (
        <p className="text-fg-muted">Safety limit: only a person&apos;s chat on trusted input can be applied automatically, so automatic rules were skipped.</p>
      ) : null}
    </div>
  );
}

function TestBench({
  draft,
  dirty,
  available,
  onMatched,
}: {
  draft: readonly PolicyRuleView[];
  dirty: boolean;
  available: boolean;
  onMatched: (ruleId: string | null) => void;
}) {
  const admin = useAdminApi();
  const [preset, setPreset] = useState(CONTEXT_PRESETS[0]!.id);
  const [ctx, setCtx] = useState<ContextDraft>(() => toDraft(CONTEXT_PRESETS[0]!.context));
  const [result, setResult] = useState<PolicyTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const set = (patch: Partial<ContextDraft>) => setCtx((prev) => ({ ...prev, ...patch }));

  const run = async () => {
    const checked = checkContext(fromDraft(ctx));
    if (!checked.ok) {
      setError(`The sample is not valid: ${checked.message}`);
      return;
    }
    setTesting(true);
    setError(null);
    try {
      const out = await admin.testPolicy(checked.context, dirty ? ordered(draft) : undefined);
      setResult(out);
      onMatched(out.decision.rule_id);
    } catch (err) {
      setResult(null);
      onMatched(null);
      setError(adminError(err));
    } finally {
      setTesting(false);
    }
  };

  const num = (label: string, key: keyof ContextDraft, description?: string) => (
    <Field label={label} description={description}>
      <TextInput type="number" step={1} value={String(ctx[key])} onChange={(e) => set({ [key]: e.target.value } as Partial<ContextDraft>)} />
    </Field>
  );

  return (
    <AdminSection
      title="Test bench"
      description={dirty ? "Evaluates your unsaved rules against a sample change set." : "Evaluates the saved rules against a sample change set."}
      actions={
        <Button size="sm" variant="primary" icon={<FlaskConical className="size-4" />} loading={testing} disabled={!available} onClick={() => void run()}>
          {dirty ? "Test draft rules" : "Test rules"}
        </Button>
      }
    >
      {!available ? <p className="text-ui text-fg-muted">The policy evaluator is not available on the server, so rules can&apos;t be tested right now.</p> : null}
      <Field label="Start from">
        <Select
          value={preset}
          onChange={(id) => {
            setPreset(id);
            const p = CONTEXT_PRESETS.find((x) => x.id === id);
            if (p) setCtx(toDraft(p.context));
          }}
          options={CONTEXT_PRESETS.map((p) => ({ value: p.id, label: p.label }))}
        />
      </Field>
      <fieldset className="grid gap-3 sm:grid-cols-3">
        <legend className="mb-1 text-ui font-medium text-fg">Change set (cs)</legend>
        {num("Lines", "lines")}
        {num("Units", "units")}
        {num("Value (cents)", "value_cents")}
        {num("Extra cost (cents)", "incremental_cost_cents")}
        {num("Units lost after", "lost_units_after")}
        <Field label="Operations" description="Comma-separated">
          <TextInput mono value={ctx.ops} onChange={(e) => set({ ops: e.target.value })} />
        </Field>
        <Field label="Systems" description="Comma-separated">
          <TextInput mono value={ctx.connectors} onChange={(e) => set({ connectors: e.target.value })} />
        </Field>
        <Field label="Levers" description="Comma-separated">
          <TextInput mono value={ctx.levers} onChange={(e) => set({ levers: e.target.value })} />
        </Field>
      </fieldset>
      <fieldset className="grid gap-3 sm:grid-cols-3">
        <legend className="mb-1 text-ui font-medium text-fg">Case and initiator</legend>
        <Field label="Mode">
          <Select value={ctx.mode} onChange={(mode) => set({ mode })} options={[{ value: "actual", label: "Actual" }, { value: "what_if", label: "What-if" }]} />
        </Field>
        <Field label="Origin">
          <Select
            value={ctx.origin}
            onChange={(origin) => set({ origin })}
            options={[
              { value: "chat", label: "Chat" },
              { value: "signal", label: "Signal" },
              { value: "webhook", label: "Webhook" },
            ]}
          />
        </Field>
        {num("Unconfirmed values", "unconfirmed")}
        <Field label="Initiator roles" description="Comma-separated">
          <TextInput mono value={ctx.roles} onChange={(e) => set({ roles: e.target.value })} />
        </Field>
        <div className="sm:col-span-2 sm:self-end">
          <Checkbox checked={ctx.tainted} onCheckedChange={(tainted) => set({ tainted })} label="Untrusted input influenced the plan (tainted)" />
        </div>
      </fieldset>
      {error ? <InlineError message={error} /> : null}
      {result ? <TestResult result={result} /> : null}
    </AdminSection>
  );
}

function CelReference() {
  return (
    <AdminSection title="CEL syntax" description="What a condition can read, with examples.">
      <div className="grid gap-3 md:grid-cols-3">
        {Object.entries(CEL_FIELDS).map(([root, fields]) => (
          <div key={root}>
            <h3 className="text-ui font-medium text-fg">{root}</h3>
            <dl className="mt-1 flex flex-col gap-1 text-caption">
              {Object.entries(fields).map(([name, f]) => (
                <div key={name}>
                  <dt className="font-mono text-fg">
                    {root}.{name} <span className="text-fg-subtle">: {f.type}</span>
                  </dt>
                  <dd className="text-fg-muted">{f.hint}</dd>
                </div>
              ))}
            </dl>
          </div>
        ))}
      </div>
      <ul className="flex flex-col gap-1 text-caption text-fg-muted">
        <li>
          <span className="font-mono text-fg">cs.lines &lt;= 10 &amp;&amp; cs.units &lt;= 500</span> — numbers and comparisons
        </li>
        <li>
          <span className="font-mono text-fg">cs.ops.exists(o, o in [&quot;po.create&quot;, &quot;po_line.cancel&quot;])</span> — any element matches
        </li>
        <li>
          <span className="font-mono text-fg">cs.ops.all(o, o == &quot;transfer_order.create&quot;)</span> — every element matches
        </li>
        <li>
          <span className="font-mono text-fg">case.origin != &quot;chat&quot; || case.tainted</span> — strings in double quotes; booleans as they are
        </li>
        <li>
          <span className="font-mono text-fg">true</span> — always matches (a catch-all last rule)
        </li>
      </ul>
    </AdminSection>
  );
}

/**
 * Admin **Policies** (M5, `docs/contracts/95` §1): the approval rules in evaluation order (first
 * match wins) with priorities, an editor per rule (CEL condition with syntax hints, outcome,
 * quorum), guarded changes for tenant admins (the engine's own guard rules: e.g. adding an
 * automatic rule, raising its caps, removing or weakening a refuse / approval rule — M7: saved as a
 * change request a second admin approves), and a test bench (`POST /api/admin/policies/test`) for
 * the saved or the draft rules. M8 (`126` §6): the **Option scoring** section (weight profiles, the
 * default profile, sampled futures; {@link ScoringSection}).
 */
export function PoliciesPage() {
  const admin = useAdminApi();
  const router = useRouter();
  const { me } = useSession();
  const platform = hasAnyRole(me.user, ["platform-admin"]);
  const res = useResource(() => admin.policies(), "policies");
  const [draft, setDraft] = useState<PolicyRuleView[] | null>(null);
  const [editing, setEditing] = useState<number | null>(null);
  const [matched, setMatched] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<{ message: string; details: string[]; conflict: boolean } | null>(null);

  const data = res.data;
  const base = useMemo(() => (data ? toPolicySet(data.policy) : null), [data]);
  const rules = useMemo(() => draft ?? base?.rules ?? [], [draft, base]);
  const dirty = draft !== null && JSON.stringify(ordered(draft)) !== JSON.stringify(ordered(base?.rules ?? []));
  const problems = useMemo(() => validateRules(rules), [rules]);
  const guarded = useMemo(() => (base && draft ? guardedChanges(base.rules, draft) : []), [base, draft]);
  /** M7: a tenant admin's guarded save becomes a change request (it no longer blocks Save). */
  const needsSecond = guarded.length > 0 && !platform;
  const editable = data?.editable !== false;

  const update = (index: number, next: PolicyRuleView) =>
    setDraft((prev) => {
      const list = [...(prev ?? base?.rules ?? [])];
      list[index] = next;
      return list;
    });

  const move = (index: number, delta: -1 | 1) => {
    const list = [...rules];
    const order = ordered(list.map((r, i) => ({ ...r, i }))).map((r) => r.i);
    const at = order.indexOf(index);
    const other = order[at + delta];
    if (other === undefined) return;
    const a = list[index]!;
    const b = list[other]!;
    list[index] = { ...a, priority: b.priority };
    list[other] = { ...b, priority: a.priority };
    setDraft(list);
  };

  const remove = (index: number) => {
    setDraft(rules.filter((_, i) => i !== index));
    setEditing(null);
  };

  const addRule = () => {
    const priority = Math.max(0, ...rules.map((r) => r.priority)) + 10;
    const list = [...rules, { id: nextRuleId(rules), description: "", when: "false", require: { ...DEFAULT_REQUIREMENT }, priority }];
    setDraft(list);
    setEditing(list.length - 1);
  };

  const discard = () => {
    setDraft(null);
    setEditing(null);
    setSaveError(null);
  };

  const save = async () => {
    if (!base || !data || !draft || problems.length > 0) return;
    setSaving(true);
    setSaveError(null);
    try {
      const res2 = await admin.savePolicies(data.spec_version, ordered(draft));
      if (res2.kind === "change_request") {
        announceChangeRequest(res2.request, router.push);
        setDraft(null);
        setEditing(null);
        return;
      }
      const out = res2.value;
      if (out.unchanged) toast("Nothing to save", { description: "The rules are the same as the saved ones." });
      else toast.success(`Policies saved (${out.policy.version})`, { description: `Agent settings ${out.spec_version}.` });
      setDraft(null);
      setEditing(null);
      res.reload();
    } catch (err) {
      const conflict = isApiError(err, "version_conflict");
      const details =
        isApiError(err) && Array.isArray(err.body["errors"])
          ? (err.body["errors"] as { loc?: unknown; msg?: unknown }[]).map((e) => `${String(e.loc ?? "")}: ${String(e.msg ?? "")}`)
          : isApiError(err, "guarded_change") && Array.isArray(err.body["reasons"])
            ? (err.body["reasons"] as { field?: unknown; reason?: unknown }[]).map((r) => `${String(r.field ?? "")}: ${String(r.reason ?? "")}`)
            : [];
      setSaveError({ message: adminError(err), details, conflict });
    } finally {
      setSaving(false);
    }
  };

  const sorted = ordered(rules.map((r, index) => ({ ...r, index })));

  return (
    <>
      <AdminPageHeader
        title="Policies"
        description="Who must approve a change before it is applied. Rules run from the top; the first one that matches decides."
        actions={
          <>
            {base ? <Badge tone="neutral">Policy {base.version}</Badge> : null}
            {dirty ? (
              <>
                <Button size="sm" variant="ghost" onClick={discard} disabled={saving}>
                  Discard
                </Button>
                <Button size="sm" variant="primary" loading={saving} disabled={problems.length > 0} onClick={() => void save()}>
                  {needsSecond ? "Request approval" : "Save rules"}
                </Button>
              </>
            ) : null}
          </>
        }
      />
      {data && data.source === "default" ? (
        <p className="text-ui text-fg-muted">This workspace uses the default rules. Saving a change stores them in its agent settings.</p>
      ) : null}
      {!editable ? <p className="text-ui text-fg-muted">This server can&apos;t store policy rules yet, so they are read-only.</p> : null}
      {needsSecond ? (
        <div data-guarded-summary="">
          <SecondAdminBanner
            reasons={guarded.map((g) => (
              <>
                <span className="font-mono">{g.ruleId}</span>: {g.reason}
              </>
            ))}
          />
        </div>
      ) : null}
      {saveError ? (
        <div className="flex flex-col gap-1">
          <InlineError message={saveError.message} />
          {saveError.details.length > 0 ? (
            <ul className="text-caption text-danger">
              {saveError.details.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          ) : null}
          {saveError.conflict ? (
            <div>
              <Button size="sm" onClick={() => {
                  discard();
                  res.reload();
                }}>
                Load the latest version
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}

      <AdminSection
        title="Rules"
        description="Safety limits apply whatever the rules say: what-if cases and unconfirmed values are never committed, and only a person's chat on trusted input can be applied automatically."
        actions={
          editable && base ? (
            <Button size="sm" icon={<Plus className="size-4" />} onClick={addRule} disabled={rules.length >= 50}>
              Add rule
            </Button>
          ) : null
        }
      >
        <ResourceView resource={res} label="policies">
          {() => (
            <ol aria-label="Rules in evaluation order" className="flex flex-col gap-2">
              {sorted.map((r, pos) => {
                const ruleProblems = problems.filter((p) => p.ruleIndex === r.index);
                const byField = Object.fromEntries(ruleProblems.map((p) => [p.field, p.message]));
                const { index, ...rule } = r;
                return (
                  <RuleItem
                    key={index}
                    rule={rule}
                    position={pos + 1}
                    count={sorted.length}
                    editable={editable}
                    editing={editing === index}
                    matched={matched === rule.id}
                    guarded={platform ? [] : guarded.filter((g) => g.ruleId === rule.id)}
                    problems={ruleProblems.map((p) => p.message)}
                    onEdit={() => setEditing(editing === index ? null : index)}
                    onMove={(delta) => move(index, delta)}
                    onDelete={() => remove(index)}
                    editor={<RuleEditor rule={rule} problems={byField} onChange={(next) => update(index, next)} onDone={() => setEditing(null)} />}
                  />
                );
              })}
            </ol>
          )}
        </ResourceView>
        {problems.some((p) => p.ruleIndex === null) ? (
          <InlineError message={problems.filter((p) => p.ruleIndex === null).map((p) => p.message).join(" ")} />
        ) : null}
        {guarded.length > 0 && !platform
          ? guarded
              .filter((g) => !rules.some((r) => r.id === g.ruleId))
              .map((g) => (
                <p key={`${g.ruleId}:${g.reason}`} className="flex items-center gap-1 text-caption text-fg-muted">
                  <UsersRound aria-hidden="true" className="size-3.5" />
                  Needs a second admin: {g.reason} ({g.ruleId})
                </p>
              ))
          : null}
      </AdminSection>

      {data ? <TestBench draft={rules} dirty={dirty} available={data.evaluator_available !== false} onMatched={setMatched} /> : null}
      <CelReference />
      <ScoringSection />
    </>
  );
}
