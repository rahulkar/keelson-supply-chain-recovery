import { IconAlertTriangle, IconLock } from "@tabler/icons-react";

import { ApiError } from "@/lib/http";

/** Inline rendering of a failed API load (RFC 9457 problem from merxa-api). */
export function ProblemPanel({ error, what }: { error: ApiError; what: string }) {
  const forbidden = error.status === 403;
  return (
    <div className="mx-banner mx-banner--danger" role="alert">
      <span className="mx-banner__icon" aria-hidden="true">
        {forbidden ? <IconLock size={16} /> : <IconAlertTriangle size={16} />}
      </span>
      <div className="mx-banner__body">
        <div className="mx-banner__title">
          {forbidden ? `You do not have access to ${what}` : `Could not load ${what}`}
        </div>
        <div>
          {error.problem.detail || error.problem.title || error.message}
          {error.code && (
            <>
              {" "}
              <code>({error.code})</code>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
