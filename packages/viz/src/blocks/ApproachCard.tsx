"use client";

import { checkSentence, checkTaskId, type ApproachCardProps, type ApproachDecision, type EditableValue } from "@keelson/contracts";
import { AvatarStack, Badge, Button, Checkbox, ProvenanceChip, TextInput } from "@keelson/ui";
import { Compass, X } from "lucide-react";
import { useEffect, useId, useRef, useState, type FormEvent, type RefObject } from "react";
import { formatValue } from "../format";
import { useActionRunner } from "../lib/useActionRunner";
import { NoBreakCodes } from "../lib/noBreak";
import type { BlockComponentProps } from "../types";
import { approachEditDraft, approachEditResult, hasTypedEdit, parseIdList, type ApproachEditDraft } from "./approachEdit";
import { BlockFrame } from "./BlockFrame";
import { DecidedNote } from "./DecidedNote";

const DECISION_LABELS: Record<ApproachDecision, string> = {
  confirm: "Looks right",
  edit: "Edit",
  what_if: "Treat as what-if",
  cancel: "Cancel",
};

const FAMILY_LABELS: Record<ApproachCardProps["event"]["family"], string> = {
  SUP: "Supplier",
  LOG: "Logistics",
  NAT: "Natural hazard",
  CYB: "Cyber",
  REG: "Regulatory",
  DEM: "Demand",
  OTHER: "Other",
};

const ENTITY_LABELS: Record<string, string> = {
  supplier: "Supplier",
  po: "Purchase order",
  po_line: "PO line",
  sku: "SKU",
  style: "Style",
  dc: "DC",
  cluster: "Store cluster",
  store: "Store",
  factory: "Factory",
  component: "Component",
  port: "Port",
  lane: "Lane",
  chokepoint: "Chokepoint",
  region: "Region",
  other: "Other",
};

