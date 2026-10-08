"use client";

import {
  Badge,
  Button,
  ConfirmDialog,
  Dialog,
  Field,
  Meter,
  Select,
  Switch,
  Table,
  TextInput,
  Textarea,
  toast,
  type TableColumn,
} from "@keelson/ui";
import { BarChart } from "@keelson/viz";
import { Check, ExternalLink, KeyRound, Lock, Pencil, Plus, Trash2, X } from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import type { GatewayModelInput } from "@/lib/admin/client";
import { adminError, eur, int, keyPrefix, shortDay } from "@/lib/admin/format";
import type { DailyUsage, GatewayModel, GatewayTenant, UsageRow } from "@/lib/admin/types";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { hasAnyRole } from "@/lib/roles";
import { AdminPageHeader, AdminSection, Facts, InlineError, ResourceView } from "./common";

const CAPABILITIES = [
  { key: "structured_output", label: "Structured output" },
  { key: "reasoning_stream", label: "Reasoning stream" },
  { key: "tool_loop_signatures", label: "Tool-loop signatures" },
] as const;

const ALIAS_PATTERN = /^(anthropic\/)?[a-z0-9][a-z0-9-]*$/;

/** Alias rules of 70 §2 (the server auto-prefixes `anthropic/` for Claude provider models). */
export function aliasError(alias: string): string | null {
  const a = alias.trim();
  if (!a) return "Enter an alias.";
  if (a.startsWith("gemini/") || a.startsWith("openai/")) return "Aliases never start with a provider prefix like gemini/ or openai/.";
  if (!ALIAS_PATTERN.test(a)) return "Lower-case letters, digits and hyphens; starts with a letter or digit.";
  return null;
}

function healthBadge(health: string | null | undefined) {
  const h = (health ?? "").toLowerCase();
  if (h === "healthy" || h === "ok") return <Badge size="sm" tone="success" dot>Healthy</Badge>;
  if (h === "unhealthy" || h === "down" || h === "error") return <Badge size="sm" tone="danger" dot>Unhealthy</Badge>;
  return <Badge size="sm" tone="neutral" dot>{h ? h.replace(/_/g, " ") : "Unknown"}</Badge>;
}

function CapabilityBadges({ model }: { model: GatewayModel }) {
  return (
    <ul className="flex flex-wrap gap-1" aria-label={`Capabilities of ${model.alias}`}>
      {CAPABILITIES.map((c) => {
        const on = model.capabilities?.[c.key] === true;
        return (
          <li key={c.key}>
            <Badge size="sm" tone={on ? "success" : "neutral"} variant={on ? "soft" : "outline"} icon={on ? <Check /> : <X />}>
              {c.label}
              <span className="sr-only">{on ? ": supported" : ": not supported"}</span>
            </Badge>
          </li>
        );
      })}
    </ul>
  );
}

// ───────────────────────────── model editor (platform-admin) ─────────────────────────────

type ModelDraft = { alias: string; provider_model: string; mode: string; params: string; grant_all_tenants: boolean };

export function parseParams(text: string): { ok: true; value: Record<string, unknown> } | { ok: false; message: string } {
  if (!text.trim()) return { ok: true, value: {} };
  try {
    const v: unknown = JSON.parse(text);
    if (!v || typeof v !== "object" || Array.isArray(v)) return { ok: false, message: "Params must be a JSON object." };
    return { ok: true, value: v as Record<string, unknown> };
  } catch {
    return { ok: false, message: "Params are not valid JSON." };
  }
}

