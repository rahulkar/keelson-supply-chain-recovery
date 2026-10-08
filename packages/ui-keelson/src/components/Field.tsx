"use client";

/**
 * Form primitives for dense admin screens: `Field` (label + help + error, wires ids), `TextInput`,
 * `Textarea` (character counter), `Select` (native, so keyboard / screen-reader behaviour is the
 * platform's), `Checkbox` and `Switch` (a `button role="switch"`).
 *
 * A control inside a `Field` picks up its `id`, `aria-describedby` and `aria-invalid` from the
 * field, so the label, description, error and counter are always announced with it.
 */
import { Lock } from "lucide-react";
import {
  createContext,
  forwardRef,
  useContext,
  useId,
  type ChangeEvent,
  type ComponentPropsWithoutRef,
  type ReactNode,
} from "react";
import { cn } from "../lib/cn";

type FieldContextValue = {
  id: string;
  describedBy: string | undefined;
  invalid: boolean;
  disabled: boolean;
  /** Id the counter of a `Textarea` uses (already part of `describedBy`). */
  counterId: string | undefined;
};

const FieldContext = createContext<FieldContextValue | null>(null);

function joinIds(...ids: (string | false | null | undefined)[]): string | undefined {
  const out = ids.filter((x): x is string => typeof x === "string" && x.length > 0);
  return out.length ? out.join(" ") : undefined;
}

export type FieldProps = {
  label: ReactNode;
  /** Help under the label. */
  description?: ReactNode | undefined;
  /** Validation message (sets `aria-invalid` on the control and is announced politely). */
  error?: ReactNode | undefined;
  /**
   * Field is locked for this person (e.g. "Requires platform admin"): shows a lock and the
   * reason, which is part of the control's description.
   */
  lockedReason?: ReactNode | undefined;
  /** Visually hide the label (it still names the control). */
  hideLabel?: boolean | undefined;
  /** Mark as required (visual asterisk; set `required` on the control too). */
  required?: boolean | undefined;
  disabled?: boolean | undefined;
  /** Use a fixed id for the control (default: generated). */
  id?: string | undefined;
  /** Reserve a counter slot (`Textarea maxLength`). */
  withCounter?: boolean | undefined;
  children: ReactNode;
  className?: string | undefined;
};

/** Label + control + description + error, ids wired for assistive tech. */
export function Field({
  label,
  description,
  error,
  lockedReason,
  hideLabel,
  required,
  disabled = false,
  id,
  withCounter,
  children,
  className,
}: FieldProps) {
  const auto = useId();
  const controlId = id ?? `f-${auto}`;
  const descId = `${controlId}-desc`;
  const errId = `${controlId}-err`;
  const lockId = `${controlId}-lock`;
  const counterId = `${controlId}-count`;
  const describedBy = joinIds(description ? descId : null, lockedReason ? lockId : null, error ? errId : null, withCounter ? counterId : null);
  return (
    <FieldContext.Provider value={{ id: controlId, describedBy, invalid: !!error, disabled, counterId: withCounter ? counterId : undefined }}>
      <div className={cn("flex min-w-0 flex-col gap-1", className)} data-invalid={error ? "" : undefined}>
        <label htmlFor={controlId} className={cn("flex items-center gap-1.5 text-ui font-medium text-fg", hideLabel && "sr-only")}>
          {label}
          {required ? (
            <span aria-hidden="true" className="text-danger">
              *
            </span>
          ) : null}
          {lockedReason ? <Lock aria-hidden="true" className="size-3.5 text-fg-subtle" /> : null}
        </label>
        {description ? (
          <p id={descId} className="text-caption text-fg-muted">
            {description}
          </p>
        ) : null}
        {children}
        {lockedReason ? (
          <p id={lockId} className="flex items-center gap-1 text-caption text-fg-muted">
            {lockedReason}
          </p>
        ) : null}
        {error ? (
          <p id={errId} className="text-caption font-medium text-danger" aria-live="polite">
            {error}
          </p>
        ) : null}
      </div>
    </FieldContext.Provider>
  );
}

