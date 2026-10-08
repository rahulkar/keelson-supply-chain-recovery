"use client";

import type { ApprovalCardProps, ApprovalDecision } from "@keelson/contracts";
import { Badge, Button, Chip, Field, Meter, Spinner, Textarea } from "@keelson/ui";
import { CheckCircle2, CircleX, Plug, ShieldCheck } from "lucide-react";
import { useId, useState, type FormEvent } from "react";
import { formatDateTime, formatValue } from "../format";
import { currentReturnTo } from "../links";
import { NoBreakCodes } from "../lib/noBreak";
import { roleLabel } from "../lib/roles";
import { useActionRunner } from "../lib/useActionRunner";
import type { BlockComponentProps, BlockViewer } from "../types";
import { BlockFrame } from "./BlockFrame";
import { DecidedNote } from "./DecidedNote";
import { approvalNeedsLink, quorumSummary, viewerApproval } from "./quorum";

const DECISION_LABELS: Record<ApprovalDecision, string> = {
  approve: "Approve",
  reject: "Reject",
  revise: "Revise",
};

/** Longest note a decider may attach to reject / revise (contracts `MAX_DECISION_REASON_CHARS`). */
export const DECISION_REASON_MAX = 500;

/** The note form of a reject / revise decision. */
const REASON_FORM: Record<"reject" | "revise", { title: string; submit: string; hint: string }> = {
  reject: { title: "Reject these changes", submit: "Reject changes", hint: "Say why, so the planner knows what to change." },
  revise: { title: "Send back for revision", submit: "Send back", hint: "Say what should change before you approve." },
};

/** What the Merxa link of the person looking at the card allows. */
export type ApprovalConnectState = "ready" | "checking" | "connect" | "reconnect" | "upgrade";

/**
 * The viewer's link (`viewer.connections.merxa`, from `GET /api/connectors/merxa/link`) when
 * the app supplies it; the block's own `connect` prop (evaluated for the initiator when the card
 * was created) only when it does not (e.g. `/dev/blocks`).
 */
export function approvalConnectState(connect: ApprovalCardProps["connect"], viewer: BlockViewer | undefined): ApprovalConnectState {
  const link = viewer?.connections?.merxa;
  if (!link) return connect.status === "active" ? "ready" : connect.status === "expired" ? "reconnect" : "connect";
  switch (link.status) {
    case "checking":
      return "checking";
    case "unknown":
      return "ready"; // the decision API's 412 connect_required still sends them to connect
    case "active":
      return link.applyReady ? "ready" : "upgrade";
    case "expired":
      return link.applyReady ? "ready" : "reconnect";
    case "missing":
      return link.applyReady ? "ready" : "connect";
  }
}

const CONNECT_HINT: Record<Exclude<ApprovalConnectState, "ready">, string> = {
  checking: "Checking your Merxa connection…",
  connect: "Connect your Merxa account so the changes are applied under your name.",
  reconnect: "Your Merxa connection has expired. Reconnect to approve these changes.",
  upgrade: "Your Merxa connection doesn't allow applying changes. Reconnect to approve these changes.",
};

/**
 * Reject / revise with an optional note (at most 500 characters, sent as the decision's
 * `reason`). An empty note is sent without `reason`.
 */
