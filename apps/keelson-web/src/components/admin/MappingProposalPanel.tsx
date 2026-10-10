"use client";

import { confidencePercent, confidenceTone } from "@keelson/contracts";
import { Badge, Button, Checkbox, Field, Select, cn, toast } from "@keelson/ui";
import { opLabel } from "@keelson/viz";
import { FileCode2, Sparkles, Trash2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { adminError, when } from "@/lib/admin/format";
import {
  actionDraftText,
  defaultSelection,
  noteLabel,
  originLabel,
  proposalHeadline,
  readDraftText,
  sourceLabel,
  type DraftSelection,
  type Proposal,
  type ProposedEntity,
  type ProposedOp,
  type ProposerStatus,
} from "@/lib/admin/proposals";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { linkClass } from "./ConnectorsPage";
import { InlineError } from "./common";

const TONE: Record<"high" | "medium" | "low", "success" | "warning" | "danger"> = { high: "success", medium: "warning", low: "danger" };
const TONE_WORD: Record<"high" | "medium" | "low", string> = { high: "high", medium: "medium", low: "low" };

/** Confidence as text (percentage + word) with a tone — never colour alone. */
export function ConfidenceBadge({ bp }: { bp: number }) {
  const tone = confidenceTone(bp);
  return (
    <Badge size="sm" tone={TONE[tone]} title={`Confidence ${confidencePercent(bp)} (${TONE_WORD[tone]})`}>
      <span data-confidence={tone}>
        {confidencePercent(bp)} <span className="sr-only">confidence, </span>
        {TONE_WORD[tone]}
      </span>
    </Badge>
  );
}

export function OriginBadge({ origin }: { origin: string }) {
  return (
    <Badge size="sm" variant="outline" tone={origin === "heuristic" ? "neutral" : "info"}>
      {originLabel(origin)}
    </Badge>
  );
}

function aiReason(status: ProposerStatus | undefined): string | null {
  if (!status) return null;
  if (status.llm.available) return null;
  return status.llm.message ?? "The AI step is not available here: rule-based suggestions only.";
}

function EntityCard({ entity, index, included, onToggle }: { entity: ProposedEntity; index: number; included: boolean; onToggle: (on: boolean) => void }) {
  const kind = entity.fields.find((f) => f.column === "kind")?.expression;
  const missing = entity.missing_required ?? [];
  const title = `${entity.entity}${kind ? ` ${kind.replace(/'/g, "")}` : ""}`;
  return (
    <div className={cn("flex flex-col gap-2 rounded-sm border p-2", missing.length ? "border-danger/40" : "border-border")} data-proposed-entity={index}>
      <div className="flex flex-wrap items-center gap-2">
        <Checkbox checked={included} onCheckedChange={onToggle} label={<span className="font-mono">{title}</span>} />
        <Badge size="sm" tone="neutral" title="Where the rows come from">
          <span className="font-mono">{sourceLabel(entity.source)}</span>
        </Badge>
        {entity.group_by ? <span className="text-caption text-fg-muted">one row per {entity.group_by.join(", ")}</span> : null}
        <span className="ml-auto" />
        <ConfidenceBadge bp={entity.confidence_bp} />
        <OriginBadge origin={entity.origin} />
      </div>
      {entity.rationale ? <p className="text-caption text-fg-muted">{entity.rationale}</p> : null}
      {missing.length ? (
        <p className="text-caption text-danger" role="note">
          Required {missing.length === 1 ? "column" : "columns"} without a proposal: <span className="font-mono">{missing.join(", ")}</span>. Opened in the editor as
          null, so the check points at what to fill in.
        </p>
      ) : null}
      <div className="max-w-full overflow-x-auto">
        <table className="min-w-full text-caption">
          <caption className="sr-only">Proposed columns of {title}</caption>
          <thead>
            <tr className="text-left text-fg-muted">
              <th scope="col" className="px-2 py-1 font-medium">Column</th>
              <th scope="col" className="px-2 py-1 font-medium">Expression</th>
              <th scope="col" className="px-2 py-1 font-medium">Confidence</th>
              <th scope="col" className="px-2 py-1 font-medium">Why</th>
              <th scope="col" className="px-2 py-1 font-medium">Alternatives</th>
            </tr>
          </thead>
          <tbody>
            {entity.fields.map((f) => (
              <tr key={f.column} className="border-t border-border align-top">
                <td className="px-2 py-1 font-mono">{f.column}</td>
                <td className="px-2 py-1 font-mono break-all">{f.expression}</td>
                <td className="px-2 py-1">
                  <span className="flex flex-wrap gap-1">
                    <ConfidenceBadge bp={f.confidence_bp} />
                    <OriginBadge origin={f.origin} />
                  </span>
                </td>
                <td className="px-2 py-1 text-fg-muted" data-rationale="">
                  {f.rationale}
                </td>
                <td className="px-2 py-1">
                  {(f.alternatives ?? []).length ? (
                    <ul className="flex flex-col gap-0.5">
                      {(f.alternatives ?? []).map((a) => (
                        <li key={a.expression} className="font-mono break-all">
                          {a.expression} <span className="font-sans text-fg-muted">({confidencePercent(a.confidence_bp)}, {originLabel(a.origin)})</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-fg-subtle">—</span>
                  )}
                </td>
              </tr>
            ))}
            {missing.map((c) => (
              <tr key={c} className="border-t border-border" data-missing-column={c}>
                <td className="px-2 py-1 font-mono text-danger">{c}</td>
                <td className="px-2 py-1" colSpan={4}>
                  <Badge size="sm" tone="danger">
                    Required · fill in
                  </Badge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function OpCard({ op, included, onToggle }: { op: ProposedOp; included: boolean; onToggle: (on: boolean) => void }) {
  const m = op.mapping;
  return (
    <div className="flex flex-col gap-1 rounded-sm border border-border p-2" data-proposed-op={m.op}>
      <div className="flex flex-wrap items-center gap-2">
        <Checkbox checked={included} onCheckedChange={onToggle} label={<span>{opLabel(m.op)}</span>} />
        <span className="text-caption">
          calls <code className="font-mono">{m.tool}</code>
          {m.readback ? (
            <>
              , read back with <code className="font-mono">{m.readback.tool}</code>
            </>
          ) : null}
        </span>
        <Badge size="sm" tone={m.idempotency === "enforced" ? "success" : m.idempotency === "none" ? "warning" : "info"}>
          Idempotency: {m.idempotency.replace("_", " ")}
        </Badge>
        {op.generic ? (
          <Badge size="sm" tone="warning" variant="outline">
            Generic update tool
          </Badge>
        ) : null}
        <span className="ml-auto" />
        <ConfidenceBadge bp={op.confidence_bp} />
        <OriginBadge origin={op.origin} />
      </div>
      {op.rationale ? (
        <p className="text-caption text-fg-muted" data-rationale="">
          {op.rationale}
        </p>
      ) : null}
    </div>
  );
}

/** The result of one proposal: headline, notes, entity / op cards with include boxes, open-in-editor. */
export function ProposalView({
  proposal,
  onOpenDraft,
  onDiscarded,
}: {
  proposal: Proposal;
  onOpenDraft: (kind: "read" | "action", text: string, proposalId: string) => void;
  onDiscarded: (p: Proposal) => void;
}) {
  const admin = useAdminApi();
  const [selection, setSelection] = useState<DraftSelection>(() => defaultSelection(proposal));
  const [busy, setBusy] = useState(false);
  const notes = proposal.notes ?? [];
  const warnings = notes.filter((n) => n.severity === "warning");
  const entities = proposal.read?.entities ?? [];
  const ops = proposal.action?.ops ?? [];
  const readText = readDraftText(proposal, selection);
  const actionText = actionDraftText(proposal, selection);
  const decided = proposal.status !== "ready";

  const toggleEntity = (i: number, on: boolean) =>
    setSelection((s) => {
      const next = new Set(s.entities);
      if (on) next.add(i);
      else next.delete(i);
      return { ...s, entities: next };
    });
  const toggleOp = (op: string, on: boolean) =>
    setSelection((s) => {
      const next = new Set(s.ops);
      if (on) next.add(op);
      else next.delete(op);
      return { ...s, ops: next };
    });

  const discard = async () => {
    setBusy(true);
    try {
      onDiscarded(await admin.discardProposal(proposal.connector, proposal.id));
      toast("Proposal discarded");
    } catch (err) {
      toast.error("Couldn't discard the proposal", { description: adminError(err) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-3" data-proposal={proposal.id}>
      <div role="status" className="flex flex-col gap-1 rounded-sm border border-border bg-surface-2 px-3 py-2 text-ui">
        <p className="flex flex-wrap items-center gap-2">
          <span className="font-medium" data-proposal-headline="">
            {proposalHeadline(proposal)}
          </span>
          <Badge size="sm" tone={proposal.status === "ready" ? "info" : proposal.status === "accepted" ? "success" : "neutral"}>
            {proposal.status === "ready" ? "Draft, not active" : proposal.status === "accepted" ? "Saved from this proposal" : "Discarded"}
          </Badge>
          <span className="text-caption text-fg-muted">
            {when(proposal.created_at)}
            {proposal.created_by ? ` by ${proposal.created_by}` : ""} · {proposal.profiles.length} {proposal.profiles.length === 1 ? "read" : "reads"} sampled
          </span>
        </p>
        <p className="text-caption text-fg-muted">Nothing is active until you review a draft in the editor, preview it and save it.</p>
      </div>
      {notes.length ? (
        <details open={warnings.length > 0}>
          <summary className="cursor-pointer text-ui">
            {notes.length} {notes.length === 1 ? "note" : "notes"}
            {warnings.length ? ` (${warnings.length} to check)` : ""}
          </summary>
          <ul className="mt-1 flex flex-col gap-1 text-caption" aria-label="Proposal notes">
            {notes.map((n, i) => (
              <li key={`${n.code}-${n.where ?? ""}-${i}`} className="flex flex-wrap items-baseline gap-2">
                <Badge size="sm" tone={n.severity === "warning" ? "warning" : "neutral"}>
                  {noteLabel(n.code)}
                </Badge>
                {n.where ? <span className="font-mono text-fg-muted">{n.where}</span> : null}
                <span>{n.message}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {entities.length ? (
        <section aria-label="Proposed read mapping" className="flex flex-col gap-2">
          <h3 className="text-ui font-semibold">Read mapping: {entities.length} entity {entities.length === 1 ? "mapping" : "mappings"}</h3>
          {entities.map((e, i) => (
            <EntityCard key={`${e.entity}-${e.source_ref}-${i}`} entity={e} index={i} included={selection.entities.has(i)} onToggle={(on) => toggleEntity(i, on)} />
          ))}
        </section>
      ) : proposal.read ? (
        <p className="text-ui text-fg-muted">No read mapping could be proposed{proposal.profiles.length ? "" : " without samples"}.</p>
      ) : null}
      {ops.length ? (
        <section aria-label="Proposed action mapping" className="flex flex-col gap-2">
          <h3 className="text-ui font-semibold">Action mapping: {ops.length} {ops.length === 1 ? "change" : "changes"}</h3>
          {ops.map((o) => (
            <OpCard key={o.mapping.op} op={o} included={selection.ops.has(o.mapping.op)} onToggle={(on) => toggleOp(o.mapping.op, on)} />
          ))}
          <p className="text-caption text-fg-muted">An action mapping from a proposal takes effect only after a second admin approves it.</p>
        </section>
      ) : null}
      <div className="flex flex-wrap justify-end gap-2">
        {!decided ? (
          <Button size="sm" variant="ghost" icon={<Trash2 className="size-4" />} loading={busy} onClick={() => void discard()}>
            Discard proposal
          </Button>
        ) : null}
        {proposal.read ? (
          <Button size="sm" variant="secondary" icon={<FileCode2 className="size-4" />} disabled={readText === null || proposal.status === "discarded"} onClick={() => readText && onOpenDraft("read", readText, proposal.id)}>
            Open read draft in editor
          </Button>
        ) : null}
        {proposal.action ? (
          <Button size="sm" variant="secondary" icon={<FileCode2 className="size-4" />} disabled={actionText === null || proposal.status === "discarded"} onClick={() => actionText && onOpenDraft("action", actionText, proposal.id)}>
            Open action draft in editor
          </Button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * **Propose mappings** (`docs/contracts/121` §3): samples the connector's approved read tools
 * (shape statistics only, never free text) and proposes read / action drafts — rule-based, refined
 * by the AI when it is available. The result is a reviewable draft; nothing is activated and no
 * write tool is called.
 */
export function MappingProposalPanel({
  slug,
  connectorActive,
  onOpenDraft,
}: {
  slug: string;
  connectorActive: boolean;
  onOpenDraft: (kind: "read" | "action", text: string, proposalId: string) => void;
}) {
  const admin = useAdminApi();
  const status = useResource(() => admin.proposerStatus(), "mapping-proposer");
  const history = useResource(() => admin.mappingProposals(slug), `mapping-proposals:${slug}`);
  const [read, setRead] = useState(true);
  const [action, setAction] = useState(true);
  // The choice survives the connector status loading: sampling happens only while it is active.
  const [sample, setSample] = useState(true);
  const [ai, setAi] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const aiBlocked = aiReason(status.data);
  const disabled = status.data ? !status.data.enabled : false;
  const kinds = [...(read ? (["read"] as const) : []), ...(action ? (["action"] as const) : [])];

  const propose = async () => {
    setBusy(true);
    setError(null);
    try {
      const p = await admin.proposeMappings(slug, { kinds: [...kinds], proposer: ai && !aiBlocked ? "auto" : "heuristic", sample: sample && connectorActive });
      setProposal(p);
      history.reload();
    } catch (err) {
      setError(adminError(err));
    } finally {
      setBusy(false);
    }
  };

  const load = async (id: string) => {
    if (!id) return;
    setError(null);
    try {
      setProposal(await admin.mappingProposal(slug, id));
    } catch (err) {
      setError(adminError(err));
    }
  };

  return (
    <div className="flex flex-col gap-3" data-proposal-panel="">
      {disabled ? <p className="text-ui text-fg-muted">Mapping proposals are switched off in this installation.</p> : null}
      <div className="grid gap-3 md:grid-cols-3">
        <fieldset className="flex flex-col gap-1">
          <legend className="text-caption font-medium text-fg-muted">Propose</legend>
          <Checkbox checked={read} onCheckedChange={setRead} label="Read mapping" description="Which read tool fills each scn entity." />
          <Checkbox checked={action} onCheckedChange={setAction} label="Action mapping" description="Which write tool applies each change (needs a second admin to save)." />
        </fieldset>
        <Checkbox
          checked={sample && connectorActive}
          onCheckedChange={setSample}
          disabled={!connectorActive}
          disabledReason={connectorActive ? undefined : "Activate the connector to sample it; until then proposals use the tool catalogue only."}
          label="Sample the read tools"
          description="One small page per read, reduced to shapes (codes, numbers, dates). Free text is never kept or sent to a model."
        />
        <Checkbox
          checked={ai && !aiBlocked}
          onCheckedChange={setAi}
          disabled={Boolean(aiBlocked)}
          disabledReason={aiBlocked ?? undefined}
          label="Refine with AI"
          description={status.data?.llm.available ? `Model ${status.data.alias ?? "fast-default"}; falls back to the rules if it fails.${status.data.llm.message ? ` ${status.data.llm.message}` : ""}` : "Rule-based suggestions work without a model."}
        />
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <Button size="sm" variant="primary" icon={<Sparkles className="size-4" />} loading={busy} disabled={disabled || kinds.length === 0} onClick={() => void propose()}>
          Propose mappings
        </Button>
        {history.data && history.data.length ? (
          <Field label="Earlier proposals">
            <Select
              aria-label="Earlier proposals"
              value={proposal?.id ?? ""}
              onChange={(v) => void load(v)}
              options={[
                { value: "", label: "Choose…" },
                ...history.data.map((h) => ({
                  value: h.id,
                  label: `${when(h.created_at)} · ${h.proposer === "llm+heuristic" ? "rules + AI" : "rules"} · ${h.complete_entities} entities · ${h.ops} changes · ${h.status}`,
                })),
              ]}
            />
          </Field>
        ) : null}
      </div>
      {busy ? <p className="text-caption text-fg-muted" role="status">Sampling and proposing… this can take up to two minutes.</p> : null}
      {error ? <InlineError message={error} /> : null}
      {proposal ? (
        <ProposalView
          key={`${proposal.id}:${proposal.status}`}
          proposal={proposal}
          onOpenDraft={onOpenDraft}
          onDiscarded={(p) => {
            setProposal(p);
            history.reload();
          }}
        />
      ) : null}
    </div>
  );
}

/** `/admin/connectors/{slug}/mappings?propose=1` — where the call to action leads (the panel opens). */
export function proposeHref(slug: string): string {
  return `/admin/connectors/${encodeURIComponent(slug)}/mappings?propose=1`;
}

/**
 * Call to action for a tier-A connector that has no read mapping yet (connector page; the wizard
 * links to the same place after activation): proposals draft one from the connector's own tools.
 */
export function ProposeMappingsCallout({ slug }: { slug: string }) {
  const admin = useAdminApi();
  const read = useResource(() => admin.mapping(slug, "read"), `mapping:${slug}:read`);
  if (read.data !== null) return null;
  return (
    <div role="note" className="flex flex-wrap items-center gap-3 rounded-sm border border-info/40 bg-info-soft px-3 py-2 text-ui" data-propose-callout="">
      <Sparkles aria-hidden="true" className="size-4 shrink-0 text-info" />
      <span className="min-w-0 flex-1">
        This connector has no read mapping yet, so Keelson cannot crawl it. Let Keelson propose one from its read tools (and an action mapping for its write
        tools) — you review every field before anything is saved.
      </span>
      <Link href={proposeHref(slug)} className={cn(linkClass, "font-medium")}>
        Propose mappings
      </Link>
    </div>
  );
}