function ModelDialog({
  open,
  onOpenChange,
  initial,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initial: GatewayModel | null;
  onSaved: () => void;
}) {
  const admin = useAdminApi();
  const [draft, setDraft] = useState<ModelDraft>(() => ({
    alias: initial?.alias ?? "",
    provider_model: initial?.provider_model ?? "",
    mode: initial?.mode ?? "chat",
    params: initial?.params ? JSON.stringify(initial.params, null, 2) : "",
    grant_all_tenants: false,
  }));
  const [submitted, setSubmitted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editing = initial !== null;
  const aErr = editing ? null : aliasError(draft.alias);
  const pmErr = draft.provider_model.trim() ? null : "Enter the provider model, e.g. vertex_ai/gemini-2.5-flash.";
  const params = parseParams(draft.params);
  const pErr = params.ok ? null : params.message;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (aErr || pmErr || !params.ok) return;
    setSaving(true);
    setError(null);
    const body: GatewayModelInput = {
      provider_model: draft.provider_model.trim(),
      mode: draft.mode,
      params: params.value,
      ...(draft.grant_all_tenants ? { grant_all_tenants: true } : {}),
    };
    try {
      await admin.upsertModel(draft.alias.trim(), body);
      toast.success(editing ? `Updated ${draft.alias}` : `Added ${draft.alias}`);
      onSaved();
      onOpenChange(false);
    } catch (err) {
      setError(adminError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={editing ? `Edit ${initial.alias}` : "Add a model alias"} description="Agents refer to models only by alias.">
      <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)} noValidate>
        <Field label="Alias" required error={submitted ? aErr : null} description={editing ? "Aliases can't be renamed." : "e.g. fast-default"}>
          <TextInput value={draft.alias} readOnly={editing} required mono onChange={(e) => setDraft({ ...draft, alias: e.target.value })} />
        </Field>
        <Field label="Provider model" required error={submitted ? pmErr : null}>
          <TextInput value={draft.provider_model} mono required onChange={(e) => setDraft({ ...draft, provider_model: e.target.value })} />
        </Field>
        <Field label="Mode">
          <Select
            value={draft.mode}
            onChange={(mode) => setDraft({ ...draft, mode })}
            options={[
              { value: "chat", label: "Chat" },
              { value: "responses", label: "Responses" },
              { value: "embedding", label: "Embedding" },
            ]}
          />
        </Field>
        <Field label="Params (JSON)" error={submitted ? pErr : null} description='e.g. {"temperature": 0.2}'>
          <Textarea value={draft.params} rows={3} className="font-mono" onChange={(params) => setDraft({ ...draft, params })} />
        </Field>
        <Switch
          checked={draft.grant_all_tenants}
          onCheckedChange={(grant_all_tenants) => setDraft({ ...draft, grant_all_tenants })}
          label="Make available to every workspace"
          description="Adds the alias to each tenant team's allowed models."
        />
        {error ? <InlineError message={error} /> : null}
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" type="submit" variant="primary" loading={saving}>
            {editing ? "Save" : "Add model"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

// ───────────────────────────── limits editor ─────────────────────────────

function LimitsForm({ tenant, platform, onSaved }: { tenant: GatewayTenant; platform: boolean; onSaved: (t: GatewayTenant | null) => void }) {
  const admin = useAdminApi();
  const [rpm, setRpm] = useState(String(tenant.rpm_limit ?? ""));
  const [tpm, setTpm] = useState(String(tenant.tpm_limit ?? ""));
  const [budget, setBudget] = useState(String(tenant.budget_eur ?? ""));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const num = (s: string, min: number) => {
    if (!s.trim()) return { value: undefined, error: null };
    const n = Number(s);
    return Number.isFinite(n) && n >= min ? { value: n, error: null } : { value: undefined, error: `At least ${min}.` };
  };
  const r = num(rpm, 1);
  const t = num(tpm, 1);
  const b = num(budget, 0);
  const patch = {
    ...(r.value !== undefined && r.value !== tenant.rpm_limit ? { rpm_limit: Math.floor(r.value) } : {}),
    ...(t.value !== undefined && t.value !== tenant.tpm_limit ? { tpm_limit: Math.floor(t.value) } : {}),
    ...(platform && b.value !== undefined && b.value !== tenant.budget_eur ? { budget_eur: b.value } : {}),
  };
  const dirty = Object.keys(patch).length > 0;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!dirty || r.error || t.error || b.error) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await admin.patchGatewayTenant(patch);
      toast.success("Limits saved");
      onSaved(updated);
    } catch (err) {
      setError(adminError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)} aria-label="Rate limits and budget" noValidate>
      <div className="grid gap-3 sm:grid-cols-3">
        <Field label="Requests per minute" error={r.error} description="Up to the platform maximum.">
          <TextInput type="number" inputMode="numeric" min={1} value={rpm} onChange={(e) => setRpm(e.target.value)} />
        </Field>
        <Field label="Tokens per minute" error={t.error} description="Up to the platform maximum.">
          <TextInput type="number" inputMode="numeric" min={1} value={tpm} onChange={(e) => setTpm(e.target.value)} />
        </Field>
        <Field
          label="Budget (€)"
          error={platform ? b.error : null}
          lockedReason={platform ? undefined : "Requires platform admin"}
          description={tenant.budget_duration ? `Per ${tenant.budget_duration}.` : undefined}
        >
          <TextInput type="number" inputMode="decimal" min={0} step="0.01" value={budget} disabled={!platform} onChange={(e) => setBudget(e.target.value)} />
        </Field>
      </div>
      {error ? <InlineError message={error} /> : null}
      <div>
        <Button size="sm" type="submit" variant="primary" disabled={!dirty} loading={saving}>
          Save limits
        </Button>
      </div>
    </form>
  );
}

// ───────────────────────────── usage ─────────────────────────────