/** Plain words for an event type: taxonomy keys ("LOG.chokepoint_closure") and M1 free text. */
export function eventTypeLabel(type: string): string {
  const words = type.replace(/^[A-Z]+\./, "").replace(/[_.]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : type;
}

/** "A", "A and B", "A, B and C". */
export function listSentence(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** Ids shown before "Show all" in an id list editor. */
const IDS_PREVIEW = 12;

const UNIT_SUFFIX: Record<Exclude<EditableValue["kind"], "ids">, string> = { days: "days", pct: "%" };

function NumberEdit({
  value,
  editable,
  error,
  disabled,
  onChange,
}: {
  value: string;
  editable: EditableValue;
  error: string | undefined;
  disabled: boolean;
  onChange: (v: string) => void;
}) {
  const id = useId();
  const errId = `${id}-err`;
  const kind = editable.kind === "pct" ? "pct" : "days";
  return (
    <div className="flex flex-col gap-1" data-edit-key={editable.key}>
      <label htmlFor={id} className="text-ui font-medium text-fg">
        {editable.label}
      </label>
      <div className="flex items-center gap-2">
        <TextInput
          id={id}
          type="number"
          inputMode="numeric"
          step={1}
          min={kind === "pct" ? -100 : 0}
          value={value}
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? errId : undefined}
          onChange={(e) => onChange(e.target.value)}
          className="h-8 w-28"
        />
        <span className="text-ui text-fg-muted">{UNIT_SUFFIX[kind]}</span>
      </div>
      {error ? (
        <p id={errId} className="text-caption font-medium text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function IdsEdit({
  ids,
  editable,
  error,
  disabled,
  onChange,
}: {
  ids: string[];
  editable: EditableValue;
  error: string | undefined;
  disabled: boolean;
  onChange: (ids: string[]) => void;
}) {
  const id = useId();
  const [adding, setAdding] = useState("");
  const [showAll, setShowAll] = useState(false);
  const shown = showAll ? ids : ids.slice(0, IDS_PREVIEW);
  const add = () => {
    const extra = parseIdList(adding).filter((x) => !ids.includes(x));
    if (extra.length) onChange([...ids, ...extra]);
    setAdding("");
  };
  return (
    <fieldset className="flex min-w-0 flex-col gap-1.5" data-edit-key={editable.key} aria-describedby={error ? `${id}-err` : undefined}>
      <legend className="text-ui font-medium text-fg">
        {editable.label} <span className="font-normal text-fg-muted">({formatValue(ids.length, "count")})</span>
      </legend>
      {ids.length > 0 ? (
        <ul className="flex flex-wrap gap-1.5" aria-label={`${editable.label}: current values`}>
          {shown.map((x) => (
            <li key={x} className="inline-flex items-center gap-1 rounded-pill bg-surface-2 py-0.5 pr-1 pl-2.5 text-caption">
              <span className="font-mono whitespace-nowrap text-fg">{x}</span>
              <button
                type="button"
                disabled={disabled}
                aria-label={`Remove ${x}`}
                onClick={() => onChange(ids.filter((y) => y !== x))}
                className="inline-flex size-5 items-center justify-center rounded-pill text-fg-muted hover:bg-surface-3 hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus disabled:cursor-not-allowed"
              >
                <X aria-hidden="true" className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {ids.length > IDS_PREVIEW ? (
        <button
          type="button"
          className="self-start text-caption font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          aria-expanded={showAll}
          onClick={() => setShowAll((v) => !v)}
        >
          {showAll ? "Show fewer" : `Show all (${formatValue(ids.length - IDS_PREVIEW, "count")} more)`}
        </button>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={`${id}-add`} className="sr-only">
          Add to {editable.label}
        </label>
        <TextInput
          id={`${id}-add`}
          mono
          value={adding}
          disabled={disabled}
          placeholder="Add codes, separated by commas"
          onChange={(e) => setAdding(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
          className="h-8 min-w-0 flex-1 basis-48"
        />
        <Button size="sm" variant="secondary" disabled={disabled || !adding.trim()} onClick={add}>
          Add
        </Button>
      </div>
      {error ? (
        <p id={`${id}-err`} className="text-caption font-medium text-danger">
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}

/** Typed Edit form (M4): editable values and which checks to keep. */
function TypedEditForm({
  props,
  disabled,
  sending,
  firstFieldRef,
  onSubmit,
  onBack,
}: {
  props: ApproachCardProps;
  disabled: boolean;
  sending: boolean;
  firstFieldRef: RefObject<HTMLDivElement | null>;
  onSubmit: (payload: NonNullable<ReturnType<typeof approachEditResult>["payload"]>) => void;
  onBack: () => void;
}) {
  const [draft, setDraft] = useState<ApproachEditDraft>(() => approachEditDraft(props));
  const [submitted, setSubmitted] = useState(false);
  const checksErrId = `${useId()}-checks-err`;
  const result = approachEditResult(props, draft);
  const errors = submitted ? result.errors : Object.fromEntries(Object.entries(result.errors).filter(([k]) => k === "checks"));
  const taskChecks = props.checks.filter((c) => checkTaskId(c) !== null);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (result.payload) onSubmit(result.payload);
  };
  return (
    <form className="flex flex-col gap-3" onSubmit={submit} aria-label="Edit the approach" noValidate>
      <div ref={firstFieldRef} tabIndex={-1} className="flex flex-col gap-3 outline-none">
        <p className="text-ui text-fg-muted">Change any value, or untick checks you don't need. I'll redo the approach with your changes.</p>
        {props.editable.map((e) =>
          e.kind === "ids" ? (
            <IdsEdit
              key={e.key}
              editable={e}
              ids={draft.ids[e.key] ?? []}
              error={errors[e.key]}
              disabled={disabled}
              onChange={(ids) => setDraft((d) => ({ ...d, ids: { ...d.ids, [e.key]: ids } }))}
            />
          ) : (
            <NumberEdit
              key={e.key}
              editable={e}
              value={draft.numbers[e.key] ?? ""}
              error={errors[e.key]}
              disabled={disabled}
              onChange={(v) => setDraft((d) => ({ ...d, numbers: { ...d.numbers, [e.key]: v } }))}
            />
          ),
        )}
        {taskChecks.length > 0 ? (
          <fieldset className="flex flex-col gap-1.5" aria-describedby={errors["checks"] ? checksErrId : undefined}>
            <legend className="mb-1 text-ui font-medium text-fg">Checks to run</legend>
            {taskChecks.map((c) => {
              const taskId = checkTaskId(c)!;
              return (
                <Checkbox
                  key={taskId}
                  checked={!draft.removedChecks.includes(taskId)}
                  disabled={disabled}
                  label={<NoBreakCodes text={checkSentence(c)} />}
                  onCheckedChange={(keep) =>
                    setDraft((d) => ({
                      ...d,
                      removedChecks: keep ? d.removedChecks.filter((x) => x !== taskId) : [...d.removedChecks, taskId],
                    }))
                  }
                />
              );
            })}
            {errors["checks"] ? (
              <p id={checksErrId} className="text-caption font-medium text-danger" role="alert">
                {errors["checks"]}
              </p>
            ) : null}
          </fieldset>
        ) : null}
      </div>
      {errors["form"] ? (
        <p className="text-caption font-medium text-danger" role="alert">
          {errors["form"]}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" variant="primary" disabled={disabled || !result.changed} loading={sending}>
          Send changes
        </Button>
        <Button variant="ghost" disabled={disabled} onClick={onBack}>
          Back
        </Button>
      </div>
    </form>
  );
}

/** `approach_card@1` — "Here's what I understood and what I'll check" (approach interrupt). */
export function ApproachCard({ props, envelope, onAction, busy, decision }: BlockComponentProps<ApproachCardProps>) {
  const { pending, run } = useActionRunner(onAction);
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState("");
  const editId = useId();
  const disabled = busy || pending !== null;
  const typed = hasTypedEdit(props);
  // Keep keyboard focus where the user is: Edit → the form, Back → the Edit button
  // (the element that had focus is unmounted by the switch).
  const editFieldRef = useRef<HTMLTextAreaElement>(null);
  const typedFormRef = useRef<HTMLDivElement>(null);
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const focusAfterSwitch = useRef(false);
  useEffect(() => {
    if (!focusAfterSwitch.current) return;
    focusAfterSwitch.current = false;
    (editing ? (editFieldRef.current ?? typedFormRef.current) : editButtonRef.current)?.focus();
  }, [editing]);
  const switchEditing = (next: boolean) => {
    focusAfterSwitch.current = true;
    setEditing(next);
  };

  const decide = (decision: ApproachDecision, payload?: Record<string, unknown>) =>
    run(decision, { type: "interrupt_decision", interruptId: props.interrupt_id, decision, ...(payload ? { payload } : {}) });

  const extra = props.matched.count - props.matched.sample.length;
  const whatIf = props.mode === "what_if";
  const agentNames = props.agents.map((a) => a.display_name);
  // "Treat as what-if" on a card that already is a what-if would do the same as "Looks right".
  const buttons = whatIf && props.buttons.some((b) => b !== "what_if") ? props.buttons.filter((b) => b !== "what_if") : props.buttons;

  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle="Here's what I understood and what I'll check"
      eyebrow="Approach"
      icon={<Compass />}
      showProvenance={false}
      actions={whatIf ? <Badge tone="info">What-if</Badge> : undefined}
    >
      {whatIf ? (
        <p className="rounded-md bg-info-soft px-3 py-2 text-ui text-fg" data-what-if="">
          This is a what-if: I'll compare options, but nothing can be applied until you promote it to an actual case.
        </p>
      ) : null}

      <section aria-label="Event" className="flex flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="neutral">{FAMILY_LABELS[props.event.family]}</Badge>
          <span className="text-ui font-medium text-fg">{eventTypeLabel(props.event.type)}</span>
        </div>
        <p className="text-body text-fg">
          <NoBreakCodes text={props.event.description} />
        </p>
        <p className="text-caption text-fg-muted">Source: {props.event.source}</p>
      </section>

      <section aria-label="Matched records" className="flex flex-col gap-1.5">
        {props.matched.sample.length > 0 ? (
          <details className="group rounded-md bg-surface-2 px-3 py-2">
            <summary className="cursor-pointer text-ui font-medium text-fg marker:text-fg-subtle">
              {formatValue(props.matched.count, "count")} matched {props.matched.count === 1 ? "record" : "records"}
            </summary>
            <ul className="mt-2 flex flex-col gap-1">
              {props.matched.sample.map((m) => (
                <li key={`${m.type}:${m.code}`} className="flex flex-wrap items-baseline gap-x-2 text-ui">
                  <span className="text-caption text-fg-subtle">{ENTITY_LABELS[m.type] ?? m.type}</span>
                  <span className="font-mono text-caption whitespace-nowrap text-fg">{m.code}</span>
                  <span className="text-fg-muted">
                    <NoBreakCodes text={m.label} />
                  </span>
                </li>
              ))}
              {extra > 0 ? <li className="text-caption text-fg-subtle">and {formatValue(extra, "count")} more</li> : null}
            </ul>
          </details>
        ) : (
          <p className="text-ui text-fg-muted">
            {props.matched.count === 0 ? "No matching records found yet." : `${formatValue(props.matched.count, "count")} matched records`}
          </p>
        )}
      </section>

      {props.assumptions.length > 0 ? (
        <section aria-label="What I'm assuming">
          <h4 className="mb-1.5 text-caption font-medium tracking-wide text-fg-subtle uppercase">What I'm assuming</h4>
          <dl className="flex flex-col gap-1.5">
            {props.assumptions.map((a) => (
              <div key={a.key} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-ui">
                <dt className="text-fg-muted">{a.label}</dt>
                <dd className="flex flex-wrap items-center gap-2 font-medium text-fg">
                  <span>
                    <NoBreakCodes text={a.value_text} />
                  </span>
                  {a.prov !== "fact" ? <ProvenanceChip prov={a.prov} /> : null}
                  {a.unconfirmed ? (
                    <Badge size="sm" tone="warning">
                      Unconfirmed
                    </Badge>
                  ) : null}
                </dd>
              </div>
            ))}
          </dl>
        </section>
      ) : null}

      {props.agents.length > 0 ? (
        <section aria-label="Who I'll consult" className="flex flex-col gap-1.5">
          <h4 className="text-caption font-medium tracking-wide text-fg-subtle uppercase">Who I'll consult</h4>
          <div className="flex items-center gap-2.5">
            <div aria-hidden="true" className="shrink-0">
              <AvatarStack agents={props.agents.map((a) => ({ id: a.slug, name: a.display_name }))} max={4} size="sm" />
            </div>
            <p className="min-w-0 text-ui text-fg" data-agents="">
              {listSentence(agentNames)}
            </p>
          </div>
        </section>
      ) : null}

      {props.checks.length > 0 ? (
        <section aria-label="What I'll check">
          <h4 className="mb-1.5 text-caption font-medium tracking-wide text-fg-subtle uppercase">What I'll check</h4>
          <ul className="flex list-disc flex-col gap-1 pl-5 text-ui text-fg marker:text-fg-subtle">
            {props.checks.map((c, i) => (
              <li key={checkTaskId(c) ?? `check-${i}`}>
                <NoBreakCodes text={checkSentence(c)} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {decision ? (
        <DecidedNote decision={decision} />
      ) : editing && typed ? (
        <TypedEditForm
          props={props}
          disabled={disabled}
          sending={pending === "edit"}
          firstFieldRef={typedFormRef}
          onSubmit={(payload) => void decide("edit", payload)}
          onBack={() => switchEditing(false)}
        />
      ) : editing ? (
        <form
          className="flex flex-col gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (edits.trim()) void decide("edit", { edits: edits.trim() });
          }}
        >
          <label htmlFor={editId} className="text-ui font-medium text-fg">
            What should I change?
          </label>
          <textarea
            id={editId}
            ref={editFieldRef}
            value={edits}
            onChange={(e) => setEdits(e.target.value)}
            rows={3}
            maxLength={2000}
            className="w-full resize-y rounded-md border border-border-strong bg-surface px-3 py-2 text-ui text-fg focus:border-focus focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus"
            placeholder="e.g. The delay is 2 weeks, not 3, and only affects PO-BD-0002"
          />
          <div className="flex flex-wrap gap-2">
            <Button type="submit" variant="primary" disabled={disabled || !edits.trim()} loading={pending === "edit"}>
              Send changes
            </Button>
            <Button variant="ghost" disabled={disabled} onClick={() => switchEditing(false)}>
              Back
            </Button>
          </div>
        </form>
      ) : (
        <div className="flex flex-wrap gap-2" role="group" aria-label="Approach decision">
          {buttons.map((b) => (
            <Button
              key={b}
              ref={b === "edit" ? editButtonRef : undefined}
              variant={b === "confirm" ? "primary" : b === "cancel" ? "ghost" : "secondary"}
              disabled={disabled}
              loading={pending === b}
              onClick={() => (b === "edit" ? switchEditing(true) : void decide(b))}
            >
              {DECISION_LABELS[b]}
            </Button>
          ))}
        </div>
      )}
    </BlockFrame>
  );
}
