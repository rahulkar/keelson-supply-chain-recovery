"use client";

import { ArrowUp, Square } from "lucide-react";
import {
  forwardRef,
  useCallback,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { cn } from "../lib/cn";
import { IconButton } from "./Button";
import { Chip } from "./Badge";

export type Suggestion = string | { id?: string | undefined; label: string; prompt?: string | undefined; icon?: ReactNode | undefined };

function norm(s: Suggestion, i: number): { id: string; label: string; prompt: string; icon?: ReactNode | undefined } {
  if (typeof s === "string") return { id: `s${i}`, label: s, prompt: s };
  return { id: s.id ?? `s${i}`, label: s.label, prompt: s.prompt ?? s.label, icon: s.icon };
}

export type SuggestionChipsProps = {
  suggestions: Suggestion[];
  onSelect: (prompt: string) => void;
  /** Accessible name of the group (default "Suggestions"). */
  label?: string | undefined;
  disabled?: boolean | undefined;
  className?: string | undefined;
};

/** Row of pill suggestions (home screen, follow-ups at the end of a turn). */
export function SuggestionChips({ suggestions, onSelect, label = "Suggestions", disabled, className }: SuggestionChipsProps) {
  if (suggestions.length === 0) return null;
  return (
    <ul aria-label={label} className={cn("flex flex-wrap gap-2", className)}>
      {suggestions.map((s, i) => {
        const item = norm(s, i);
        return (
          <li key={item.id} className="max-w-full">
            <Chip icon={item.icon} disabled={disabled} onClick={() => onSelect(item.prompt)}>
              {item.label}
            </Chip>
          </li>
        );
      })}
    </ul>
  );
}

export type ComposerHandle = { focus: () => void };

export type ComposerProps = {
  value: string;
  onChange: (value: string) => void;
  /** Called with the trimmed text on Enter / send. Not called while `busy` or when empty. */
  onSubmit: (value: string) => void;
  /** A run is in progress: the send button becomes Stop (when `onStop` is given). */
  busy?: boolean | undefined;
  onStop?: (() => void) | undefined;
  placeholder?: string | undefined;
  /** Suggestions shown under the composer while it is empty. */
  suggestions?: Suggestion[] | undefined;
  /** `fill` (default) puts the suggestion in the box; `submit` sends it straight away. */
  suggestionBehavior?: "fill" | "submit" | undefined;
  /** Accessible name of the text box (default: the placeholder or "Message"). */
  label?: string | undefined;
  disabled?: boolean | undefined;
  autoFocus?: boolean | undefined;
  maxLength?: number | undefined;
  /** Max rows before the textarea scrolls (default 8). */
  maxRows?: number | undefined;
  className?: string | undefined;
};

/**
 * The hero pill composer. Enter sends, Shift+Enter adds a line, IME composition is respected,
 * the textarea grows with content. While `busy`, sending is blocked and Stop is offered.
 */
export const Composer = forwardRef<ComposerHandle, ComposerProps>(function Composer(
  {
    value,
    onChange,
    onSubmit,
    busy = false,
    onStop,
    placeholder = "Describe a disruption or ask a question…",
    suggestions,
    suggestionBehavior = "fill",
    label,
    disabled = false,
    autoFocus,
    maxLength = 4000,
    maxRows = 8,
    className,
  },
  ref,
) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => ({ focus: () => textareaRef.current?.focus() }), []);

  const resize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const line = parseFloat(getComputedStyle(el).lineHeight) || 24;
    el.style.height = `${Math.min(el.scrollHeight, line * maxRows)}px`;
  }, [maxRows]);
  useLayoutEffect(resize, [value, resize]);

  const canSend = !busy && !disabled && value.trim().length > 0;

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (!canSend) return;
    onSubmit(value.trim());
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const selectSuggestion = (prompt: string) => {
    if (suggestionBehavior === "submit" && !busy && !disabled) {
      onSubmit(prompt);
      return;
    }
    onChange(prompt);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const showStop = busy && !!onStop;

  return (
    <div className={cn("flex w-full flex-col gap-3", className)}>
      <form
        onSubmit={submit}
        aria-busy={busy || undefined}
        className={cn(
          "flex items-end gap-2 rounded-xl border border-border bg-surface py-2 pr-2 pl-5 shadow-composer",
          "transition-[border-color,box-shadow] duration-150 focus-within:border-focus focus-within:ring-1 focus-within:ring-focus",
          disabled && "opacity-60",
        )}
      >
        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={placeholder}
          aria-label={label ?? placeholder ?? "Message"}
          disabled={disabled}
          autoFocus={autoFocus}
          maxLength={maxLength}
          enterKeyHint="send"
          className="max-h-60 min-h-6 flex-1 resize-none self-center bg-transparent py-2 text-body text-fg outline-none placeholder:text-fg-subtle"
        />
        {showStop ? (
          <IconButton label="Stop" variant="secondary" icon={<Square className="size-3.5 fill-current" />} onClick={onStop} />
        ) : (
          <IconButton
            type="submit"
            label="Send"
            variant="primary"
            disabled={!canSend}
            loading={busy && !onStop}
            icon={<ArrowUp className="size-5" />}
          />
        )}
      </form>
      {suggestions && suggestions.length > 0 && value.length === 0 ? (
        <SuggestionChips suggestions={suggestions} onSelect={selectSuggestion} disabled={disabled} className="justify-center" />
      ) : null}
    </div>
  );
});
