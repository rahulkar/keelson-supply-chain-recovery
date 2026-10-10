"use client";

import { Badge, Button, Checkbox, ConfirmDialog, Field, Select, Switch, Table, TextInput, Textarea, toast } from "@keelson/ui";
import { Plus, Save, Trash2 } from "lucide-react";
import { useId, useState, type FormEvent } from "react";
import { adminError, int, when } from "@/lib/admin/format";
import {
  ALERT_ACTION_LABELS,
  MAX_WATCH_REGIONS,
  NODE_CODE_PATTERN,
  NODE_TYPE_LABEL,
  NODE_TYPE_OPTIONS,
  RADIUS_KM,
  REGION_NODE_TYPES,
  SIGNAL_KIND_OPTIONS,
  SOURCE_STATUS_TONE,
  bpToPct,
  cadenceFromOption,
  cadenceLabel,
  cadenceOptions,
  hubErrorText,
  newRule,
  parsePolygon,
  pctToBp,
  regionIdFromName,
  regionSummary,
  ruleProblems,
  type AlertRule,
  type NetworkCodes,
  type RegionNodeType,
  type SignalSource,
  type SourceSettingPatch,
  type WatchRegion,
} from "@/lib/admin/signals";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { hasAnyRole } from "@/lib/roles";
import { DemoPacksPanel } from "../shell/DemoPacks";
import { AdminPageHeader, AdminSection, InlineError, ResourceView } from "./common";
import { SignalsClusteringSection } from "./SignalsClusteringSection";

const KIND_LABELS: Record<string, string> = {
  natural_hazard: "Natural hazard",
  weather: "Weather",
  geopolitical: "Geopolitical",
  labor: "Labour",
  logistics: "Logistics",
  regulatory: "Regulatory",
  cyber: "Cyber",
  supplier: "Supplier",
  demand: "Demand",
  hypothetical: "Hypothetical",
};
const NODE_LABELS: Record<string, string> = { supplier: "Suppliers", factory: "Factories", port: "Ports", dc: "DCs", store: "Stores", lane: "Lanes" };

// ───────────────────────────── sources ─────────────────────────────

