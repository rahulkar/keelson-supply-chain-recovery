"use client";

import type { OptionAction, OptionActionType, RecommendedOptionProps } from "@keelson/contracts";
import { Badge, Button } from "@keelson/ui";
import { ArrowUpRight, CircleCheck, ListChecks, Sparkles, Zap } from "lucide-react";
import { useId, type ReactNode } from "react";
import { useActionRunner } from "../lib/useActionRunner";
import type { BlockComponentProps } from "../types";
import { AdvisoryBadge, AdvisoryNote } from "./Advisory";
import { BlockFrame } from "./BlockFrame";
import { KpiTiles } from "./KpiStrip";

/** Why Apply is off on a what-if case (the engine's wording, used when it sent none). */
export const WHAT_IF_APPLY_REASON = "What-if case: promote it to an actual case first";
/** Why Apply is off while values still need confirming (plan §3: no commit on unconfirmed inference). */
export const UNCONFIRMED_APPLY_REASON = "Confirm the values above before applying";

const ACTION_ICONS: Record<OptionActionType, ReactNode> = {
  apply: <Zap className="size-4" />,
  guide: <ListChecks className="size-4" />,
  promote: <ArrowUpRight className="size-4" />,
  confirm_assumptions: <CircleCheck className="size-4" />,
};

/**
 * The buttons as shown: the engine's actions, made safe for the card's state. On a what-if card
 * Apply is always off (with the reason) and **Promote to actual** is offered; while values are
 * unconfirmed Apply is off and **Confirm** is offered. The server enforces the same rules — this
 * only keeps the card from offering what would be refused.
 */
export function optionActions(props: Pick<RecommendedOptionProps, "actions" | "what_if" | "unconfirmed">): OptionAction[] {
  const out: OptionAction[] = props.actions.map((a) => {
    // Guide (M5) acts on the plan too: its checklist has people make the changes by hand, so the
    // same refusals apply (policy rule 1: what-if / unconfirmed → refuse).
    if ((a.type !== "apply" && a.type !== "guide") || !a.enabled) return a;
    if (props.what_if) return { ...a, enabled: false, disabled_reason: a.disabled_reason ?? WHAT_IF_APPLY_REASON };
    if (props.unconfirmed.length > 0) return { ...a, enabled: false, disabled_reason: a.disabled_reason ?? UNCONFIRMED_APPLY_REASON };
    return a;
  });
  if (props.what_if && !out.some((a) => a.type === "promote")) {
    out.push({ type: "promote", label: "Promote to actual", enabled: true, disabled_reason: null });
  }
  return out;
}

/**
 * How Apply splits the option's changes (M5 Guide mode, `docs/contracts/95` §2):
 * "Apply 3 changes for me · Guide me through 2" when some lines can be applied and others must
 * be made by hand; "Guide me through all 5" when none can be applied; `null` when everything is
 * applied (or the engine sent no mix).
 */
export function mixSentence(mix: RecommendedOptionProps["remediation_mix"]): string | null {
  if (!mix) return null;
  const { apply_lines: apply, guide_lines: guide } = mix;
  if (guide <= 0) return null;
  if (apply <= 0) return `Guide me through all ${guide === 1 ? "1 change" : `${guide} changes`}`;
  return `Apply ${apply === 1 ? "1 change" : `${apply} changes`} for me · Guide me through ${guide}`;
}

/** The Confirm button of the "Confirm before applying" box (the engine's, or a default one). */
export function confirmAction(props: Pick<RecommendedOptionProps, "actions" | "unconfirmed">): OptionAction | null {
  if (props.unconfirmed.length === 0) return null;
  return props.actions.find((a) => a.type === "confirm_assumptions") ?? { type: "confirm_assumptions", label: "Confirm", enabled: true, disabled_reason: null };
}

