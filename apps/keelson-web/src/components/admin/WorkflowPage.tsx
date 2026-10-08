"use client";

import { AGENT_DISPLAY_NAMES, STAGE_IDS } from "@keelson/contracts";
import { Badge, Spinner } from "@keelson/ui";
import { WORKFLOW_KIND_LABELS, buildWorkflowModel, type WorkflowInput, type WorkflowNode } from "@keelson/viz";
import { useMemo } from "react";
import { customSpecialistsOf } from "@/lib/admin/specialists";
import type { AgentSpec } from "@/lib/admin/types";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AgentsSubnav } from "./AgentsSubnav";
import { AdminPageHeader, AdminSection, ResourceView } from "./common";
import { LazyWorkflowGraph } from "./lazy";

const STAGE_LABELS: Record<string, string> = {
  understanding: "Understanding",
  checking_systems: "Checking systems",
  measuring_impact: "Measuring impact",
  building_options: "Building options",
  ready_for_review: "Ready for review",
  preparing_changes: "Preparing changes",
  awaiting_approval: "Awaiting approval",
  applying: "Applying",
  verified: "Verified",
};

/** The tenant's specialists (built-in + custom) and stage toggles as the graph's input. */
export function workflowInputOf(spec: AgentSpec): WorkflowInput {
  const builtIn = spec.specialists.map((s) => ({
    slug: s.slug,
    label: s.display_name || (AGENT_DISPLAY_NAMES as Record<string, string>)[s.slug] || s.slug.replace(/_/g, " "),
    custom: false,
    enabled: (s as Record<string, unknown>)["enabled"] !== false,
    connectors: [s.connector ?? spec.connector ?? "merxa"],
    tools: s.tools.length,
  }));
  const custom = customSpecialistsOf(spec).map((s) => ({
    slug: s.slug,
    label: s.display_name,
    custom: true,
    enabled: s.enabled,
    connectors: [...new Set((s.tools ?? []).map((t) => t.connector))],
    tools: (s.tools ?? []).length,
    knowledgeBases: (s.kb_ids ?? []).length,
  }));
  const rawStages = (spec as Record<string, unknown>)["stages"];
  const stages =
    rawStages && typeof rawStages === "object" && !Array.isArray(rawStages)
      ? Object.fromEntries(Object.entries(rawStages as Record<string, unknown>).filter(([, v]) => typeof v === "boolean")) as Record<string, boolean>
      : null;
  return { specialists: [...builtIn, ...custom], stages };
}

/** The same graph as an ordered list per stage (always present: the accessible view). */
export function WorkflowOutline({ nodes }: { nodes: readonly WorkflowNode[] }) {
  const byStage = new Map<string, WorkflowNode[]>();
  for (const n of nodes) {
    if (!n.stage) continue;
    byStage.set(n.stage, [...(byStage.get(n.stage) ?? []), n]);
  }
  return (
    <ol aria-label="Workflow steps by stage" className="flex flex-col gap-3">
      {STAGE_IDS.filter((s) => byStage.has(s)).map((stage) => (
        <li key={stage}>
          <p className="text-caption font-medium tracking-wide text-fg-subtle uppercase">{STAGE_LABELS[stage] ?? stage}</p>
          <ul className="mt-1 flex flex-col gap-1">
            {byStage.get(stage)!.map((n) => (
              <li key={n.id} className="flex flex-wrap items-center gap-2 text-ui" data-workflow-node={n.id}>
                <span className={n.enabled ? "font-medium text-fg" : "text-fg-muted line-through"}>{n.label}</span>
                <Badge size="sm" tone={n.kind === "specialist" ? "info" : n.kind === "llm" ? "accent" : n.kind === "gate" ? "warning" : "neutral"}>
                  {n.custom ? "Custom specialist" : WORKFLOW_KIND_LABELS[n.kind]}
                </Badge>
                {!n.enabled ? (
                  <Badge size="sm" tone="neutral">
                    {n.kind === "specialist" ? "Disabled" : "Off"}
                  </Badge>
                ) : null}
                {n.detail ? <span className="text-caption text-fg-muted">{n.detail}</span> : null}
              </li>
            ))}
          </ul>
        </li>
      ))}
    </ol>
  );
}

/**
 * **Agents → Workflow** (`docs/contracts/110` §3): the engine's workflow as a read-only graph
 * (React Flow + elkjs) with this workspace's enabled specialists and stage toggles. Nothing here
 * can be edited: the topology is code; specialists are managed on their own page.
 */
export function WorkflowPage() {
  const admin = useAdminApi();
  const spec = useResource(() => admin.getSpec(), "spec");
  const input = useMemo(() => (spec.data ? workflowInputOf(spec.data.spec) : null), [spec.data]);
  const model = useMemo(() => (input ? buildWorkflowModel(input) : null), [input]);
  return (
    <>
      <AdminPageHeader title="Agents" description="How a case flows through the agents and deterministic steps (read-only)." />
      <AgentsSubnav />
      <AdminSection
        title="Workflow"
        description={spec.data ? `Agent settings ${spec.data.version}. Dashed boxes are switched off; specialists fan out from Investigate.` : undefined}
      >
        <ResourceView resource={spec} label="the agent settings">
          {() =>
            input && model ? (
              <div className="flex flex-col gap-4">
                <LazyWorkflowGraph input={input} height={640} />
                <details open>
                  <summary className="cursor-pointer text-ui font-medium text-fg">Steps as a list</summary>
                  <div className="mt-2">
                    <WorkflowOutline nodes={model.nodes} />
                  </div>
                </details>
              </div>
            ) : (
              <Spinner size={16} />
            )
          }
        </ResourceView>
      </AdminSection>
    </>
  );
}