function SourcesSection() {
  const admin = useAdminApi();
  const list = useResource(() => admin.signalSources(), "signal-sources");
  const [busy, setBusy] = useState<string | null>(null);

  const update = async (s: SignalSource, patch: SourceSettingPatch, done: string) => {
    setBusy(s.id);
    try {
      const out = await admin.setSignalSource(s.id, patch);
      list.setData((prev) =>
        prev
          ? {
              ...prev,
              sources: prev.sources.map((x) =>
                x.id === s.id
                  ? {
                      ...x,
                      enabled: out?.enabled ?? patch.enabled ?? x.enabled,
                      cadence_s: out && "cadence_s" in out ? (out.cadence_s ?? null) : patch.cadence_s !== undefined ? patch.cadence_s : x.cadence_s,
                    }
                  : x,
              ),
            }
          : prev,
      );
      const warn = out && out.hub_synced === false ? hubErrorText(out.hub_error) : null;
      if (warn) toast(done, { description: warn });
      else toast.success(done);
    } catch (err) {
      toast.error(`Couldn't update ${s.name}`, { description: adminError(err) });
    } finally {
      setBusy(null);
    }
  };
  const subscribe = (s: SignalSource, enabled: boolean) => update(s, { enabled }, `${s.name}: ${enabled ? "subscribed" : "unsubscribed"}`);
  const setCadence = (s: SignalSource, value: string) => {
    const cadence = cadenceFromOption(value);
    if (cadence === undefined || cadence === (s.cadence_s ?? null)) return;
    void update(s, { cadence_s: cadence }, `${s.name}: ${cadence === null ? "default cadence" : cadenceLabel(cadence)}`);
  };

  return (
    <AdminSection title="Sources" description="Public feeds and connected systems the Signal Hub watches. Turn a source off to stop its alerts for this workspace. Licences are shown wherever their data appears.">
      <ResourceView resource={list} label="signal sources">
        {({ sources, hubError }) => (
          <div className="flex flex-col gap-3">
            {hubError ? <InlineError message={hubErrorText(hubError) ?? "The Signal Hub couldn't be reached."} /> : null}
            <Table<SignalSource>
              caption="Signal sources"
              hideCaption
              rows={sources}
              rowKey={(s) => s.id}
              rowTone={(s) => (s.enabled ? (s.last_status === "error" ? "warning" : undefined) : "muted")}
              empty="No sources reported by the Signal Hub."
              columns={[
                {
                  id: "name",
                  header: "Source",
                  rowHeader: true,
                  cell: (s) => (
                    <span className="flex flex-col text-left">
                      <span className="font-medium">{s.name}</span>
                      {s.name !== s.id ? <span className="font-mono text-caption font-normal text-fg-muted">{s.id}</span> : null}
                    </span>
                  ),
                },
                { id: "licence", header: "Licence", cell: (s) => s.licence ?? s.attribution ?? "—" },
                {
                  id: "status",
                  header: "Last poll",
                  cell: (s) => (
                    <span className="flex flex-col gap-0.5">
                      <span className="flex items-center gap-1.5">
                        {s.last_status ? (
                          <Badge size="sm" tone={SOURCE_STATUS_TONE[s.last_status] ?? "neutral"} dot>
                            {s.last_status.replace(/_/g, " ")}
                          </Badge>
                        ) : null}
                        <span className="text-caption">{when(s.last_polled_at)}</span>
                      </span>
                      {s.consecutive_failures ? <span className="text-caption text-danger">{s.consecutive_failures} failures in a row</span> : null}
                      {s.last_error ? <span className="text-caption break-words text-fg-muted">{s.last_error}</span> : null}
                    </span>
                  ),
                },
                { id: "items", header: "Items", align: "end", cell: (s) => int(s.items_total ?? null) },
                {
                  id: "cadence",
                  header: "Cadence",
                  nowrap: true,
                  cell: (s) => (
                    <span className="flex flex-col gap-0.5">
                      <Field label={`Polling cadence of ${s.name}`} hideLabel>
                        <Select value={s.cadence_s ? String(s.cadence_s) : ""} disabled={busy === s.id} options={cadenceOptions(s)} onChange={(v) => setCadence(s, v)} />
                      </Field>
                      {s.effective_cadence_s && s.effective_cadence_s !== (s.cadence_s ?? s.default_cadence_s) ? (
                        <span className="text-caption text-fg-muted">Polled {cadenceLabel(s.effective_cadence_s)}</span>
                      ) : null}
                    </span>
                  ),
                },
                {
                  id: "enabled",
                  header: "Subscribed",
                  cell: (s) => (
                    <Switch
                      checked={s.enabled}
                      disabled={busy === s.id}
                      aria-label={`${s.enabled ? "Unsubscribe from" : "Subscribe to"} ${s.name}`}
                      onCheckedChange={(on) => void subscribe(s, on)}
                    />
                  ),
                },
              ]}
            />
          </div>
        )}
      </ResourceView>
    </AdminSection>
  );
}

// ───────────────────────────── watch regions ─────────────────────────────

