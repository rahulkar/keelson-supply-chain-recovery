"use client";

import type { BlockEnvelope } from "@keelson/contracts";
import { Card, CardHeader, ProvenanceChip, type Prov } from "@keelson/ui";
import { useId, type ReactNode } from "react";
import { formatDate } from "../format";

const PROVS: readonly string[] = ["fact", "inference", "assumption", "simulated", "decision", "committed"];

export function envelopeProv(envelope: BlockEnvelope): Prov | null {
  const kind = envelope.provenance?.kind;
  return kind && PROVS.includes(kind) ? (kind as Prov) : null;
}

export type BlockFrameProps = {
  envelope: BlockEnvelope;
  /** Fallback heading when the envelope has no title. */
  defaultTitle: string;
  eyebrow?: ReactNode | undefined;
  icon?: ReactNode | undefined;
  /** Extra header badges/actions (right side). */
  actions?: ReactNode | undefined;
  tone?: "default" | "accent" | "warning" | "danger" | undefined;
  /** Show the block-level provenance chip (non-fact only) and as-of date. */
  showProvenance?: boolean | undefined;
  /** Show "As of <date>" (defaults to `showProvenance`). */
  showAsOf?: boolean | undefined;
  children: ReactNode;
  className?: string | undefined;
};

/** Card + header shared by all blocks: titled region, optional provenance chip and "as of". */
export function BlockFrame({
  envelope,
  defaultTitle,
  eyebrow,
  icon,
  actions,
  tone = "default",
  showProvenance = true,
  showAsOf,
  children,
  className,
}: BlockFrameProps) {
  const titleId = useId();
  const prov = envelopeProv(envelope);
  const asOf = envelope.provenance?.as_of;
  const provChip = showProvenance && prov && prov !== "fact" ? <ProvenanceChip prov={prov} /> : null;
  return (
    <Card labelledBy={titleId} tone={tone} className={className}>
      <div data-block-kind={envelope.kind} data-block-id={envelope.id} className="flex flex-col gap-4">
        <CardHeader
          title={envelope.title || defaultTitle}
          titleId={titleId}
          eyebrow={eyebrow}
          icon={icon}
          description={(showAsOf ?? showProvenance) && asOf ? `As of ${formatDate(asOf)}` : undefined}
          actions={
            provChip || actions ? (
              <>
                {provChip}
                {actions}
              </>
            ) : undefined
          }
        />
        {children}
      </div>
    </Card>
  );
}
