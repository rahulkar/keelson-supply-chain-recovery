"use client";

import { Button, Field, Select, Switch, Table, TextInput, toast } from "@keelson/ui";
import { Play, RotateCw, Save } from "lucide-react";
import { useMemo, useState } from "react";
import { adminError } from "@/lib/admin/format";
import {
  CLUSTERING_BOUNDS,
  COUNT_LABELS,
  ClusteringApi,
  THRESHOLD_LABELS,
  bpToPctText,
  clusteringProblems,
  clusteringSaveError,
  draftOf,
  pctTextToBp,
  remoteText,
  updateBody,
  type ClusteringDraft,
  type ClusteringView,
  type ThresholdKey,
  type ThresholdProfile,
} from "@/lib/admin/signalsClustering";
import { useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { hasAnyRole } from "@/lib/roles";
import { AdminSection, Facts, InlineError, PlatformOnlyNote, ResourceView } from "./common";

const LOCK = "Requires platform admin: clustering applies to every workspace";

function NumberField({ label, value, onChange, disabled, error, suffix }: {
  label: string;
  value: number;
  onChange: (n: number) => void;
  disabled: boolean;
  error?: string | undefined;
  suffix?: string;
}) {
  const [text, setText] = useState(Number.isNaN(value) ? "" : String(value));
  return (
    <Field label={suffix ? `${label} (${suffix})` : label} error={error} disabled={disabled}>
      <TextInput
        inputMode="numeric"
        value={text}
        disabled={disabled}
        onChange={(e) => {
          setText(e.target.value);
          const n = Number(e.target.value);
          onChange(e.target.value.trim() === "" || !Number.isFinite(n) ? Number.NaN : n);
        }}
      />
    </Field>
  );
}

function PercentField({ label, bp, onChange, disabled, error }: {
  label: string;
  bp: number;
  onChange: (bp: number) => void;
  disabled: boolean;
  error?: string | undefined;
}) {
  const [text, setText] = useState(bpToPctText(bp));
  return (
    <Field label={`${label} (%)`} error={error} disabled={disabled}>
      <TextInput
        inputMode="decimal"
        value={text}
        disabled={disabled}
        onChange={(e) => {
          setText(e.target.value);
          onChange(pctTextToBp(e.target.value));
        }}
      />
    </Field>
  );
}

function ClusteringForm({ view, editable, onSaved }: { view: ClusteringView; editable: boolean; onSaved: () => void }) {
  const { api } = useSession();
  const client = useMemo(() => new ClusteringApi(api), [api]);
  const [draft, setDraft] = useState<ClusteringDraft>(() => draftOf(view.config));
  const [busy, setBusy] = useState<"save" | "run" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const problems = clusteringProblems(draft);
  const invalid = Object.keys(problems).length > 0;
  const dirty = JSON.stringify(draft) !== JSON.stringify(draftOf(view.config));
  const locked = !editable;
  const setSim = (patch: Partial<ClusteringDraft["simhash"]>) => setDraft((d) => ({ ...d, simhash: { ...d.simhash, ...patch } }));
  const setVec = (patch: Partial<ClusteringDraft["vector"]>) => setDraft((d) => ({ ...d, vector: { ...d.vector, ...patch } }));
  const setThreshold = (profile: ThresholdProfile, key: ThresholdKey, bp: number) =>
    setDraft((d) => ({ ...d, vector: { ...d.vector, [profile]: { ...d.vector[profile], [key]: bp } } }));

  const save = async () => {
    if (locked || invalid || !dirty) return;
    setBusy("save");
    setError(null);
    try {
      const saved = await client.save(updateBody(view.config.version, draft));
      toast.success("Clustering settings saved", { description: `Version ${saved.version}: new reports are grouped with these settings.` });
      onSaved();
    } catch (err) {
      const known = clusteringSaveError(err);
      setStale(known !== null && known.startsWith("Settings changed"));
      setError(known ?? adminError(err));
    } finally {
      setBusy(null);
    }
  };

  const run = async () => {
    setBusy("run");
    try {
      const out = await client.run();
      toast.success("Clustering ran", { description: `${out.merged.length} event merge${out.merged.length === 1 ? "" : "s"}, ${out.embedded} report${out.embedded === 1 ? "" : "s"} embedded.` });
      onSaved();
    } catch (err) {
      toast.error("Couldn't run clustering", { description: adminError(err) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <form
      aria-label="Clustering settings"
      noValidate
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      {error ? <InlineError message={error} onRetry={stale ? onSaved : undefined} /> : null}
      {locked ? <PlatformOnlyNote>{LOCK}. You can see the values in effect.</PlatformOnlyNote> : null}
      <fieldset className="flex flex-col gap-3">
        <legend className="mb-1 text-ui font-medium text-fg">Near-duplicates (SimHash)</legend>
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          <Switch checked={draft.simhash.enabled} disabled={locked} label="Detect syndicated copies" onCheckedChange={(on) => setSim({ enabled: on })} />
          <Switch
            checked={draft.simhash.reuse_extraction}
            disabled={locked}
            label="Reuse the original's extraction"
            description="A copy naming the same places and companies needs no model call."
            onCheckedChange={(on) => setSim({ reuse_extraction: on })}
          />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Most differing bits (of 64)" error={problems["simhash.max_hamming_bits"]} disabled={locked}>
            <Select
              value={String(draft.simhash.max_hamming_bits)}
              disabled={locked}
              onChange={(v) => setSim({ max_hamming_bits: Number(v) })}
              options={Array.from({ length: CLUSTERING_BOUNDS.hammingBits.max + 1 }, (_, i) => ({ value: String(i), label: `${i} bit${i === 1 ? "" : "s"}` }))}
            />
          </Field>
          <NumberField label="Look back" suffix="hours" value={draft.simhash.window_hours} disabled={locked} error={problems["simhash.window_hours"]} onChange={(n) => setSim({ window_hours: n })} />
        </div>
      </fieldset>
      <fieldset className="flex flex-col gap-3">
        <legend className="mb-1 text-ui font-medium text-fg">Similar descriptions (vector attach and merge)</legend>
        <Switch checked={draft.vector.enabled} disabled={locked} label="Group reports v1 cannot place by similar cluster terms" onCheckedChange={(on) => setVec({ enabled: on })} />
        <div className="grid gap-3 sm:grid-cols-2">
          <NumberField label="Time window" suffix="hours" value={draft.vector.window_hours} disabled={locked} error={problems["vector.window_hours"]} onChange={(n) => setVec({ window_hours: n })} />
          <NumberField label="Farthest apart" suffix="km" value={draft.vector.max_distance_km} disabled={locked} error={problems["vector.max_distance_km"]} onChange={(n) => setVec({ max_distance_km: n })} />
        </div>
        {(["local", "remote"] as const).map((profile) => (
          <div key={profile} className="flex flex-col gap-2">
            <p className="text-caption font-medium text-fg-muted">
              {profile === "local" ? "Local term vectors (always used when reports arrive)" : "Remote embeddings (background merges only, when configured)"}
            </p>
            <div className="grid gap-3 sm:grid-cols-3">
              {(["attach_bp", "merge_bp", "strict_bp"] as const).map((key) => (
                <PercentField
                  key={key}
                  label={`${profile === "local" ? "Local" : "Remote"} ${THRESHOLD_LABELS[key].toLowerCase()}`}
                  bp={draft.vector[profile][key]}
                  disabled={locked}
                  error={problems[`vector.${profile}.${key}`]}
                  onChange={(bp) => setThreshold(profile, key, bp)}
                />
              ))}
            </div>
          </div>
        ))}
      </fieldset>
      {editable ? (
        <div className="flex flex-wrap gap-2">
          <Button type="submit" size="sm" variant="primary" icon={<Save className="size-4" />} disabled={!dirty || invalid} loading={busy === "save"}>
            Save clustering settings
          </Button>
          <Button type="button" size="sm" variant="secondary" icon={<Play className="size-4" />} loading={busy === "run"} disabled={busy !== null} onClick={() => void run()}>
            Run clustering now
          </Button>
          {stale ? (
            <Button type="button" size="sm" variant="ghost" icon={<RotateCw className="size-4" />} onClick={onSaved}>
              Reload
            </Button>
          ) : null}
        </div>
      ) : null}
    </form>
  );
}

/**
 * Signals admin **Clustering** section (M8, `docs/contracts/123` §7): how the Signal Hub groups reports
 * into events — the near-duplicate (SimHash) and similar-description (vector) settings, the
 * similarity backends and the last 7 days' counts. The settings are platform-wide: editable by a
 * platform admin, read-only for a tenant admin. Hidden when the engine does not offer them (404).
 */
export function SignalsClusteringSection() {
  const { api, me } = useSession();
  const client = useMemo(() => new ClusteringApi(api), [api]);
  const status = useResource(() => client.status(), "signals-clustering");
  const platform = hasAnyRole(me.user, ["platform-admin"]);
  if (status.errorStatus === 404 && status.data === undefined) return null;
  return (
    <AdminSection
      title="Clustering"
      description="How reports are grouped into one event: syndicated copies (near-duplicates) count once, and reports the place rules cannot locate can join by a similar description. These settings apply to every workspace."
    >
      <ResourceView resource={status} label="clustering settings">
        {(view) => (
          <div className="flex flex-col gap-4">
            <Facts
              items={[
                ["Settings version", `${view.config.version}${view.config.updated_by ? ` · by ${view.config.updated_by}` : ""}`],
                ["Similarity", "Local term vectors (deterministic, no model)"],
                ["pgvector", view.vector_backend.pgvector ? "Available" : "Not installed"],
                ["Remote embeddings", remoteText(view)],
              ]}
            />
            <Table<[string, number]>
              caption={`Grouping in the last ${view.window_days} days`}
              rows={COUNT_LABELS.map(([key, label]) => [label, view.counts[key]] as [string, number])}
              rowKey={([label]) => label}
              empty="Nothing grouped yet."
              columns={[
                { id: "what", header: "What", rowHeader: true, cell: ([label]) => label },
                { id: "n", header: "Reports", align: "end", cell: ([, n]) => new Intl.NumberFormat("en-GB").format(n) },
              ]}
            />
            <ClusteringForm key={view.config.version} view={view} editable={platform && view.editable} onSaved={status.reload} />
          </div>
        )}
      </ResourceView>
    </AdminSection>
  );
}