/**
 * Spend per day (oldest first) and totals per alias. Days come from the engine's zero-filled
 * `daily` totals when given (every day of the window, quiet days included), else from the rows.
 */
export function summariseUsage(rows: readonly UsageRow[], daily?: readonly DailyUsage[] | null) {
  const byDay = new Map<string, number>();
  for (const d of daily ?? []) byDay.set(d.date, d.spend_eur);
  const fromRows = !daily || daily.length === 0;
  const byAlias = new Map<string, { alias: string; spend: number; requests: number; tokensIn: number; tokensOut: number }>();
  for (const r of rows) {
    if (fromRows) byDay.set(r.date, (byDay.get(r.date) ?? 0) + r.spend_eur);
    const alias = r.alias ?? "(all)";
    const a = byAlias.get(alias) ?? { alias, spend: 0, requests: 0, tokensIn: 0, tokensOut: 0 };
    a.spend += r.spend_eur;
    a.requests += r.requests;
    a.tokensIn += r.tokens_in;
    a.tokensOut += r.tokens_out;
    byAlias.set(alias, a);
  }
  const days = [...byDay.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([date, spend]) => ({ date, spend }));
  const aliases = [...byAlias.values()].sort((a, b) => b.spend - a.spend);
  return { days, aliases };
}

function UsageSection() {
  const admin = useAdminApi();
  const usage = useResource(() => admin.usageReport(7), "usage:7");
  return (
    <AdminSection title="Usage, last 7 days" description="Spend and traffic through your workspace's gateway team.">
      <ResourceView resource={usage} label="usage">
        {({ rows, daily }) => {
          const { days, aliases } = summariseUsage(rows, daily);
          if (aliases.length === 0 && days.every((d) => d.spend === 0)) return <p className="text-ui text-fg-muted">No usage in the last 7 days.</p>;
          return (
            <div className="grid gap-4 lg:grid-cols-2">
              <BarChart
                caption="Spend per day"
                unit="eur_cents"
                formatOptions={{ exactCents: true }}
                data={days.map((d) => ({ label: shortDay(d.date), value: Math.round(d.spend * 100) }))}
              />
              <Table
                caption="Usage per model alias"
                rows={aliases}
                rowKey={(a) => a.alias}
                columns={[
                  { id: "alias", header: "Alias", cell: (a) => <span className="font-mono text-caption">{a.alias}</span>, rowHeader: true, nowrap: true },
                  { id: "spend", header: "Spend", cell: (a) => eur(a.spend), align: "end", nowrap: true },
                  { id: "req", header: "Requests", cell: (a) => int(a.requests), align: "end" },
                  { id: "tin", header: "Tokens in", cell: (a) => int(a.tokensIn), align: "end" },
                  { id: "tout", header: "Tokens out", cell: (a) => int(a.tokensOut), align: "end" },
                ]}
              />
            </div>
          );
        }}
      </ResourceView>
    </AdminSection>
  );
}

// ───────────────────────────── page ─────────────────────────────

