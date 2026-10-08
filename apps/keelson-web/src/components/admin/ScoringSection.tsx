"use client";

import { Badge, Button, Field, IconButton, Select, TextInput, toast } from "@keelson/ui";
import { Plus, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { isApiError } from "@/lib/api/client";
import { adminError } from "@/lib/admin/format";
import {
  bpToPercent,
  centsToEuros,
  emissionsInfo,
  eurosToCents,
  MAX_PROFILES,
  newProfileId,
  percentToBp,
  SAMPLES_MAX,
  SAMPLES_MIN,
  scoringFromSpec,
  scoringPatch,
  serverIssues,
  validateScoring,
  type ScoringDraft,
  type ScoringIssues,
  type WeightProfile,
} from "@/lib/admin/scoring";
import type { SpecEnvelope } from "@/lib/admin/types";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AdminSection, Facts, InlineError, ResourceView } from "./common";
import { announceChangeRequest } from "./SecondAdmin";

/** A profile as the form edits it: numbers stay text until saved, so "12." can be typed. */
type ProfileForm = { id: string; label: string; margin: string; cost: string; carbon: string; risk: string };

function toForm(p: WeightProfile): ProfileForm {
  return {
    id: p.id,
    label: p.label,
    margin: bpToPercent(p.lost_margin_bp),
    cost: bpToPercent(p.cost_bp),
    carbon: centsToEuros(p.carbon_price_cents_per_t),
    risk: bpToPercent(p.risk_aversion_bp),
  };
}

function fromForm(f: ProfileForm): WeightProfile {
  return {
    id: f.id,
    label: f.label,
    lost_margin_bp: percentToBp(f.margin),
    cost_bp: percentToBp(f.cost),
    carbon_price_cents_per_t: eurosToCents(f.carbon),
    risk_aversion_bp: percentToBp(f.risk),
  };
}

function ProfileEditor({
  form,
  index,
  issues,
  isDefault,
  canRemove,
  onChange,
  onRemove,
}: {
  form: ProfileForm;
  index: number;
  issues: ScoringIssues;
  isDefault: boolean;
  canRemove: boolean;
  onChange: (next: ProfileForm) => void;
  onRemove: () => void;
}) {
  const err = (field: string) => issues[`profiles.${index}.${field}`] ?? null;
  const set = (patch: Partial<ProfileForm>) => onChange({ ...form, ...patch });
  return (
    <li className="flex flex-col gap-3 rounded-md border border-border bg-surface-2 p-3" data-profile-row={form.id}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-ui font-medium text-fg">{form.label.trim() || "New profile"}</span>
        {isDefault ? (
          <Badge size="sm" tone="accent">
            Default
          </Badge>
        ) : null}
        <span className="ms-auto">
          <IconButton
            size="sm"
            label={`Remove ${form.label.trim() || form.id}`}
            icon={<Trash2 className="size-4" />}
            disabled={!canRemove}
            onClick={onRemove}
          />
        </span>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Name" error={err("label")}>
          <TextInput value={form.label} maxLength={60} onChange={(e) => set({ label: e.target.value })} />
        </Field>
        <Field label="Profile id" description="Used in the compare view; lower-case letters, digits, dashes." error={err("id")}>
          <TextInput value={form.id} mono maxLength={40} onChange={(e) => set({ id: e.target.value })} />
        </Field>
        <Field label="Weight of protected margin (%)" description="100% counts each euro of margin once." error={err("lost_margin_bp")}>
          <TextInput type="number" inputMode="decimal" min={0} max={500} step={0.01} value={form.margin} onChange={(e) => set({ margin: e.target.value })} />
        </Field>
        <Field label="Weight of plan cost (%)" description="Above 100% makes spending count more than margin." error={err("cost_bp")}>
          <TextInput type="number" inputMode="decimal" min={0} max={500} step={0.01} value={form.cost} onChange={(e) => set({ cost: e.target.value })} />
        </Field>
        <Field label="Carbon price (€ per tonne CO₂e)" description="Charged on the plan's extra transport CO₂e." error={err("carbon_price_cents_per_t")}>
          <TextInput type="number" inputMode="decimal" min={0} max={10000} step={0.01} value={form.carbon} onChange={(e) => set({ carbon: e.target.value })} />
        </Field>
        <Field
          label="Caution (%)"
          description="How much the pessimistic case (10th percentile of the sampled futures) counts instead of the expected one."
          error={err("risk_aversion_bp")}
        >
          <TextInput type="number" inputMode="decimal" min={0} max={100} step={0.01} value={form.risk} onChange={(e) => set({ risk: e.target.value })} />
        </Field>
      </div>
    </li>
  );
}