function ReasonForm({
  decision,
  disabled,
  pending,
  onSubmit,
  onCancel,
}: {
  decision: "reject" | "revise";
  disabled: boolean;
  pending: boolean;
  onSubmit: (reason: string | null) => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState("");
  const titleId = useId();
  const copy = REASON_FORM[decision];
  const trimmed = reason.trim();
  const tooLong = trimmed.length > DECISION_REASON_MAX;
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (tooLong || disabled) return;
    onSubmit(trimmed ? trimmed : null);
  };
  return (
    <form
      aria-labelledby={titleId}
      className="flex flex-col gap-2 rounded-md border border-border bg-surface-2 px-3 py-3"
      onSubmit={submit}
      data-reason-form={decision}
    >
      <p id={titleId} className="text-ui font-medium text-fg">
        {copy.title}
      </p>
      <Field label="Reason (optional)" description={copy.hint} error={tooLong ? `At most ${DECISION_REASON_MAX} characters.` : null}>
        <Textarea value={reason} onChange={setReason} rows={3} maxLength={DECISION_REASON_MAX} />
      </Field>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" variant={decision === "reject" ? "danger" : "primary"} disabled={disabled || tooLong} loading={pending}>
          {copy.submit}
        </Button>
        <Button type="button" size="sm" variant="ghost" disabled={pending} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

/** "1 of 2 approvals: Marco Rossi ✓ · waiting for Compliance" + a meter and who approved when. */
function QuorumProgress({ quorum }: { quorum: NonNullable<ApprovalCardProps["quorum"]> }) {
  const summary = quorumSummary(quorum);
  return (
    <section aria-label="Approvals" className="flex flex-col gap-2 rounded-md border border-border px-3 py-2.5" data-quorum="">
      <Meter
        label="Approvals"
        value={summary.approved}
        max={summary.needed}
        valueText={`${summary.approved} of ${summary.needed} approvals`}
        warnAt={2}
        dangerAt={3}
        showLabel={false}
      />
      <p className="text-ui text-fg" data-quorum-text="">
        {summary.text}
      </p>
      {quorum.approvals.length > 0 ? (
        <ul aria-label="Approved by" className="flex flex-col gap-1 text-caption text-fg-muted">
          {quorum.approvals.map((a) => (
            <li key={a.sub} className="flex flex-wrap items-center gap-1.5">
              <CheckCircle2 aria-hidden="true" className="size-3.5 text-success" />
              <span className="font-medium text-fg">{a.name || a.sub}</span>
              <span>· {roleLabel(a.role)}</span>
              <span>· {formatDateTime(a.at)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {quorum.required.exclude_initiator ? (
        <p className="text-caption text-fg-muted">The person who started this change can't approve it.</p>
      ) : null}
    </section>
  );
}

/** "Rejected by Lena Vogel (Compliance) · time" + the reason they gave. */
function RejectionNote({ rejection }: { rejection: NonNullable<ApprovalCardProps["rejection"]> }) {
  return (
    <div role="note" className="flex flex-col gap-1 rounded-md border border-danger/40 bg-danger-soft px-3 py-2.5 text-ui" data-rejection="">
      <p className="flex flex-wrap items-center gap-1.5 font-medium text-danger">
        <CircleX aria-hidden="true" className="size-4" />
        Rejected by {rejection.name || rejection.sub}
        {rejection.role ? ` (${roleLabel(rejection.role)})` : ""}
        <span className="font-normal text-fg-muted">· {formatDateTime(rejection.at)}</span>
      </p>
      {rejection.reason ? (
        <p className="text-fg">
          <span className="text-fg-muted">Reason: </span>
          {rejection.reason}
        </p>
      ) : (
        <p className="text-fg-muted">No reason given.</p>
      )}
    </div>
  );
}

/**
 * `approval_card@1` — diff summary + why approval is needed. Approve is only enabled once the
 * **viewer** has an active Merxa link that may apply changes; otherwise **Connect Merxa** comes
 * first (and while the link is being checked, Approve waits). M5 (`docs/contracts/95` §1, §5):
 * quorum progress, reject / revise with a reason, the rejection note and multi-system bundles. A
 * co-approver (whose approval does not execute the change set) needs no Merxa link, and a viewer
 * who already approved sees that instead of the buttons.
 */
export function ApprovalCard({ props, envelope, onAction, busy, viewer, decision }: BlockComponentProps<ApprovalCardProps>) {
  const { pending, run } = useActionRunner(onAction);
  const connectHintId = useId();
  const [reasonFor, setReasonFor] = useState<"reject" | "revise" | null>(null);
  const needsLink = approvalNeedsLink(props, viewer);
  const state = needsLink ? approvalConnectState(props.connect, viewer) : "ready";
  const connected = state === "ready";
  const disabled = busy || pending !== null;
  const mine = viewerApproval(props.quorum, viewer);
  const closed = !!decision || !!props.rejection || !!mine;
  const neededRoles = props.quorum?.required.roles ?? props.required_roles;

  const decide = async (choice: ApprovalDecision, reason: string | null = null) => {
    const payload: Record<string, unknown> =
      choice === "approve"
        ? { plan_version: props.plan_version, change_set_hash: props.change_set_hash }
        : { plan_version: props.plan_version, ...(reason ? { reason } : {}) };
    await run(choice, { type: "interrupt_decision", interruptId: props.interrupt_id, decision: choice, payload });
  };

  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle="Approval needed"
      eyebrow="Approval"
      icon={<ShieldCheck />}
      tone={props.rejection ? "danger" : "warning"}
      showProvenance={false}
      actions={<Badge tone="neutral">Plan v{props.plan_version}</Badge>}
    >
      <dl className="grid grid-cols-3 gap-3">
        {(
          [
            ["Lines", formatValue(props.summary.lines, "count")],
            ["Units", formatValue(props.summary.units, "units")],
            ["Value", formatValue(props.summary.value_cents, "eur_cents")],
          ] as const
        ).map(([k, v]) => (
          <div key={k} className="rounded-md bg-surface-2 px-3 py-2">
            <dt className="text-caption text-fg-muted">{k}</dt>
            <dd className="text-body font-semibold tabular-nums">{v}</dd>
          </div>
        ))}
      </dl>

      <div className="flex flex-col gap-2 text-ui">
        <p>
          <span className="font-medium text-fg">Why approval is needed: </span>
          <span className="text-fg-muted">
            <NoBreakCodes text={props.reason} />
          </span>
        </p>
        {neededRoles.length > 0 ? (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-fg-muted">Needs sign-off from</span>
            {neededRoles.map((r) => (
              <Chip key={r}>{roleLabel(r)}</Chip>
            ))}
          </div>
        ) : null}
        {props.bundle.length > 0 ? (
          <section aria-label="Systems changed" className="flex flex-col gap-1">
            <p className="text-fg-muted">
              One approval covers the changes in {props.bundle.length === 1 ? "1 system" : `${props.bundle.length} systems`}:
            </p>
            <ul className="flex flex-col gap-0.5">
              {props.bundle.map((m) => (
                <li key={m.change_set_id} className="flex flex-wrap items-baseline gap-x-2">
                  <span className="font-medium text-fg">{m.system}</span>
                  <span className="text-fg-muted">
                    {formatValue(m.lines, "count")} line{m.lines === 1 ? "" : "s"}
                  </span>
                  <span className="font-mono text-caption whitespace-nowrap text-fg-subtle">{m.change_set_id}</span>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        <p className="text-caption text-fg-subtle">
          Change set <span className="font-mono whitespace-nowrap">{props.change_set_id}</span> · fingerprint{" "}
          <span className="font-mono" title={props.change_set_hash}>
            {props.change_set_hash.slice(0, 12)}
          </span>
        </p>
      </div>

      {props.quorum && !props.rejection ? <QuorumProgress quorum={props.quorum} /> : null}
      {props.rejection ? <RejectionNote rejection={props.rejection} /> : null}

      {closed ? null : state === "checking" ? (
        <p id={connectHintId} className="flex items-center gap-2 text-ui text-fg-muted" data-connect-state="checking">
          <Spinner size={14} />
          {CONNECT_HINT.checking}
        </p>
      ) : !connected ? (
        <div
          className="flex flex-col gap-3 rounded-md border border-accent/40 bg-accent-soft px-4 py-3 sm:flex-row sm:items-center"
          data-connect-state={state}
        >
          <p id={connectHintId} className="flex-1 text-ui text-accent-soft-fg">
            {CONNECT_HINT[state]}
          </p>
          <Button
            variant="primary"
            icon={<Plug className="size-4" />}
            disabled={disabled}
            loading={pending === "connect"}
            onClick={() => void run("connect", { type: "connect", connector: "merxa", returnTo: currentReturnTo() })}
          >
            {state === "connect" ? "Connect Merxa" : "Reconnect Merxa"}
          </Button>
        </div>
      ) : !needsLink && props.quorum ? (
        <p className="text-caption text-fg-muted" data-co-approval="">
          Your approval counts as a co-approval: the changes are applied under the approver's Merxa account.
        </p>
      ) : null}

      {decision ? (
        <DecidedNote decision={decision} />
      ) : props.rejection ? null : mine ? (
        <DecidedNote
          decision={{
            status: props.quorum && quorumSummary(props.quorum).met ? "decided" : "recorded",
            decision: "approve",
            by: mine.name || mine.sub,
            at: mine.at,
          }}
        />
      ) : reasonFor ? (
        <ReasonForm
          key={reasonFor}
          decision={reasonFor}
          disabled={disabled}
          pending={pending === reasonFor}
          onSubmit={(reason) => void decide(reasonFor, reason)}
          onCancel={() => setReasonFor(null)}
        />
      ) : (
        <div className="flex flex-wrap gap-2" role="group" aria-label="Approval decision">
          {props.buttons.map((b) => (
            <Button
              key={b}
              variant={b === "approve" ? "primary" : b === "reject" ? "secondary" : "ghost"}
              disabled={disabled || (b === "approve" && !connected)}
              aria-describedby={b === "approve" && !connected ? connectHintId : undefined}
              loading={pending === b}
              onClick={() => (b === "approve" ? void decide(b) : setReasonFor(b))}
            >
              {DECISION_LABELS[b]}
            </Button>
          ))}
        </div>
      )}
    </BlockFrame>
  );
}