function RegionForm({ taken, network, onAdd }: { taken: readonly string[]; network: NetworkCodes | null; onAdd: (r: WatchRegion) => Promise<boolean> }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"radius" | "polygon">("radius");
  const [nodeType, setNodeType] = useState<RegionNodeType>("port");
  const [node, setNode] = useState("");
  const [radius, setRadius] = useState("150");
  const [polygon, setPolygon] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const r = Number(radius);
  const poly = kind === "polygon" ? parsePolygon(polygon) : null;
  const trimmedName = name.trim();
  const code = node.trim();
  const listId = useId();
  const codes = network?.[nodeType] ?? [];
  const errors = {
    name: trimmedName ? (trimmedName.length > 120 ? "At most 120 characters." : null) : "Name the region.",
    node:
      kind === "radius"
        ? !code
          ? "Enter the site code."
          : code.length > 120 || !NODE_CODE_PATTERN.test(code)
            ? "Letters, digits and . _ : / # > - (starts with a letter or digit)."
            : null
        : null,
    radius: kind === "radius" ? (Number.isInteger(r) && r >= RADIUS_KM.min && r <= RADIUS_KM.max ? null : `A whole number between ${RADIUS_KM.min} and ${RADIUS_KM.max} km.`) : null,
    polygon: poly && "error" in poly ? poly.error : null,
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (Object.values(errors).some(Boolean)) return;
    const region: WatchRegion = {
      id: regionIdFromName(trimmedName, taken),
      name: trimmedName.replace(/\s+/g, " "),
      kind,
      enabled: true,
      node: kind === "radius" ? { type: nodeType, code } : null,
      radius_km: kind === "radius" ? r : null,
      polygon: kind === "polygon" && poly && "ring" in poly ? { type: "Polygon", coordinates: [poly.ring] } : null,
    };
    setBusy(true);
    const ok = await onAdd(region);
    setBusy(false);
    if (ok) {
      setName("");
      setNode("");
      setPolygon("");
      setSubmitted(false);
    }
  };

  return (
    <form onSubmit={(e) => void submit(e)} noValidate className="flex flex-col gap-3 rounded-sm border border-border p-3" aria-label="Add a watch region">
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Name" required error={submitted ? errors.name : undefined}>
          <TextInput value={name} maxLength={120} onChange={(e) => setName(e.target.value)} placeholder="e.g. Cat Lai port area" />
        </Field>
        <Field label="Shape">
          <Select
            value={kind}
            onChange={(v) => setKind(v === "polygon" ? "polygon" : "radius")}
            options={[
              { value: "radius", label: "Radius around a site" },
              { value: "polygon", label: "Polygon" },
            ]}
          />
        </Field>
      </div>
      {kind === "radius" ? (
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Site type">
            <Select value={nodeType} onChange={(v) => setNodeType(v as RegionNodeType)} options={REGION_NODE_TYPES.map((t) => ({ value: t, label: NODE_TYPE_LABEL[t] }))} />
          </Field>
          <Field
            label="Site code"
            required
            error={submitted ? errors.node : undefined}
            description={
              !codes.length
                ? "As in your network (e.g. VNSGN)."
                : code && !codes.includes(code)
                  ? `Not a ${NODE_TYPE_LABEL[nodeType].toLowerCase()} of your current network: it is watched once the site appears.`
                  : `${codes.length} ${NODE_TYPE_LABEL[nodeType].toLowerCase()} ${codes.length === 1 ? "site" : "sites"} in your network: start typing to pick one.`
            }
          >
            <TextInput value={node} mono maxLength={120} autoComplete="off" list={codes.length ? listId : undefined} onChange={(e) => setNode(e.target.value)} />
          </Field>
          {codes.length ? (
            <datalist id={listId}>
              {codes.map((c) => (
                <option key={c} value={c} />
              ))}
            </datalist>
          ) : null}
          <Field label="Radius (km)" required error={submitted ? errors.radius : undefined}>
            <TextInput value={radius} inputMode="numeric" onChange={(e) => setRadius(e.target.value)} />
          </Field>
        </div>
      ) : (
        <Field label="Points" required error={submitted ? errors.polygon : undefined} description='One "longitude, latitude" per line, or a GeoJSON polygon.'>
          <Textarea value={polygon} rows={4} className="font-mono text-caption" onChange={setPolygon} />
        </Field>
      )}
      <div>
        <Button size="sm" type="submit" icon={<Plus className="size-4" />} loading={busy}>
          Add region
        </Button>
      </div>
    </form>
  );
}

function WatchRegionsSection() {
  const admin = useAdminApi();
  const regions = useResource(() => admin.watchRegions(), "watch-regions");
  // Site codes to pick from (optional: without them the code is typed).
  const network = useResource(() => admin.signalNetwork(), "signal-network");
  const [removing, setRemoving] = useState<WatchRegion | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const save = async (next: WatchRegion[], done: string): Promise<boolean> => {
    setError(null);
    try {
      const out = await admin.saveWatchRegions(next);
      regions.setData(() => out.regions ?? next);
      const warn = out.hubSynced === false ? hubErrorText(out.hubError) : null;
      if (warn) toast(done, { description: warn });
      else toast.success(done);
      return true;
    } catch (err) {
      setError(adminError(err));
      return false;
    }
  };

  return (
    <AdminSection title="Watch regions" description="Extra areas to watch besides your network's sites: a polygon, or a radius around a named site. Events inside them raise alerts like exposed sites do.">
      {error ? <InlineError message={error} /> : null}
      <ResourceView resource={regions} label="watch regions">
        {(rows) => (
          <div className="flex flex-col gap-3">
            <Table<WatchRegion>
              caption="Watch regions"
              hideCaption
              rows={rows}
              rowKey={(r) => r.id}
              rowTone={(r) => (r.enabled ? undefined : "muted")}
              empty="No extra regions: only your network's sites are watched."
              columns={[
                { id: "name", header: "Region", rowHeader: true, cell: (r) => r.name },
                { id: "shape", header: "Shape", cell: (r) => regionSummary(r) },
                {
                  id: "enabled",
                  header: "Watched",
                  cell: (r) => (
                    <Switch
                      checked={r.enabled}
                      disabled={busy !== null}
                      aria-label={`${r.enabled ? "Stop watching" : "Watch"} ${r.name}`}
                      onCheckedChange={async (on) => {
                        setBusy(r.id);
                        await save(
                          rows.map((x) => (x.id === r.id ? { ...x, enabled: on } : x)),
                          on ? `Watching ${r.name}` : `Paused ${r.name}`,
                        );
                        setBusy(null);
                      }}
                    />
                  ),
                },
                {
                  id: "actions",
                  header: "Actions",
                  srOnlyHeader: true,
                  cell: (r) => (
                    <Button size="sm" variant="ghost" icon={<Trash2 className="size-3.5" />} aria-label={`Remove ${r.name}`} onClick={() => setRemoving(r)}>
                      Remove
                    </Button>
                  ),
                },
              ]}
            />
            {rows.length < MAX_WATCH_REGIONS ? (
              <RegionForm taken={rows.map((r) => r.id)} network={network.data ?? null} onAdd={(r) => save([...rows, r], `Watching ${r.name}`)} />
            ) : (
              <p className="text-caption text-fg-muted">At most {MAX_WATCH_REGIONS} regions.</p>
            )}
          </div>
        )}
      </ResourceView>
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(o) => (o ? undefined : setRemoving(null))}
        title={`Stop watching ${removing?.name ?? ""}?`}
        description="New events inside this region no longer raise alerts on their own."
        confirmLabel="Remove region"
        tone="danger"
        onConfirm={async () => {
          if (!removing) return;
          const ok = await save((regions.data ?? []).filter((r) => r.id !== removing.id), `Stopped watching ${removing.name}`);
          if (ok) setRemoving(null);
          return ok;
        }}
      />
    </AdminSection>
  );
}

