"use client";

import { IconAlertTriangle } from "@tabler/icons-react";

export default function AppError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <div className="mx-banner mx-banner--danger" role="alert">
      <span className="mx-banner__icon" aria-hidden="true">
        <IconAlertTriangle size={16} />
      </span>
      <div className="mx-banner__body">
        <div className="mx-banner__title">Something went wrong while loading this page</div>
        <div>
          {error.message || "Unexpected error."}
          {error.digest && <span className="mx-muted"> (ref {error.digest})</span>}
        </div>
      </div>
      <div className="mx-banner__actions">
        <button type="button" className="mx-btn mx-btn--sm" onClick={reset}>
          Try again
        </button>
      </div>
    </div>
  );
}