function useFieldProps(
  props: { id?: string | undefined; "aria-describedby"?: string | undefined; "aria-invalid"?: ComponentPropsWithoutRef<"input">["aria-invalid"]; disabled?: boolean | undefined },
) {
  const field = useContext(FieldContext);
  return {
    id: props.id ?? field?.id,
    "aria-describedby": joinIds(field?.describedBy, props["aria-describedby"]),
    "aria-invalid": props["aria-invalid"] ?? (field?.invalid ? true : undefined),
    disabled: props.disabled ?? field?.disabled ?? false,
    counterId: field?.counterId,
  };
}

const control =
  "w-full min-w-0 rounded-sm border border-border-strong bg-surface px-2.5 text-ui text-fg placeholder:text-fg-subtle " +
  "focus-visible:border-focus focus-visible:outline-2 focus-visible:outline-offset-0 focus-visible:outline-focus " +
  "disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-fg-muted aria-invalid:border-danger";

export type TextInputProps = Omit<ComponentPropsWithoutRef<"input">, "size"> & { mono?: boolean | undefined };

/** Single-line text input (32 px, compact). */
export const TextInput = forwardRef<HTMLInputElement, TextInputProps>(function TextInput({ className, mono, ...rest }, ref) {
  const { counterId: _c, ...wired } = useFieldProps(rest);
  return <input ref={ref} {...rest} {...wired} className={cn(control, "h-8", mono && "font-mono", className)} />;
});

export type TextareaProps = Omit<ComponentPropsWithoutRef<"textarea">, "value" | "onChange"> & {
  value: string;
  onChange: (value: string, event: ChangeEvent<HTMLTextAreaElement>) => void;
  /** Character limit; shows a counter ("120 / 4,000") and blocks typing past it. */
  maxLength?: number | undefined;
  /** Show the counter (default: when `maxLength` is set). */
  showCount?: boolean | undefined;
};

const nf = new Intl.NumberFormat("en-GB");

/** Characters left announcement thresholds: only near the limit (never every keystroke). */
export function counterAnnouncement(length: number, max: number): string {
  const left = max - length;
  if (left < 0) return `${nf.format(-left)} characters over the limit`;
  if (left <= Math.max(10, Math.floor(max * 0.1))) return left === 0 ? "Character limit reached" : `${nf.format(left)} characters left`;
  return "";
}

/** Multi-line text with an optional character counter (announced politely near the limit). */
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { value, onChange, maxLength, showCount, className, rows = 4, ...rest },
  ref,
) {
  const { counterId, ...wired } = useFieldProps(rest);
  const ownCounterId = useId();
  const counter = (showCount ?? maxLength !== undefined) && maxLength !== undefined;
  const cid = counterId ?? `c-${ownCounterId}`;
  const over = maxLength !== undefined && value.length > maxLength;
  const announcement = maxLength !== undefined ? counterAnnouncement(value.length, maxLength) : "";
  return (
    <div className="flex flex-col gap-1">
      <textarea
        ref={ref}
        rows={rows}
        {...rest}
        {...wired}
        aria-describedby={counter && !counterId ? joinIds(wired["aria-describedby"], cid) : wired["aria-describedby"]}
        aria-invalid={over ? true : wired["aria-invalid"]}
        maxLength={maxLength}
        value={value}
        onChange={(e) => onChange(e.target.value, e)}
        className={cn(control, "resize-y py-2 leading-5", className)}
      />
      {counter ? (
        <div className="flex justify-end">
          <span id={cid} className={cn("text-caption tabular-nums", over ? "font-medium text-danger" : "text-fg-subtle")}>
            {nf.format(value.length)} / {nf.format(maxLength)}
            <span className="sr-only"> characters</span>
          </span>
          <span className="sr-only" aria-live="polite">
            {announcement}
          </span>
        </div>
      ) : null}
    </div>
  );
});

export type SelectOption = { value: string; label: string; disabled?: boolean | undefined };

export type SelectProps = Omit<ComponentPropsWithoutRef<"select">, "onChange" | "value" | "size"> & {
  value: string;
  onChange: (value: string) => void;
  options: readonly SelectOption[];
  /** First, empty option (e.g. "Choose a model…"). */
  placeholder?: string | undefined;
};

