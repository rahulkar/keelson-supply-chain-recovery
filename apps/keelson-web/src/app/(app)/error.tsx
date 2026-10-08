"use client";

import { ErrorState } from "@/components/common/ErrorState";

/** Errors inside a product page (below the app chrome). */
export default function AppError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return <ErrorState digest={error.digest} onRetry={reset} />;
}
