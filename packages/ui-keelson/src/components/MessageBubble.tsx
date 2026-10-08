"use client";

import { Sparkles } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../lib/cn";

export type MessageRole = "user" | "assistant" | "system";

export type MessageBubbleProps = {
  role: MessageRole;
  children: ReactNode;
  /** Screen-reader label for the author (defaults: "You" / "Assistant" / "System"). */
  author?: string | undefined;
  /** Optional trailing row (copy, follow-up chips…). */
  footer?: ReactNode | undefined;
  className?: string | undefined;
};

const defaultAuthor: Record<MessageRole, string> = { user: "You", assistant: "Assistant", system: "System" };

/**
 * One thread message. The user's turn is a soft rounded bubble on the right; the assistant's
 * turn is unboxed, full-width prose with a small mark (calm, Gemini-like behaviour).
 */
export function MessageBubble({ role, children, author, footer, className }: MessageBubbleProps) {
  const who = author ?? defaultAuthor[role];
  if (role === "user") {
    return (
      <article aria-label={`${who} said`} className={cn("flex justify-end", className)} data-role="user">
        <div className="max-w-[85%] rounded-xl rounded-br-sm bg-surface-2 px-4 py-2.5 text-body break-words whitespace-pre-wrap text-fg">
          {children}
        </div>
      </article>
    );
  }
  if (role === "system") {
    return (
      <article aria-label={who} className={cn("flex justify-center", className)} data-role="system">
        <p className="rounded-pill bg-surface-2 px-3 py-1 text-caption text-fg-muted">{children}</p>
      </article>
    );
  }
  return (
    <article aria-label={`${who} said`} className={cn("flex gap-3", className)} data-role="assistant">
      <span
        aria-hidden="true"
        className="mt-0.5 inline-flex size-7 shrink-0 items-center justify-center rounded-pill bg-accent-soft text-accent-soft-fg"
      >
        <Sparkles className="size-4" />
      </span>
      <div className="min-w-0 flex-1 text-body text-fg">
        {children}
        {footer ? <div className="mt-3">{footer}</div> : null}
      </div>
    </article>
  );
}
