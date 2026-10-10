"use client";

import {
  Badge,
  Button,
  Card,
  Checkbox,
  ConfirmDialog,
  Dialog,
  Drawer,
  Field,
  JsonDiff,
  Select,
  Spinner,
  TextInput,
  Textarea,
  jsonEqual,
  toast,
} from "@keelson/ui";
import { History, RotateCcw, Save } from "lucide-react";
import { useRouter } from "next/navigation";
import { Fragment, useState } from "react";
import { isApiError } from "@/lib/api/client";
import { adminError, when } from "@/lib/admin/format";
import {
  DISPLAY_NAME_MAX,
  GUIDANCE_HEADING,
  MAX_TOOL_CALLS,
  MODEL_ROLE_INFO,
  aliasProblem,
  canRaiseGuarded,
  connectorTools,
  guardedChanges,
  guardedMessage,
  isPromptRole,
  mergePatch,
  normalizeSpec,
  patchSummary,
  promptFraming,
  toolEligibility,
  validateDraft,
  type SpecIssues,
} from "@/lib/admin/spec";
import { ACCESS_LABELS, TRUST_LABELS, label as fmtLabel } from "@/lib/admin/tools";
import { MODEL_ROLES, PROMPT_MAX_CHARS, type AgentCatalog, type AgentSpec, type ModelRole, type SpecEnvelope } from "@/lib/admin/types";
import { blockedFactoriesMessage, blockedFactoriesRemoved } from "@/lib/admin/planning";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { AgentsSubnav } from "./AgentsSubnav";
import { AdminPageHeader, AdminSection, InlineError, ResourceView } from "./common";
import { PlanningSolverSection } from "./PlanningSolverSection";
import { SecondAdminBanner, announceChangeRequest } from "./SecondAdmin";

const DIFF_IGNORE = ["version", "tenant"] as const;

export function personName(x: unknown): string {
  if (typeof x === "string") return x;
  if (x && typeof x === "object") {
    const o = x as Record<string, unknown>;
    for (const k of ["name", "username", "sub"]) if (typeof o[k] === "string" && o[k]) return o[k] as string;
  }
  return "—";
}

function clone<T>(v: T): T {
  return structuredClone(v);
}

// ───────────────────────────── small fields ─────────────────────────────

/** Integer input that keeps what is typed; reports `NaN` while it is not a number. */
function NumberField({
  label,
  value,
  onChange,
  error,
  description,
  lockedReason,
  min,
  max,
}: {
  label: string;
  value: number | undefined;
  onChange: (n: number) => void;
  error?: string | undefined;
  description?: string | undefined;
  lockedReason?: string | undefined;
  min?: number | undefined;
  max?: number | undefined;
}) {
  const [text, setText] = useState(value === undefined || Number.isNaN(value) ? "" : String(value));
  return (
    <Field label={label} error={error} description={description} lockedReason={lockedReason}>
      <TextInput
        type="number"
        inputMode="numeric"
        step={1}
        min={min}
        max={max}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          onChange(e.target.value.trim() === "" ? Number.NaN : Number(e.target.value));
        }}
      />
    </Field>
  );
}

