"use client";

import { Checkbox, Field, Select, TextInput } from "@keelson/ui";
import { useState } from "react";
import {
  INTEGER_STAGE_OPTIONS,
  MOQ_BASIS_OPTIONS,
  PLANNING_DEFAULTS,
  altSourcing,
  integerStage,
  parseFactoryCodes,
  setAltSourcing,
  setIntegerStage,
  type IntegerStage,
  type MoqBasis,
} from "@/lib/admin/planning";
import type { SpecIssues } from "@/lib/admin/spec";
import type { AgentSpec } from "@/lib/admin/types";
import { AdminSection } from "./common";

/** Integer input that keeps what is typed; reports `NaN` while it is not a number (empty = default). */
function IntField({
  label,
  value,
  fallback,
  onChange,
  error,
  description,
  min,
  max,
}: {
  label: string;
  value: number | null | undefined;
  fallback?: number | undefined;
  onChange: (n: number | undefined) => void;
  error?: string | undefined;
  description?: string | undefined;
  min?: number | undefined;
  max?: number | undefined;
}) {
  const [text, setText] = useState(value == null || Number.isNaN(value) ? "" : String(value));
  return (
    <Field label={label} error={error} description={description}>
      <TextInput
        type="number"
        inputMode="numeric"
        step={1}
        min={min}
        max={max}
        value={text}
        placeholder={fallback !== undefined ? String(fallback) : undefined}
        onChange={(e) => {
          setText(e.target.value);
          onChange(e.target.value.trim() === "" ? undefined : Number(e.target.value));
        }}
      />
    </Field>
  );
}

/**
 * Admin → Agents "Planning solver" (M8, `125-m8-optimizer-cpsat.md` §7): how the optimizer finds
 * whole units and the sourcing terms of the alternate sources it derives from the snapshot.
 * Commercial tuning applies right away; taking a factory off the compliance block list needs a
 * second admin (`guardNote`, shown to tenant admins).
 */
export function PlanningSolverSection({
  draft,
  issues,
  setDraft,
  guardNote,
}: {
  draft: AgentSpec;
  issues: SpecIssues;
  setDraft: (fn: (d: AgentSpec) => void) => void;
  guardNote?: string | undefined;
}) {
  const alt = altSourcing(draft);
  const [blockedText, setBlockedText] = useState((alt.blocked_factories ?? []).join(", "));
  // An emptied number field means "the engine default" (the key is removed: `null` in the patch).
  const setNumber = (key: "moq_units" | "order_multiple_units" | "max_orders_per_source", n: number | undefined) =>
    setDraft((d) => setAltSourcing(d, key, n));
  return (
    <AdminSection
      title="Planning solver"
      description="How the optimizer turns its plan into whole units, and the terms of alternate (near-shore) sources."
    >
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <Field
          label="Whole units"
          description="CP-SAT also decides buys, splits and factory sequences in whole units; it is slower and opt-in."
        >
          <Select
            value={integerStage(draft)}
            options={INTEGER_STAGE_OPTIONS}
            onChange={(v) => setDraft((d) => setIntegerStage(d, v as IntegerStage))}
          />
        </Field>
        <IntField
          label="Minimum order (units)"
          value={alt.moq_units}
          fallback={PLANNING_DEFAULTS.moq_units}
          min={0}
          error={issues["planning.alt_sourcing.moq_units"]}
          onChange={(n) => setNumber("moq_units", n)}
        />
        <Field label="Minimum order counts" description="Per style-colour: each style in an order must reach the minimum.">
          <Select
            value={alt.moq_basis ?? PLANNING_DEFAULTS.moq_basis}
            options={MOQ_BASIS_OPTIONS}
            onChange={(v) => setDraft((d) => setAltSourcing(d, "moq_basis", v as MoqBasis))}
          />
        </Field>
        <IntField
          label="Case pack (units)"
          description="Every order line is a multiple of it; 1 = any quantity."
          value={alt.order_multiple_units}
          fallback={PLANNING_DEFAULTS.order_multiple_units}
          min={1}
          error={issues["planning.alt_sourcing.order_multiple_units"]}
          onChange={(n) => setNumber("order_multiple_units", n)}
        />
        <IntField
          label="Orders per source (at most)"
          description="Distinct arrival weeks per source over the horizon; empty = no limit."
          value={alt.max_orders_per_source}
          min={1}
          max={52}
          error={issues["planning.alt_sourcing.max_orders_per_source"]}
          onChange={(n) => setNumber("max_orders_per_source", n)}
        />
        <div className="flex items-end pb-1">
          <Checkbox
            checked={alt.share_factory_capacity ?? PLANNING_DEFAULTS.share_factory_capacity}
            onCheckedChange={(on) => setDraft((d) => setAltSourcing(d, "share_factory_capacity", on))}
            label="One capacity per factory (shared by its lanes)"
          />
        </div>
        <Field
          className="sm:col-span-2 lg:col-span-3"
          label="Blocked factories (compliance)"
          description="Factory codes the planner never buys from, separated by commas."
          lockedReason={guardNote}
        >
          <TextInput
            value={blockedText}
            placeholder="e.g. F-MA-01"
            onChange={(e) => {
              setBlockedText(e.target.value);
              const codes = parseFactoryCodes(e.target.value);
              setDraft((d) => setAltSourcing(d, "blocked_factories", codes));
            }}
          />
        </Field>
      </div>
    </AdminSection>
  );
}