function ScoringEditor({ env, onSaved }: { env: SpecEnvelope; onSaved: () => void }) {
  const admin = useAdminApi();
  const router = useRouter();
  const base = useMemo(() => scoringFromSpec(env.spec), [env.spec]);
  const info = useMemo(() => emissionsInfo(env.spec), [env.spec]);
  const [forms, setForms] = useState<ProfileForm[]>(() => base.profiles.map(toForm));
  const [defaultProfile, setDefault] = useState(base.defaultProfile);
  const [samples, setSamples] = useState(String(base.samples));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [server, setServer] = useState<ScoringIssues>({});

  const draft: ScoringDraft = useMemo(
    () => ({ defaultProfile, profiles: forms.map(fromForm), samples: samples.trim() === "" ? Number.NaN : Number(samples) }),
    [defaultProfile, forms, samples],
  );
  const local = useMemo(() => validateScoring(draft), [draft]);
  const issues: ScoringIssues = { ...server, ...local };
  const patch = useMemo(() => scoringPatch(base, draft), [base, draft]);
  const dirty = Object.keys(patch).length > 0;
  const blocked = Object.keys(local).length > 0;

  const update = (index: number, next: ProfileForm) => {
    setServer({});
    const old = forms[index];
    if (old && old.id === defaultProfile && next.id !== old.id) setDefault(next.id); // renaming the default keeps it default
    setForms((prev) => prev.map((f, j) => (j === index ? next : f)));
  };
  const discard = () => {
    setForms(base.profiles.map(toForm));
    setDefault(base.defaultProfile);
    setSamples(String(base.samples));
    setServer({});
    setError(null);
  };
  const save = async () => {
    if (!dirty || blocked) return;
    setSaving(true);
    setError(null);
    setServer({});
    try {
      const res = await admin.saveSpec(env.version, patch);
      if (res.kind === "change_request") {
        announceChangeRequest(res.request, router.push);
        return;
      }
      if (res.value.unchanged) toast("Nothing to save", { description: "The scoring settings are the same as the saved ones." });
      else toast.success(`Option scoring saved (${res.value.version})`, { description: "New plans rank options with these profiles." });
      onSaved();
    } catch (err) {
      if (isApiError(err) && err.status === 409) setConflict(true);
      if (isApiError(err, "spec_invalid") && Array.isArray(err.body["errors"])) {
        setServer(serverIssues(err.body["errors"] as { loc?: unknown; msg?: unknown }[]));
      }
      setError(adminError(err));
    } finally {
      setSaving(false);
    }
  };

  const unitWeights = info.unitWeights.length
    ? info.unitWeights.map((w) => `${w.stylePrefix}… ${w.grams.toLocaleString("en-GB")} g`).join(" · ")
    : "No style rules";

  return (
    <div className="flex flex-col gap-3" data-scoring-section="">
      {error ? (
        <div className="flex flex-col gap-1">
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
      {issues["form"] ? <InlineError message={issues["form"]} /> : null}
      <div className="grid gap-3 sm:grid-cols-2">
        <Field
          label="Default profile"
          description="Sets the optimizer's weights and picks the recommended option. The others only rank the options in the compare view."
          error={issues["default"] ?? null}
        >
          <Select
            value={defaultProfile}
            onChange={(v) => {
              setServer({});
              setDefault(v);
            }}
            options={forms.map((f) => ({ value: f.id, label: f.label.trim() || f.id }))}
          />
        </Field>
        <Field
          label="Sampled futures per option"
          description={`How many futures each plan is re-run in for its likely ranges (${SAMPLES_MIN}–${SAMPLES_MAX}; more is steadier and slower).`}
          error={issues["samples"] ?? null}
        >
          <TextInput type="number" inputMode="numeric" min={SAMPLES_MIN} max={SAMPLES_MAX} step={1} value={samples} onChange={(e) => {
            setServer({});
            setSamples(e.target.value);
          }} />
        </Field>
      </div>
      <ol aria-label="Weight profiles" className="flex flex-col gap-2">
        {forms.map((f, i) => (
          <ProfileEditor
            key={i}
            form={f}
            index={i}
            issues={issues}
            isDefault={f.id === defaultProfile}
            canRemove={forms.length > 1 && f.id !== defaultProfile}
            onChange={(next) => update(i, next)}
            onRemove={() => setForms((prev) => prev.filter((_, j) => j !== i))}
          />
        ))}
      </ol>
      {issues["profiles"] ? <p className="text-caption text-danger">{issues["profiles"]}</p> : null}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          icon={<Plus className="size-4" />}
          disabled={forms.length >= MAX_PROFILES}
          onClick={() =>
            setForms((prev) => [
              ...prev,
              toForm({ id: newProfileId(prev.map(fromForm)), label: "", lost_margin_bp: 10_000, cost_bp: 10_000, carbon_price_cents_per_t: 0, risk_aversion_bp: 0 }),
            ])
          }
        >
          Add profile
        </Button>
        <span className="text-caption text-fg-subtle">
          {forms.length} of {MAX_PROFILES} profiles
        </span>
        <span className="ms-auto flex gap-2">
          {dirty ? (
            <Button size="sm" variant="ghost" onClick={discard} disabled={saving}>
              Discard
            </Button>
          ) : null}
          <Button size="sm" variant="primary" loading={saving} disabled={!dirty || blocked} onClick={() => void save()}>
            Save scoring
          </Button>
        </span>
      </div>
      <Facts
        className="mt-1"
        items={[
          ["CO₂e factors", `${info.factorsId} (platform data, not editable)${info.enabled ? "" : " · CO₂e is off"}`],
          ["Unit weights", unitWeights],
          ["Other units", `${info.defaultWeightG.toLocaleString("en-GB")} g each`],
          ["Likely ranges", info.robustnessEnabled ? "On" : "Off"],
        ]}
      />
    </div>
  );
}

/**
 * Policies page section **Option scoring** (M8, `docs/contracts/126` §6): the weight profiles the
 * options are ranked by, the default one, and the number of sampled futures behind the likely
 * ranges. Saved as a new agent-spec version (not a guarded change); a newer version saved by
 * someone else meanwhile is reported (409) with a reload button, invalid values inline (422).
 */
export function ScoringSection() {
  const admin = useAdminApi();
  const res = useResource(() => admin.getSpec(), "agent-spec:scoring");
  return (
    <AdminSection
      id="option-scoring"
      title="Option scoring"
      description="How plan options are ranked: weights for protected margin, plan cost and the plan's extra CO₂e, and how much the pessimistic case counts. Approval rules are not affected."
    >
      <ResourceView resource={res} label="option scoring">
        {(env) => <ScoringEditor key={env.version} env={env} onSaved={res.reload} />}
      </ResourceView>
    </AdminSection>
  );
}