function ToolAllowlist({
  legend,
  connector,
  selected,
  catalog,
  onChange,
  error,
}: {
  legend: string;
  connector: string;
  selected: readonly string[];
  catalog: AgentCatalog;
  onChange: (tools: string[]) => void;
  error?: string | undefined;
}) {
  const tools = connectorTools(catalog, connector);
  const known = new Set(tools.map((t) => t.name));
  const orphans = selected.filter((t) => !known.has(t) && t !== "calc");
  const toggle = (name: string, on: boolean) => onChange(on ? [...selected, name] : selected.filter((t) => t !== name));
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-ui font-medium text-fg">
        {legend} <span className="font-normal text-fg-muted">· {connector}</span>
      </legend>
      {tools.length === 0 && orphans.length === 0 ? <p className="text-caption text-fg-muted">This connector has no tools in the catalog.</p> : null}
      <div className="grid gap-x-4 gap-y-2 sm:grid-cols-2">
        {tools.map((t) => {
          const el = toolEligibility(t);
          const checked = selected.includes(t.name);
          return (
            <Checkbox
              key={t.name}
              checked={checked}
              // An ineligible tool that is still selected can be removed, never added.
              disabled={!el.eligible && !checked}
              onCheckedChange={(on) => toggle(t.name, on)}
              label={<span className="font-mono text-caption">{t.name}</span>}
              description={t.title ?? t.description ?? undefined}
              disabledReason={
                el.eligible ? undefined : `${el.reason} (${fmtLabel(ACCESS_LABELS, t.access)}, ${fmtLabel(TRUST_LABELS, t.content_trust)})`
              }
            />
          );
        })}
        {orphans.map((name) => (
          <Checkbox
            key={name}
            checked
            onCheckedChange={(on) => toggle(name, on)}
            label={<span className="font-mono text-caption">{name}</span>}
            disabledReason="Not in the connector catalog any more: remove it."
          />
        ))}
      </div>
      {error ? (
        <p className="text-caption font-medium text-danger" role="alert">
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}

// ───────────────────────────── role card ─────────────────────────────

const DEFAULT_PROMPT_PLACEHOLDER = "e.g. Our peak season is W45–W52; prefer moving stock between EU DCs before re-ordering.";
const PROMPT_PLACEHOLDERS: Partial<Record<ModelRole, string>> = {
  planner: "e.g. Always ask the playbook analyst when expediting or another supplier may be recommended.",
  critic: "e.g. Treat stock figures older than two days as stale and say so.",
};

function RoleCard({
  role,
  draft,
  baseAlias,
  catalog,
  issues,
  setDraft,
}: {
  role: ModelRole;
  draft: AgentSpec;
  /** The alias saved in the version being edited (always selectable again). */
  baseAlias: string | undefined;
  catalog: AgentCatalog;
  issues: SpecIssues;
  setDraft: (fn: (d: AgentSpec) => void) => void;
}) {
  const info = MODEL_ROLE_INFO[role];
  const fromCatalog = catalog.roles.find((r) => r.role === role);
  const title = fromCatalog?.display_name || info.label;
  const alias = draft.models[role] ?? fromCatalog?.alias ?? "";
  // Aliases this role can't use (not a chat model, no tool-loop support) are listed but disabled.
  const options = catalog.aliases.map((a) => {
    const problem = a.alias === baseAlias ? null : aliasProblem(role, a.alias, catalog);
    const base = a.mode ? `${a.alias} (${a.mode})` : a.alias;
    return { value: a.alias, label: problem ? `${base}: not usable here` : base, disabled: problem !== null };
  });
  // Gateway unreachable: the catalog has no aliases, so nothing can be checked or offered. Keep
  // the saved alias (not "not available") and lock the choice until the gateway answers again.
  const gatewayDown = catalog.gateway_available === false;
  if (alias && !options.some((o) => o.value === alias)) {
    options.unshift({ value: alias, label: gatewayDown ? alias : `${alias} (not available)`, disabled: false });
  }
  const headingId = `role-${role}`;
  const prompt = isPromptRole(role) ? (draft.prompts?.[role] ?? "") : null;
  return (
    <Card padding="sm" labelledBy={headingId} className="flex flex-col gap-3">
      <div>
        <h3 id={headingId} className="text-ui font-semibold text-fg">
          {title}
        </h3>
        <p className="text-caption text-fg-muted">{fromCatalog?.description ?? info.description}</p>
      </div>
      <Field
        label="Model alias"
        error={issues[`models.${role}`]}
        lockedReason={gatewayDown ? "The AI Gateway can't be reached, so models can't be changed right now." : undefined}
      >
        <Select
          value={alias}
          placeholder="Choose a model…"
          disabled={gatewayDown}
          options={options}
          onChange={(v) =>
            setDraft((d) => {
              d.models = { ...d.models, [role]: v };
            })
          }
        />
      </Field>
      {prompt !== null ? (
        <>
          <Field
            label="Prompt addendum"
            withCounter
            error={issues[`prompts.${role}`]}
            description="Extra guidance for this agent. It is added as data and can't change the output format or the numbers rule."
          >
            <Textarea
              value={prompt}
              maxLength={PROMPT_MAX_CHARS}
              rows={3}
              placeholder={PROMPT_PLACEHOLDERS[role] ?? DEFAULT_PROMPT_PLACEHOLDER}
              onChange={(v) =>
                setDraft((d) => {
                  d.prompts = { ...(d.prompts ?? {}), [role]: v };
                })
              }
            />
          </Field>
          <details className="rounded-sm bg-surface-2 px-3 py-2">
            <summary className="cursor-pointer text-caption font-medium text-fg-muted">Preview how it is framed</summary>
            <pre className="mt-2 font-mono text-caption break-words whitespace-pre-wrap text-fg" aria-label={`Framing preview: ${GUIDANCE_HEADING}`}>
              {promptFraming(prompt)}
            </pre>
          </details>
        </>
      ) : null}
    </Card>
  );
}

// ───────────────────────────── versions drawer ─────────────────────────────

function VersionsDrawer({ open, onOpenChange, current, onRestored }: { open: boolean; onOpenChange: (o: boolean) => void; current: SpecEnvelope; onRestored: () => void }) {
  const admin = useAdminApi();
  const versions = useResource(() => (open ? admin.listSpecVersions() : Promise.resolve([])), `versions:${open}:${current.version}`);
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useResource(
    () => (selected ? admin.getSpecVersion(selected) : Promise.resolve(null)),
    `version:${selected ?? ""}`,
  );
  const [confirm, setConfirm] = useState(false);
  const router = useRouter();

  return (
    <Drawer open={open} onOpenChange={onOpenChange} size="xl" title="Versions" description={`Active: ${current.version}. Restoring copies an older version as a new one.`}>
      <div className="flex flex-col gap-4 lg:flex-row">
        <div className="lg:w-64 lg:shrink-0">
          <ResourceView resource={versions} label="versions">
            {(list) => (
              <ul className="flex flex-col gap-1" aria-label="Saved versions">
                {list.map((v) => {
                  const isCurrent = v.version === current.version;
                  return (
                    <li key={v.version}>
                      <button
                        type="button"
                        aria-pressed={selected === v.version}
                        onClick={() => setSelected(v.version)}
                        className="flex w-full flex-col items-start gap-0.5 rounded-sm border border-border px-2.5 py-1.5 text-left text-ui hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus aria-pressed:border-accent aria-pressed:bg-accent-soft"
                      >
                        <span className="flex items-center gap-2 font-medium text-fg">
                          {v.version}
                          {isCurrent ? (
                            <Badge size="sm" tone="success">
                              Active
                            </Badge>
                          ) : null}
                        </span>
                        <span className="text-caption text-fg-muted">
                          {personName(v.created_by)} · {when(v.created_at)}
                        </span>
                        {v.summary ? <span className="text-caption text-fg-subtle">{v.summary}</span> : null}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </ResourceView>
        </div>
        <div className="min-w-0 flex-1">
          {!selected ? (
            <p className="text-ui text-fg-muted">Choose a version to compare it with the active one.</p>
          ) : (
            <ResourceView resource={detail} label={`version ${selected}`}>
              {(d) =>
                d ? (
                  <div className="flex flex-col gap-3">
                    <JsonDiff
                      caption={`Changes from ${selected} to the active ${current.version}`}
                      before={d.spec}
                      after={current.spec}
                      beforeLabel={selected}
                      afterLabel={`${current.version} (active)`}
                      ignorePaths={DIFF_IGNORE}
                      empty="Same settings as the active version."
                    />
                    {selected !== current.version ? (
                      <div>
                        <Button size="sm" icon={<RotateCcw className="size-4" />} onClick={() => setConfirm(true)}>
                          Restore {selected}
                        </Button>
                      </div>
                    ) : null}
                  </div>
                ) : null
              }
            </ResourceView>
          )}
        </div>
      </div>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Restore ${selected ?? ""}?`}
        description="Its settings are saved as a new version and new runs use them. Runs in progress keep their version."
        confirmLabel="Restore"
        onConfirm={async () => {
          if (!selected) return;
          try {
            const out = await admin.restoreSpec(selected);
            if (out.kind === "change_request") {
              announceChangeRequest(out.request, router.push);
              setSelected(null);
              return;
            }
            toast.success(`Restored ${selected} as ${out.value.version}`);
            setSelected(null);
            onRestored();
          } catch (err) {
            toast.error("Couldn't restore", { description: adminError(err) });
            return false;
          }
        }}
      />
    </Drawer>
  );
}

// ───────────────────────────── editor ─────────────────────────────

function SpecEditor({ base, catalog, onSaved }: { base: SpecEnvelope; catalog: AgentCatalog; onSaved: () => void }) {
  const admin = useAdminApi();
  const router = useRouter();
  const { me } = useSession();
  const platform = canRaiseGuarded(me.user);
  const [draft, setDraftState] = useState<AgentSpec>(() => clone(base.spec));
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const [versionsOpen, setVersionsOpen] = useState(false);
  /** Bumped by Discard: re-mounts the fields so their typed text resets too. */
  const [formKey, setFormKey] = useState(0);

  const setDraft = (fn: (d: AgentSpec) => void) =>
    setDraftState((prev) => {
      const next = clone(prev);
      fn(next);
      return next;
    });

  const issues = validateDraft(draft, catalog, base.spec);
  // M7 four-eyes: a tenant admin may still save a loosened guarded field; it becomes a change request.
  const loosened = platform ? [] : guardedChanges(base.spec, draft);
  // M8: taking a factory off the compliance block list is guarded the same way.
  const unblocked = platform ? [] : blockedFactoriesRemoved(base.spec, draft);
  const guardReasons = [
    ...loosened.map((f) => guardedMessage(base.spec, f)),
    ...(unblocked.length ? [blockedFactoriesMessage(unblocked)] : []),
  ];
  // Compared and patched in normal form: an emptied addendum / display name means "none".
  const baseNorm = normalizeSpec(base.spec);
  const draftNorm = normalizeSpec(draft);
  const dirty = !jsonEqual(baseNorm, draftNorm);
  const patch = dirty ? mergePatch(baseNorm, draftNorm) : {};
  const blocked = Object.keys(issues).length > 0;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const res = await admin.saveSpec(base.version, patch);
      setReviewOpen(false);
      if (res.kind === "change_request") {
        announceChangeRequest(res.request, router.push);
        // Nothing changed yet: show the active settings again (the request waits on Pending changes).
        setDraftState(clone(base.spec));
        setFormKey((k) => k + 1);
        return;
      }
      const out = res.value;
      if (out.unchanged) toast("Nothing to save", { description: `The settings are the same as ${out.version}.` });
      else toast.success(`Saved as ${out.version}`, { description: "New runs use these settings." });
      onSaved();
    } catch (err) {
      if (isApiError(err) && err.status === 409) setConflict(true);
      setError(adminError(err));
      setReviewOpen(false);
    } finally {
      setSaving(false);
    }
  };

  const guardNote = platform ? undefined : "Raising it needs a second admin; lowering applies right away.";
  const quorumNote = platform ? undefined : "Lowering it needs a second admin; raising applies right away.";
  const policy = draft.policy;
  const budgets = draft.budgets;

  return (
    <>
      <AdminPageHeader
        title="Agents"
        description={
          <>
            Models, guidance and tools per agent. Active version <Badge size="sm" tone="accent">{base.version}</Badge>
            {base.created_at ? ` · saved ${when(base.created_at)} by ${personName(base.created_by)}` : null}
          </>
        }
        actions={
          <Button size="sm" variant="secondary" icon={<History className="size-4" />} onClick={() => setVersionsOpen(true)}>
            Versions
          </Button>
        }
      />
      <AgentsSubnav />

      {error ? (
        <div className="flex flex-col gap-2">
          <InlineError message={error} />
          {conflict ? (
            <div>
              <Button size="sm" onClick={onSaved}>
                Load the latest version
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}

      <Fragment key={formKey}>
      <AdminSection title="Agents and models" description="Each agent uses a model alias from the AI Gateway.">
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {MODEL_ROLES.map((role) => (
            <RoleCard key={role} role={role} draft={draft} baseAlias={base.spec.models[role]} catalog={catalog} issues={issues} setDraft={setDraft} />
          ))}
        </div>
      </AdminSection>

      <AdminSection title="Tools" description="Only approved, read-only tools that return trusted data can be given to agents.">
        <div className="flex flex-col gap-5">
          {draft.specialists.map((s, i) => (
            <div key={s.slug} className="flex flex-col gap-3 border-b border-border pb-4 last:border-b-0 last:pb-0">
              <h3 className="text-ui font-semibold text-fg">{s.display_name || s.slug.replace(/_/g, " ")}</h3>
              <div className="grid gap-3 sm:grid-cols-2">
                <Field label="Display name" description="Shown in the thinking timeline." error={issues[`specialists.${s.slug}.display_name`]}>
                  <TextInput
                    value={s.display_name ?? ""}
                    maxLength={DISPLAY_NAME_MAX}
                    placeholder={s.slug.replace(/_/g, " ")}
                    onChange={(e) =>
                      setDraft((d) => {
                        d.specialists[i]!.display_name = e.target.value || null;
                      })
                    }
                  />
                </Field>
                <NumberField
                  label="Max tool calls per run"
                  value={s.max_tool_calls}
                  min={1}
                  max={MAX_TOOL_CALLS}
                  error={issues[`specialists.${s.slug}.max_tool_calls`]}
                  onChange={(n) =>
                    setDraft((d) => {
                      d.specialists[i]!.max_tool_calls = n;
                    })
                  }
                />
              </div>
              <ToolAllowlist
                legend="Allowed tools"
                connector={s.connector ?? draft.connector ?? "merxa"}
                selected={s.tools}
                catalog={catalog}
                error={issues[`specialists.${s.slug}.tools`]}
                onChange={(tools) =>
                  setDraft((d) => {
                    d.specialists[i]!.tools = tools;
                  })
                }
              />
            </div>
          ))}
          <div className="flex flex-col gap-3">
            <h3 className="text-ui font-semibold text-fg">Answer path</h3>
            <ToolAllowlist
              legend="Tools for direct questions"
              connector={draft.answer_connector ?? draft.connector ?? "merxa"}
              selected={draft.answer_tools}
              catalog={catalog}
              error={issues["answer_tools"]}
              onChange={(tools) =>
                setDraft((d) => {
                  d.answer_tools = tools;
                })
              }
            />
          </div>
        </div>
      </AdminSection>

      <AdminSection title="Budgets and policy" description="Limits per run and when changes may apply without a person approving.">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          <NumberField
            label="Max model calls per run"
            value={budgets.max_llm_calls}
            min={1}
            error={issues["budgets.max_llm_calls"]}
            onChange={(n) => setDraft((d) => void (d.budgets = { ...d.budgets, max_llm_calls: n }))}
          />
          <NumberField
            label="Max tokens per run"
            value={budgets.max_total_tokens}
            min={1000}
            error={issues["budgets.max_total_tokens"]}
            onChange={(n) => setDraft((d) => void (d.budgets = { ...d.budgets, max_total_tokens: n }))}
          />
          <NumberField
            label="Approvals needed"
            value={policy.approval_quorum}
            min={platform ? 1 : (base.spec.policy.approval_quorum ?? 1)}
            lockedReason={quorumNote}
            error={issues["policy.approval_quorum"]}
            onChange={(n) => setDraft((d) => void (d.policy = { ...d.policy, approval_quorum: n }))}
          />
          <NumberField
            label="Auto-apply limit (lines)"
            description="Change sets up to this many lines may apply without approval."
            value={policy.auto_max_lines}
            min={0}
            max={platform ? undefined : base.spec.policy.auto_max_lines}
            lockedReason={guardNote}
            error={issues["policy.auto_max_lines"]}
            onChange={(n) => setDraft((d) => void (d.policy = { ...d.policy, auto_max_lines: n }))}
          />
          <NumberField
            label="Auto-apply limit (units)"
            description="…and up to this many units in total."
            value={policy.auto_max_units}
            min={0}
            max={platform ? undefined : base.spec.policy.auto_max_units}
            lockedReason={guardNote}
            error={issues["policy.auto_max_units"]}
            onChange={(n) => setDraft((d) => void (d.policy = { ...d.policy, auto_max_units: n }))}
          />
          <NumberField
            label="Auto-confirm up to (matched records)"
            description="Approach cards with fewer matches are confirmed automatically."
            value={policy.auto_confirm_max_matched}
            min={0}
            error={issues["policy.auto_confirm_max_matched"]}
            onChange={(n) => setDraft((d) => void (d.policy = { ...d.policy, auto_confirm_max_matched: n }))}
          />
        </div>
      </AdminSection>

      <PlanningSolverSection
        draft={draft}
        issues={issues}
        setDraft={setDraft}
        guardNote={platform ? undefined : "Removing a factory needs a second admin; adding one applies right away."}
      />
      </Fragment>

      <div
        className="sticky bottom-0 z-10 -mx-4 flex flex-wrap items-center gap-2 border-t border-border bg-bg/95 px-4 py-2.5 backdrop-blur md:-mx-6 md:px-6"
        role="region"
        aria-label="Save changes"
      >
        <div className="flex min-w-0 flex-1 basis-72 flex-col gap-2">
          <SecondAdminBanner reasons={guardReasons} />
          <p className="text-caption text-fg-muted" aria-live="polite">
            {dirty ? `Unsaved changes: ${patchSummary(patch)}.` : "No unsaved changes."}
            {dirty && blocked ? " Fix the highlighted fields to save." : ""}
          </p>
        </div>
        <Button
          size="sm"
          variant="ghost"
          disabled={!dirty || saving}
          onClick={() => {
            setDraftState(clone(base.spec));
            setFormKey((k) => k + 1);
          }}
        >
          Discard
        </Button>
        <Button size="sm" variant="primary" icon={<Save className="size-4" />} disabled={!dirty || blocked} loading={saving} onClick={() => setReviewOpen(true)}>
          {guardReasons.length ? "Review and request approval" : "Review and save"}
        </Button>
      </div>

      <Dialog
        open={reviewOpen}
        onOpenChange={setReviewOpen}
        title={guardReasons.length ? "Request approval for a new version?" : "Save a new version?"}
        description={
          guardReasons.length
            ? `Based on ${base.version}. Another admin must approve it on Pending changes before new runs use it.`
            : `Based on ${base.version}. New runs use the new version; runs in progress keep theirs.`
        }
        className="w-[min(760px,calc(100vw-2rem))]"
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setReviewOpen(false)}>
              Keep editing
            </Button>
            <Button size="sm" variant="primary" loading={saving} onClick={() => void save()}>
              {guardReasons.length ? "Send for approval" : "Save version"}
            </Button>
          </>
        }
      >
        <JsonDiff caption={`Changes from ${base.version}`} before={baseNorm} after={draftNorm} beforeLabel={base.version} afterLabel="New version" ignorePaths={DIFF_IGNORE} />
      </Dialog>

      <VersionsDrawer open={versionsOpen} onOpenChange={setVersionsOpen} current={base} onRestored={onSaved} />
    </>
  );
}

export function AgentsPage() {
  const admin = useAdminApi();
  const spec = useResource(() => admin.getSpec(), "spec");
  const catalog = useResource(() => admin.catalog(), "catalog");
  if (spec.data && catalog.data) {
    // Re-mount the editor whenever a new version is loaded (fresh draft, fresh base).
    return <SpecEditor key={spec.data.version} base={spec.data} catalog={catalog.data} onSaved={() => spec.reload()} />;
  }
  return (
    <>
      <AdminPageHeader title="Agents" description="Models, guidance and tools per agent." />
      <AgentsSubnav />
      {spec.error ? (
        <ResourceView resource={spec} label="the agent settings">
          {() => null}
        </ResourceView>
      ) : catalog.error ? (
        <ResourceView resource={catalog} label="the tool catalog">
          {() => null}
        </ResourceView>
      ) : (
        <div role="status" aria-label="Loading agent settings" className="flex items-center gap-2 text-ui text-fg-muted">
          <Spinner size={16} /> Loading agent settings…
        </div>
      )}
    </>
  );
}
