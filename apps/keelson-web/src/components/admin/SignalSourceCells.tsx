"use client";

import { Badge, Switch } from "@keelson/ui";
import { ExternalLink } from "lucide-react";
import { SOURCE_KIND_LABEL, provenanceLabel, safeTermsUrl, sourceSwitchNote, type SignalSource } from "@/lib/admin/signals";

/**
 * Cells of the Signals admin Sources table for the M8 catalogue facts (`docs/contracts/124` §4):
 * coverage and description under the name, the source type and provenance, licence + attribution
 * + terms link, and the subscribe switch with its opt-in / not-enabled notes. Hub text is shown as
 * plain text; the only link is an https terms URL ({@link safeTermsUrl}).
 */

export function SourceNameCell({ source: s }: { source: SignalSource }) {
  return (
    <span className="flex flex-col text-left">
      <span className="font-medium">{s.name}</span>
      {s.name !== s.id ? <span className="font-mono text-caption font-normal text-fg-muted">{s.id}</span> : null}
      {s.coverage ? <span className="text-caption font-normal text-fg-muted">{s.coverage}</span> : null}
      {s.description ? <span className="max-w-prose text-caption font-normal text-fg-muted">{s.description}</span> : null}
    </span>
  );
}

export function SourceTypeCell({ source: s }: { source: SignalSource }) {
  const kind = s.kind ? (SOURCE_KIND_LABEL[s.kind] ?? null) : null;
  const provenance = provenanceLabel(s);
  if (!kind && !provenance && !s.opt_in) return <span className="text-fg-muted">—</span>;
  return (
    <span className="flex flex-wrap items-center gap-1">
      {kind ? (
        <Badge size="sm" tone={s.kind === "regulatory" ? "accent" : s.kind === "news" ? "neutral" : "info"}>
          {kind}
        </Badge>
      ) : null}
      {provenance ? (
        <Badge size="sm" variant="outline" tone={s.provenance === "fact" ? "success" : "warning"}>
          {provenance}
        </Badge>
      ) : null}
      {s.opt_in ? (
        <Badge size="sm" variant="outline" tone="neutral">
          Opt-in
        </Badge>
      ) : null}
    </span>
  );
}

export function SourceLicenceCell({ source: s }: { source: SignalSource }) {
  const terms = safeTermsUrl(s.terms_url);
  if (!s.licence && !s.attribution && !terms) return <span className="text-fg-muted">—</span>;
  return (
    <span className="flex flex-col gap-0.5">
      {s.licence ? <span>{s.licence}</span> : null}
      {s.attribution && s.attribution !== s.licence ? <span className="text-caption text-fg-muted">{s.attribution}</span> : null}
      {terms ? (
        <a
          className="inline-flex items-center gap-1 text-caption text-accent underline-offset-2 hover:underline"
          href={terms}
          target="_blank"
          rel="noopener noreferrer"
          aria-label={`Terms of ${s.name} (opens in a new tab)`}
        >
          Terms
          <ExternalLink aria-hidden className="size-3" />
        </a>
      ) : null}
    </span>
  );
}

export function SourceSubscribeCell({ source: s, busy, onChange }: { source: SignalSource; busy: boolean; onChange: (on: boolean) => void }) {
  const note = sourceSwitchNote(s);
  // a source this installation does not poll cannot be switched on; an existing subscription can still end
  return (
    <span className="flex flex-col gap-0.5">
      <Switch
        checked={s.enabled}
        disabled={busy || (s.polled === false && !s.enabled)}
        aria-label={`${s.enabled ? "Unsubscribe from" : "Subscribe to"} ${s.name}`}
        onCheckedChange={(on) => onChange(on)}
      />
      {note ? <span className="max-w-[16rem] text-caption text-fg-muted">{note}</span> : null}
    </span>
  );
}
