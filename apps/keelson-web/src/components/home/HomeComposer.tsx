"use client";

import { Composer, toast } from "@keelson/ui";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";
import { describeError } from "@/lib/api/client";
import { useSession } from "@/lib/client/session";
import { uuid } from "@/lib/ids";

export const HOME_SUGGESTIONS = [
  "Our trim supplier says wadding and zips are delayed 7 days — what's the impact?",
  "What if the Suez Canal closes for 3 weeks?",
  "Which stores run short of puffer jackets before Black Friday?",
];

type Pending = { text: string; key: string; caseId: string | null };

/**
 * Start a case: `POST /api/cases` then `POST /api/cases/{id}/messages` (Idempotency-Key), then
 * open the thread. A failed attempt keeps the created case and the key, so trying again neither
 * creates a second case nor sends the message twice.
 */
export function HomeComposer({ disabled = false }: { disabled?: boolean }) {
  const { api } = useSession();
  const router = useRouter();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef<Pending | null>(null);
  /** Synchronous guard: a second submit before `busy` re-renders must not create a second case. */
  const inFlight = useRef(false);

  const start = async (text: string) => {
    if (busy || disabled || inFlight.current) return;
    inFlight.current = true;
    if (!pending.current || pending.current.text !== text) {
      pending.current = { text, key: uuid(), caseId: pending.current?.caseId ?? null };
    }
    const attempt = pending.current;
    setBusy(true);
    try {
      attempt.caseId ??= await api.createCase();
      await api.postMessage(attempt.caseId, text, attempt.key);
      pending.current = null;
      router.push(`/cases/${encodeURIComponent(attempt.caseId)}`);
    } catch (err) {
      inFlight.current = false;
      setBusy(false);
      setValue(text);
      toast.error("Couldn't start the case", { description: describeError(err) });
    }
  };

  return (
    <Composer
      value={value}
      onChange={setValue}
      onSubmit={(text) => void start(text)}
      busy={busy}
      disabled={disabled}
      autoFocus={!disabled}
      placeholder="Describe a disruption or ask a question…"
      label="Describe a disruption or ask a question"
      suggestions={HOME_SUGGESTIONS}
      suggestionBehavior="submit"
    />
  );
}
