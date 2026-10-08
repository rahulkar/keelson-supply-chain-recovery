"use client";

import type { EvidenceItem, EvidencePackProps } from "@keelson/contracts";
import { Chip, ProvenanceChip, type Prov } from "@keelson/ui";
import { FileSearch } from "lucide-react";
import { useMemo, useState } from "react";
import { formatDateTime, formatValue } from "../format";
import type { BlockComponentProps } from "../types";
import { BlockFrame } from "./BlockFrame";

/** `evidence_pack@1` (canvas) — the full provenance ledger; every item shows its chip, facts included. */
export function EvidencePack({ props, envelope }: BlockComponentProps<EvidencePackProps>) {
  const [filter, setFilter] = useState<Prov | "all">("all");
  const counts = useMemo(() => {
    const m = new Map<Prov, number>();
    for (const i of props.items) m.set(i.prov, (m.get(i.prov) ?? 0) + 1);
    return m;
  }, [props.items]);
  const items = filter === "all" ? props.items : props.items.filter((i) => i.prov === filter);
  const bySource = useMemo(() => {
    const groups = new Map<string, EvidenceItem[]>();
    for (const i of items) groups.set(i.source, [...(groups.get(i.source) ?? []), i]);
    return [...groups.entries()];
  }, [items]);

  return (
    <BlockFrame envelope={envelope} defaultTitle="Evidence" icon={<FileSearch />} showProvenance={false}>
      {props.items.length === 0 ? (
        <p className="text-ui text-fg-muted">No evidence recorded yet.</p>
      ) : (
        <>
          {counts.size > 1 ? (
            <div role="group" aria-label="Filter evidence" className="flex flex-wrap gap-1.5">
              <Chip selected={filter === "all"} onClick={() => setFilter("all")}>
                All ({formatValue(props.items.length, "count")})
              </Chip>
              {[...counts.entries()].map(([prov, n]) => (
                <Chip key={prov} selected={filter === prov} onClick={() => setFilter(prov)}>
                  <span className="capitalize">{prov}</span> ({formatValue(n, "count")})
                </Chip>
              ))}
            </div>
          ) : null}
          <div className="flex flex-col gap-4">
            {bySource.map(([source, group]) => (
              <section key={source} aria-label={`From ${source}`}>
                <h4 className="mb-1.5 text-caption font-medium tracking-wide text-fg-subtle uppercase">
                  {source} · {formatValue(group.length, "count")}
                </h4>
                <ul className="flex flex-col divide-y divide-border rounded-md border border-border">
                  {group.map((item) => (
                    <li key={item.id} id={item.id} className="flex flex-wrap items-start gap-x-3 gap-y-1 px-3 py-2.5">
                      <span className="min-w-0 flex-1 text-ui text-fg">{item.label}</span>
                      <ProvenanceChip prov={item.prov} />
                      <span className="w-full text-caption text-fg-subtle">
                        Retrieved {formatDateTime(item.retrieved_at)} · <span className="font-mono">{item.id}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              </section>
            ))}
          </div>
        </>
      )}
    </BlockFrame>
  );
}
