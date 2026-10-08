"use client";

import { ErrorState } from "@/components/common/ErrorState";

/** Errors in the app layout itself (e.g. `/api/me` unreachable). */
export default function RootError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="flex min-h-dvh flex-col">
      <ErrorState digest={error.digest} onRetry={reset} />
    </main>
  );
}
