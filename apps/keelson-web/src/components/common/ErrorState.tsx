"use client";

import { Button, EmptyState } from "@keelson/ui";
import { CloudOff } from "lucide-react";

/**
 * Shown by the route error boundaries. Server errors reach the browser without their message in
 * production (only a digest), so the copy stays generic: the usual cause is engine-api starting.
 */
export function ErrorState({ digest, onRetry }: { digest?: string | undefined; onRetry: () => void }) {
  return (
    <div className="mx-auto flex w-full max-w-chat flex-1 flex-col justify-center px-4 py-12">
      <EmptyState
        icon={<CloudOff />}
        title="This page didn't load"
        headingLevel={1}
        description={`The engine may still be starting. Try again in a moment.${digest ? ` (Reference ${digest})` : ""}`}
        action={
          <Button variant="primary" onClick={onRetry}>
            Try again
          </Button>
        }
      />
    </div>
  );
}
