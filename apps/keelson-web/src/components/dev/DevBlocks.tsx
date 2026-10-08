"use client";

import { Chip, toast } from "@keelson/ui";
import { BlockRenderer, type BlockAction, type BlockDensity } from "@keelson/viz";
import { blockFixtures, fixtureDataLoader } from "@keelson/viz/fixtures";
import { useState } from "react";
import "@/lib/blocks/canvasBlocks";

/** Fixture payloads behind `data_ref`, served like `/api/blocks/{id}/data` (with a short delay). */
const loadFixtureData = fixtureDataLoader(300);

function describeAction(a: BlockAction): string {
  switch (a.type) {
    case "interrupt_decision":
      return `Decision "${a.decision}" on ${a.interruptId}`;
    case "case_action":
      return `Case action "${a.action}" on ${a.optionId} (plan v${a.planVersion})`;
    case "connect":
      return `Connect ${a.connector}, back to ${a.returnTo}`;
    case "open_canvas":
      return `Open canvas on ${a.blockId}`;
    case "rollback":
      return `Undo change set ${a.changeSetId}`;
    case "checklist_step":
      return `Mark step ${a.seq} of ${a.checklistId} ${a.status}`;
  }
}

/** DEMO-only gallery of every render block fixture (`@keelson/viz/fixtures`). Actions are logged, never sent. */
export function DevBlocks() {
  const [density, setDensity] = useState<BlockDensity>("inline");
  const [busy, setBusy] = useState(false);

  const onAction = async (a: BlockAction) => {
    await new Promise((resolve) => setTimeout(resolve, 400));
    toast(describeAction(a), { description: "Not sent: this page only previews blocks." });
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto flex w-full max-w-chat flex-col gap-6 px-4 py-6">
        <div className="flex flex-col gap-3">
          <h1 className="text-heading font-semibold text-fg">Render blocks</h1>
          <p className="text-ui text-fg-muted">
            Every block fixture (M1 cards and the M3 canvas visuals), rendered by the same registry as the case thread.
          </p>
          <div className="flex flex-wrap gap-2" role="group" aria-label="Preview options">
            <Chip selected={density === "inline"} onClick={() => setDensity("inline")}>
              Inline
            </Chip>
            <Chip selected={density === "canvas"} onClick={() => setDensity("canvas")}>
              Canvas
            </Chip>
            <Chip selected={busy} onClick={() => setBusy((b) => !b)}>
              Busy
            </Chip>
          </div>
        </div>
        {blockFixtures.map((f) => (
          <section key={f.name} className="flex flex-col gap-2" aria-labelledby={`fx-${f.name}`}>
            <h2 id={`fx-${f.name}`} className="text-ui font-medium text-fg">
              <code className="font-mono">{f.name}</code>
              <span className="ml-2 font-normal text-fg-muted">{f.description}</span>
            </h2>
            <BlockRenderer envelope={f.envelope} onAction={onAction} busy={busy} density={density} loadData={loadFixtureData} />
          </section>
        ))}
      </div>
    </div>
  );
}
