"use client";

import { Button, Card, EmptyState, Skeleton, cn } from "@keelson/ui";
import { AlertTriangle, RotateCw } from "lucide-react";
import type { ReactNode } from "react";
import type { Resource } from "@/lib/admin/useAdmin";

/** Page title row: h1, one-line description, actions on the right. */
export function AdminPageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-start gap-x-4 gap-y-2">
      <div className="min-w-0 flex-1 basis-64">
        <h1 className="text-heading font-semibold text-fg">{title}</h1>
        {description ? <p className="mt-0.5 text-ui text-fg-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}

/** A titled admin section (h2) in a compact card. */
export function AdminSection({
  title,
  description,
  actions,
  children,
  className,
  id,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  const headingId = `${id ?? title.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-h`;
  return (
    <Card padding="sm" labelledBy={headingId} className={cn("flex flex-col gap-3", className)}>
      <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
        <div className="min-w-0 flex-1 basis-56">
          <h2 id={headingId} className="text-body font-semibold text-fg">
            {title}
          </h2>
          {description ? <p className="text-caption text-fg-muted">{description}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </Card>
  );
}

/** Loading skeleton / error with retry / content for a `Resource`. */
export function ResourceView<T>({
  resource,
  label,
  children,
  rows = 3,
}: {
  resource: Resource<T>;
  /** "models" → "Loading models", "Couldn't load models". */
  label: string;
  children: (data: T) => ReactNode;
  rows?: number;
}) {
  if (resource.data !== undefined) {
    return (
      <>
        {resource.error ? <InlineError message={resource.error} onRetry={resource.reload} /> : null}
        {children(resource.data)}
      </>
    );
  }
  if (resource.error) {
    return (
      <EmptyState
        icon={<AlertTriangle />}
        title={`Couldn't load ${label}`}
        description={resource.error}
        action={
          <Button size="sm" icon={<RotateCw className="size-4" />} onClick={resource.reload}>
            Try again
          </Button>
        }
      />
    );
  }
  return (
    <div role="status" aria-label={`Loading ${label}`} aria-busy="true" className="flex flex-col gap-2">
      {Array.from({ length: rows }, (_, i) => (
        <Skeleton key={i} className="h-8 w-full" />
      ))}
    </div>
  );
}

export function InlineError({ message, onRetry }: { message: string; onRetry?: (() => void) | undefined }) {
  return (
    <div role="alert" className="flex flex-wrap items-center gap-2 rounded-sm border border-danger/40 bg-danger-soft px-3 py-2 text-ui text-danger">
      <AlertTriangle aria-hidden="true" className="size-4 shrink-0" />
      <span className="min-w-0 flex-1">{message}</span>
      {onRetry ? (
        <Button size="sm" variant="ghost" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

/** Key/value list for summaries. */
export function Facts({ items, className }: { items: [string, ReactNode][]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-ui", className)}>
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-fg-muted">{k}</dt>
          <dd className="min-w-0 break-words text-fg">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

/** "Requires platform admin" lock note (text, not colour). */
export function PlatformOnlyNote({ children = "Requires platform admin" }: { children?: ReactNode }) {
  return <span className="text-caption text-fg-subtle">{children}</span>;
}