/** Native select, styled (platform keyboard + screen-reader behaviour). */
export const Select = forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { value, onChange, options, placeholder, className, ...rest },
  ref,
) {
  const { counterId: _c, ...wired } = useFieldProps(rest);
  return (
    <select
      ref={ref}
      {...rest}
      {...wired}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={cn(control, "h-8 cursor-pointer pr-8", className)}
    >
      {placeholder !== undefined ? (
        <option value="" disabled>
          {placeholder}
        </option>
      ) : null}
      {options.map((o) => (
        <option key={o.value} value={o.value} disabled={o.disabled}>
          {o.label}
        </option>
      ))}
    </select>
  );
});

export type CheckboxProps = Omit<ComponentPropsWithoutRef<"input">, "type" | "onChange" | "checked"> & {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  label: ReactNode;
  /** Secondary line under the label. */
  description?: ReactNode | undefined;
  /** Why the box can't be changed (shown and announced with the box). */
  disabledReason?: ReactNode | undefined;
};

/** Native checkbox with a label, optional description and an announced disabled reason. */
export const Checkbox = forwardRef<HTMLInputElement, CheckboxProps>(function Checkbox(
  { checked, onCheckedChange, label, description, disabledReason, disabled, className, id, "aria-describedby": extraDesc, ...rest },
  ref,
) {
  const auto = useId();
  const boxId = id ?? `cb-${auto}`;
  const descId = description ? `${boxId}-desc` : undefined;
  const reasonId = disabledReason ? `${boxId}-why` : undefined;
  return (
    <div className={cn("flex items-start gap-2.5", className)} data-disabled={disabled ? "" : undefined}>
      <input
        ref={ref}
        type="checkbox"
        {...rest}
        id={boxId}
        checked={checked}
        disabled={disabled}
        aria-describedby={joinIds(descId, reasonId, extraDesc)}
        onChange={(e) => onCheckedChange(e.target.checked)}
        className="mt-0.5 size-4 shrink-0 cursor-pointer accent-[var(--k-accent)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed"
      />
      <div className="min-w-0">
        <label htmlFor={boxId} className={cn("text-ui text-fg", disabled ? "cursor-not-allowed text-fg-muted" : "cursor-pointer")}>
          {label}
        </label>
        {description ? (
          <p id={descId} className="text-caption text-fg-muted">
            {description}
          </p>
        ) : null}
        {disabledReason ? (
          <p id={reasonId} className="text-caption text-fg-subtle">
            {disabledReason}
          </p>
        ) : null}
      </div>
    </div>
  );
});

export type SwitchProps = Omit<ComponentPropsWithoutRef<"button">, "onChange" | "type" | "role"> & {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  /** Visible label (names the switch). Use `aria-label` instead when there is no visible label. */
  label?: ReactNode | undefined;
  description?: ReactNode | undefined;
};

/** On/off switch: `button role="switch"` + `aria-checked`; Space/Enter toggle (native button). */
export const Switch = forwardRef<HTMLButtonElement, SwitchProps>(function Switch(
  { checked, onCheckedChange, label, description, disabled, className, id, "aria-describedby": extraDesc, ...rest },
  ref,
) {
  const auto = useId();
  const sid = id ?? `sw-${auto}`;
  const labelId = label ? `${sid}-label` : undefined;
  const descId = description ? `${sid}-desc` : undefined;
  const button = (
    <button
      ref={ref}
      {...rest}
      type="button"
      role="switch"
      id={sid}
      aria-checked={checked}
      aria-labelledby={rest["aria-label"] ? undefined : labelId}
      aria-describedby={joinIds(descId, extraDesc)}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      className={cn(
        "relative inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-pill border border-transparent transition-colors duration-150",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus disabled:cursor-not-allowed disabled:opacity-55",
        checked ? "bg-accent" : "bg-border-strong",
        !label && className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          "inline-block size-4 rounded-pill bg-surface shadow-sm transition-transform duration-150 reduced-motion:transition-none",
          checked ? "translate-x-4" : "translate-x-0.5",
        )}
      />
    </button>
  );
  if (!label) return button;
  return (
    <div className={cn("flex items-start gap-2.5", className)}>
      {button}
      <div className="min-w-0">
        <span id={labelId} className="text-ui text-fg">
          {label}
        </span>
        {description ? (
          <p id={descId} className="text-caption text-fg-muted">
            {description}
          </p>
        ) : null}
      </div>
    </div>
  );
});