export function GatewayPage() {
  const admin = useAdminApi();
  const { me } = useSession();
  const platform = hasAnyRole(me.user, ["platform-admin"]);
  const models = useResource(() => admin.models(), "models");
  const tenant = useResource(() => admin.gatewayTenant(), "gw-tenant");
  const [editing, setEditing] = useState<{ model: GatewayModel | null } | null>(null);
  const [deleting, setDeleting] = useState<GatewayModel | null>(null);
  const [rotateOpen, setRotateOpen] = useState(false);

  const columns = useMemo(() => {
    const cols: TableColumn<GatewayModel>[] = [
      { id: "alias", header: "Alias", rowHeader: true, nowrap: true, cell: (m) => <span className="font-mono text-caption">{m.alias}</span> },
      { id: "mode", header: "Mode", cell: (m) => m.mode ?? "—" },
      { id: "caps", header: "Capabilities", cell: (m) => <CapabilityBadges model={m} /> },
      { id: "health", header: "Health", cell: (m) => healthBadge(m.health) },
    ];
    if (platform) {
      cols.push(
        { id: "provider", header: "Provider model", cell: (m) => <span className="font-mono text-caption break-all">{m.provider_model ?? "—"}</span> },
        {
          id: "params",
          header: "Params",
          cell: (m) => (m.params && Object.keys(m.params).length ? <code className="font-mono text-caption break-all">{JSON.stringify(m.params)}</code> : "—"),
        },
        {
          id: "actions",
          header: "Actions",
          srOnlyHeader: true,
          nowrap: true,
          cell: (m) => (
            <span className="flex gap-1">
              <Button size="sm" variant="ghost" icon={<Pencil className="size-3.5" />} aria-label={`Edit ${m.alias}`} onClick={() => setEditing({ model: m })}>
                Edit
              </Button>
              <Button size="sm" variant="ghost" icon={<Trash2 className="size-3.5" />} aria-label={`Delete ${m.alias}`} onClick={() => setDeleting(m)}>
                Delete
              </Button>
            </span>
          ),
        },
      );
    }
    return cols;
  }, [platform]);

  return (
    <>
      <AdminPageHeader
        title="AI Gateway"
        description="Models your agents can use, your workspace's budget and limits."
        actions={
          platform ? (
            <a
              href="/api/console/handoff?target=gateway"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex h-8 items-center gap-1.5 rounded-pill border border-border-strong px-3 text-caption font-medium text-fg hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            >
              Open advanced console
              <ExternalLink aria-hidden="true" className="size-3.5" />
              <span className="sr-only">(opens in a new tab)</span>
            </a>
          ) : null
        }
      />

      <AdminSection
        title="Models"
        description={platform ? "All aliases, with the provider model behind each." : "Aliases available to your workspace."}
        actions={
          platform ? (
            <Button size="sm" variant="secondary" icon={<Plus className="size-4" />} onClick={() => setEditing({ model: null })}>
              Add model
            </Button>
          ) : null
        }
      >
        <ResourceView resource={models} label="models">
          {(list) => <Table caption="Model aliases" hideCaption rows={list} rowKey={(m) => m.alias} columns={columns} empty="No models are available yet." />}
        </ResourceView>
      </AdminSection>

      <AdminSection
        title="Budget and limits"
        actions={
          <Button size="sm" variant="secondary" icon={<KeyRound className="size-4" />} onClick={() => setRotateOpen(true)}>
            Rotate key
          </Button>
        }
      >
        <ResourceView resource={tenant} label="the workspace budget">
          {(t) => (
            <div className="flex flex-col gap-4">
              {typeof t.budget_eur === "number" && t.budget_eur > 0 ? (
                <Meter label="Budget used" value={t.spend_eur ?? 0} max={t.budget_eur} valueText={`${eur(t.spend_eur ?? 0)} of ${eur(t.budget_eur)}`} />
              ) : (
                <p className="text-ui text-fg-muted">Spent {eur(t.spend_eur ?? 0)}; no budget set.</p>
              )}
              <Facts
                items={[
                  ["Gateway team", <span key="t" className="font-mono text-caption">{t.team_alias ?? "—"}</span>],
                  ["Virtual key", <span key="k" className="font-mono text-caption">{keyPrefix(t.key_prefix)}</span>],
                  ["Allowed models", t.models.length ? t.models.join(", ") : "—"],
                ]}
              />
              <LimitsForm key={`${t.rpm_limit}-${t.tpm_limit}-${t.budget_eur}`} tenant={t} platform={platform} onSaved={(u) => (u ? tenant.setData(() => u) : tenant.reload())} />
              {!platform ? (
                <p className="flex items-center gap-1.5 text-caption text-fg-subtle">
                  <Lock aria-hidden="true" className="size-3.5" /> The budget itself is set by a platform admin.
                </p>
              ) : null}
            </div>
          )}
        </ResourceView>
      </AdminSection>

      <UsageSection />

      {editing ? (
        <ModelDialog
          key={editing.model?.alias ?? "new"}
          open
          onOpenChange={(o) => (o ? undefined : setEditing(null))}
          initial={editing.model}
          onSaved={models.reload}
        />
      ) : null}

      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => (o ? undefined : setDeleting(null))}
        title={`Delete ${deleting?.alias ?? "model"}?`}
        description="Workspaces can no longer use this alias. Aliases used by an agent setting can't be deleted."
        confirmLabel="Delete alias"
        tone="danger"
        onConfirm={async () => {
          if (!deleting) return;
          try {
            await admin.deleteModel(deleting.alias);
            toast.success(`Deleted ${deleting.alias}`);
            models.reload();
          } catch (err) {
            toast.error("Couldn't delete the alias", { description: adminError(err) });
            return false;
          }
        }}
      />

      <ConfirmDialog
        open={rotateOpen}
        onOpenChange={setRotateOpen}
        title="Rotate the gateway key?"
        description="A new virtual key replaces the current one right away; the old key stops working. Runs in progress may fail once."
        confirmLabel="Rotate key"
        tone="danger"
        onConfirm={async () => {
          try {
            const out = await admin.rotateKey();
            toast.success("Key rotated", out.key_prefix ? { description: `New key ${keyPrefix(out.key_prefix)}` } : undefined);
            tenant.reload();
          } catch (err) {
            toast.error("Couldn't rotate the key", { description: adminError(err) });
            return false;
          }
        }}
      />
    </>
  );
}
