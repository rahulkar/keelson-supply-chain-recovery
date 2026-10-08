"use client";

import { Button, toast } from "@keelson/ui";
import { CircleAlert, Copy } from "lucide-react";
import type { RunError } from "@/lib/stream/reducer";

/**
 * A failed run's public message and its error ID (the reference support searches the engine logs
 * for). The message was already screened by the reducer (`lib/errors.ts`): never exception text
 * or a stack trace. The ID is shown in full unless the message already spells it out.
 */
export function RunErrorNotice({ error }: { error: RunError }) {
  const { message, errorId } = error;
  const idInMessage = !!errorId && message.includes(errorId);

  const copy = async () => {
    if (!errorId) return;
    try {
      await navigator.clipboard.writeText(errorId);
      toast("Error ID copied");
    } catch {
      toast.error("Couldn't copy the error ID", { description: errorId });
    }
  };

  return (
    <div
      className="flex flex-col gap-2 rounded-md border border-danger/40 bg-danger-soft px-4 py-3 text-ui sm:flex-row sm:items-start"
      data-testid="run-error"
    >
      <CircleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-danger" />
      <div className="min-w-0 flex-1">
        <p className="text-fg">{message}</p>
        {errorId && !idInMessage ? (
          <p className="mt-1 text-caption text-fg-muted">
            Error ID <code className="font-mono break-all text-fg">{errorId}</code>
          </p>
        ) : null}
      </div>
      {errorId ? (
        <Button variant="ghost" size="sm" icon={<Copy className="size-4" />} onClick={() => void copy()} className="shrink-0">
          Copy error ID
        </Button>
      ) : null}
    </div>
  );
}