/** `recommended_option@1` — the one inline option card with deterministic action buttons. */
export function RecommendedOption({ props, envelope, onAction, busy }: BlockComponentProps<RecommendedOptionProps>) {
  const { pending, run } = useActionRunner(onAction);
  const reasonBase = useId();
  const confirmReasonId = useId();
  const { option } = props;
  const alternatives = props.alternatives.slice(0, 2);
  const actions = optionActions(props).filter((a) => a.type !== "confirm_assumptions");
  const confirm = confirmAction(props);
  const mix = mixSentence(props.remediation_mix);
  const mixId = useId();

  const raise = (a: OptionAction) =>
    void run(a.type, {
      type: "case_action",
      action: a.type,
      optionId: option.id,
      planVersion: props.plan_version,
      ...(a.type === "confirm_assumptions" ? { assumptionKeys: props.unconfirmed.map((u) => u.key) } : {}),
    });

  return (
    <BlockFrame
      envelope={{ ...envelope, title: option.title }}
      defaultTitle={option.title}
      eyebrow={envelope.title || "Recommended option"}
      icon={<Sparkles />}
      tone="accent"
      actions={
        <span className="flex flex-wrap items-center gap-1.5">
          {props.what_if ? <Badge tone="info">What-if</Badge> : null}
          <AdvisoryBadge advisory={props.advisory} />
          <Badge tone="neutral">Plan v{props.plan_version}</Badge>
        </span>
      }
    >
      <AdvisoryNote advisory={props.advisory} />
      {option.summary_bullets.length > 0 ? (
        <ul className="flex list-disc flex-col gap-1 pl-5 text-body text-fg marker:text-accent">
          {option.summary_bullets.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      ) : null}

      {option.kpis.length > 0 ? <KpiTiles items={option.kpis} size="sm" /> : null}

      {props.unconfirmed.length > 0 && confirm ? (
        <section
          aria-label="Confirm before applying"
          className="flex flex-col gap-2 rounded-md border border-warning/40 bg-warning-soft px-3 py-2.5"
          data-unconfirmed=""
        >
          <h4 className="text-ui font-semibold text-fg">Confirm before applying</h4>
          <ul className="flex flex-col gap-1">
            {props.unconfirmed.map((u) => (
              <li key={u.key} className="flex flex-wrap items-baseline gap-x-2 text-ui">
                <span className="text-fg-muted">{u.label}</span>
                <span className="font-medium text-fg">{u.value_text}</span>
                <span className="text-caption text-fg-muted">· {u.source}</span>
              </li>
            ))}
          </ul>
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="secondary"
              icon={ACTION_ICONS.confirm_assumptions}
              disabled={busy || !confirm.enabled || pending !== null}
              loading={pending === "confirm_assumptions"}
              aria-describedby={confirm.disabled_reason && !confirm.enabled ? confirmReasonId : undefined}
              onClick={() => raise(confirm)}
            >
              {confirm.label}
            </Button>
            {confirm.disabled_reason && !confirm.enabled ? (
              <span id={confirmReasonId} className="text-caption text-fg-muted">
                {confirm.disabled_reason}
              </span>
            ) : (
              <span className="text-caption text-fg-muted">I'll re-check the impact and options with these values.</span>
            )}
          </div>
        </section>
      ) : null}

      {alternatives.length > 0 ? (
        <section aria-label="Also considered" className="border-t border-border pt-3">
          <h4 className="mb-1.5 text-caption font-medium tracking-wide text-fg-subtle uppercase">Also considered</h4>
          <ul className="flex flex-col gap-1.5">
            {alternatives.map((alt) => (
              <li key={alt.id} className="text-ui">
                <span className="font-medium text-fg">{alt.title}</span>
                {alt.one_liner ? <span className="text-fg-muted"> — {alt.one_liner}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {actions.length > 0 ? (
        <div className="flex flex-col gap-2">
          {mix && props.remediation_mix ? (
            <section
              aria-label="How the changes are made"
              className="flex flex-col gap-1 rounded-md border border-border bg-surface-2 px-3 py-2"
              data-remediation-mix=""
            >
              <span id={mixId} className="sr-only">
                {mix}
              </span>
              <p aria-hidden="true" className="flex flex-wrap items-center gap-x-3 gap-y-1 text-ui font-medium text-fg">
                {mix.split(" · ").map((part, i) => (
                  <span key={part} className="inline-flex items-center gap-1.5">
                    {i > 0 ? <span className="text-fg-subtle">·</span> : null}
                    {part.startsWith("Apply") ? ACTION_ICONS.apply : ACTION_ICONS.guide}
                    {part}
                  </span>
                ))}
              </p>
              <p className="text-caption text-fg-muted">
                {props.remediation_mix.apply_lines > 0
                  ? `Apply makes ${props.remediation_mix.apply_lines === 1 ? "1 change" : `${props.remediation_mix.apply_lines} changes`} for you and gives you a step-by-step checklist for the other ${props.remediation_mix.guide_lines}.`
                  : "None of these changes can be made for you; you get a step-by-step checklist instead."}
              </p>
            </section>
          ) : null}
          <div className="flex flex-wrap gap-2" role="group" aria-label="Next step">
            {actions.map((a, i) => {
              const reasonId =
                a.disabled_reason && !a.enabled ? `${reasonBase}-${i}` : a.type === "apply" && mix ? mixId : undefined;
              return (
                <Button
                  key={a.type}
                  variant={(a.type === "apply" && !props.what_if) || (a.type === "promote" && props.what_if) ? "primary" : "secondary"}
                  icon={ACTION_ICONS[a.type]}
                  disabled={busy || !a.enabled || pending !== null}
                  loading={pending === a.type}
                  aria-describedby={reasonId}
                  onClick={() => raise(a)}
                >
                  {a.label}
                </Button>
              );
            })}
          </div>
          {actions.map((a, i) =>
            a.disabled_reason && !a.enabled ? (
              <p key={a.type} id={`${reasonBase}-${i}`} className="text-caption text-fg-muted">
                {a.label}: {a.disabled_reason}
              </p>
            ) : null,
          )}
        </div>
      ) : null}
    </BlockFrame>
  );
}