// ───────────────────────────── alert rules ─────────────────────────────

function PctField({ label, bp, onChange, description }: { label: string; bp: number; onChange: (bp: number) => void; description?: string | undefined }) {
  const [text, setText] = useState(String(bpToPct(bp)));
  return (
    <Field label={label} description={description}>
      <TextInput
        inputMode="decimal"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          const n = Number(e.target.value);
          onChange(e.target.value.trim() === "" || !Number.isFinite(n) ? Number.NaN : pctToBp(n));
        }}
      />
    </Field>
  );
}

function RuleEditor({ rule, index, problems, onChange, onRemove }: { rule: AlertRule; index: number; problems: string[]; onChange: (r: AlertRule) => void; onRemove: () => void }) {
  const w = rule.when;
  const set = (patch: Partial<AlertRule["when"]>) => onChange({ ...rule, when: { ...w, ...patch } });
  const toggle = <T extends string>(list: readonly T[], v: T, on: boolean): T[] => (on ? [...list, v] : list.filter((x) => x !== v));
  return (
    <li className="flex flex-col gap-3 rounded-sm border border-border p-3" data-alert-rule={rule.id}>
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Rule id" className="w-48">
          <TextInput value={rule.id} mono maxLength={64} onChange={(e) => onChange({ ...rule, id: e.target.value.trim() })} />
        </Field>
        <Field label="Then" className="w-56">
          <Select
            value={rule.action}
            onChange={(v) => onChange({ ...rule, action: v as AlertRule["action"] })}
            options={Object.entries(ALERT_ACTION_LABELS).map(([value, label]) => ({ value, label }))}
          />
        </Field>
        <span className="ml-auto" />
        <Button size="sm" variant="ghost" icon={<Trash2 className="size-3.5" />} aria-label={`Remove rule ${rule.id || index + 1}`} onClick={onRemove}>
          Remove
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <PctField label="Minimum severity (%)" bp={w.min_severity_bp} onChange={(min_severity_bp) => set({ min_severity_bp })} />
        <PctField label="Minimum confidence (%)" bp={w.min_confidence_bp} onChange={(min_confidence_bp) => set({ min_confidence_bp })} />
        <Field label="Minimum exposed sites">
          <TextInput inputMode="numeric" value={Number.isNaN(w.min_exposed_nodes) ? "" : String(w.min_exposed_nodes)} onChange={(e) => set({ min_exposed_nodes: e.target.value.trim() === "" ? Number.NaN : Number(e.target.value) })} />
        </Field>
        <PctField label="Hysteresis (%)" bp={rule.hysteresis_bp} description="How far a score must fall before the alert can be raised again." onChange={(hysteresis_bp) => onChange({ ...rule, hysteresis_bp })} />
      </div>
      <fieldset>
        <legend className="mb-1 text-caption font-medium text-fg-muted">Kinds (none = every kind)</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {SIGNAL_KIND_OPTIONS.map((k) => (
            <Checkbox key={k} checked={w.kinds.includes(k)} onCheckedChange={(on) => set({ kinds: toggle(w.kinds, k, on) })} label={KIND_LABELS[k] ?? k} />
          ))}
        </div>
      </fieldset>
      <fieldset>
        <legend className="mb-1 text-caption font-medium text-fg-muted">Exposed site types (none = any)</legend>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {NODE_TYPE_OPTIONS.map((k) => (
            <Checkbox key={k} checked={w.node_types.includes(k)} onCheckedChange={(on) => set({ node_types: toggle(w.node_types, k, on) })} label={NODE_LABELS[k] ?? k} />
          ))}
        </div>
      </fieldset>
      {problems.length ? (
        <ul className="text-caption text-danger" role="alert">
          {problems.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function AlertRulesSection() {
  const admin = useAdminApi();
  const rules = useResource(() => admin.alertRules(), "alert-rules");
  const [draft, setDraft] = useState<AlertRule[] | null>(null);
  const [formKey, setFormKey] = useState(0);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const current = draft ?? rules.data?.rules ?? [];
  const problems = ruleProblems(current);
  const dirty = draft !== null && JSON.stringify(draft) !== JSON.stringify(rules.data?.rules ?? []);
  const invalid = Object.keys(problems).length > 0;

  const save = async () => {
    if (!draft || invalid) return;
    setSaving(true);
    setError(null);
    try {
      const out = await admin.saveAlertRules(draft);
      rules.setData((prev) => out ?? (prev ? { ...prev, rules: draft, custom: draft.length > 0 } : prev));
      setDraft(null);
      setFormKey((k) => k + 1);
      if (out?.hubSynced === false) toast("Alert rules saved", { description: hubErrorText("unreachable") ?? undefined });
      else toast.success("Alert rules saved", { description: "New events are checked against these rules." });
    } catch (err) {
      setError(adminError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <AdminSection
      title="Alert rules"
      description="When an event exposing your network raises an alert, and when it also drafts a case (the case still waits at the approach card). The first matching rule wins."
      actions={
        <>
          {dirty ? (
            <Button
              size="sm"
              variant="ghost"
              onClick={() => {
                setDraft(null);
                setFormKey((k) => k + 1);
              }}
            >
              Discard
            </Button>
          ) : null}
          <Button size="sm" variant="secondary" icon={<Plus className="size-4" />} onClick={() => setDraft([...current, newRule(current)])}>
            Add rule
          </Button>
          <Button size="sm" variant="primary" icon={<Save className="size-4" />} disabled={!dirty || invalid} loading={saving} onClick={() => void save()}>
            Save rules
          </Button>
        </>
      }
    >
      {error ? <InlineError message={error} /> : null}
      <ResourceView resource={rules} label="alert rules">
        {(data) => (
          <>
            {!data.custom && draft === null ? <p className="text-caption text-fg-muted">Using the default rules. Saving a change stores your own.</p> : null}
            {current.length === 0 ? <p className="text-ui text-fg-muted">No rules: nothing raises an alert. Add one, or save an empty list to go back to the defaults.</p> : null}
            <ol key={formKey} aria-label="Alert rules in order" className="flex flex-col gap-3">
              {current.map((r, i) => (
                <RuleEditor
                  key={i}
                  rule={r}
                  index={i}
                  problems={problems[i] ?? []}
                  onChange={(next) => setDraft(current.map((x, j) => (j === i ? next : x)))}
                  onRemove={() => {
                    // Fields keep their typed text per position: re-mount them after a removal.
                    setDraft(current.filter((_, j) => j !== i));
                    setFormKey((k) => k + 1);
                  }}
                />
              ))}
            </ol>
          </>
        )}
      </ResourceView>
    </AdminSection>
  );
}

/**
 * **Signals** (`docs/contracts/110` §5): sources (subscription, cadence, licence), watch regions,
 * alert rules and — DEMO only — the scenario pack launcher. The engine proxies these to the
 * Signal Hub with its service token.
 */
export function SignalsPage() {
  const { me } = useSession();
  const presenter = me.demo_mode && hasAnyRole(me.user, ["platform-admin", "demo-presenter"]);
  return (
    <>
      <AdminPageHeader title="Signals" description="What the Signal Hub watches for this workspace and when it raises an alert." />
      <SourcesSection />
      <WatchRegionsSection />
      <AlertRulesSection />
      <SignalsClusteringSection />
      {presenter ? (
        <AdminSection title="Demo scenarios" description="DEMO: replays recorded hazard, news and supplier updates through the real feeds. Everything they create is labelled DEMO.">
          <DemoPacksPanel active />
        </AdminSection>
      ) : null}
    </>
  );
}
