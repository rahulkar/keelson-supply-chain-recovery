"use client";

import { Skeleton } from "@keelson/ui";
import { lazy, Suspense, type ComponentType } from "react";
import type { BlockComponentProps } from "../types";
import { useIsClient } from "./theme";

/**
 * A block component loaded on demand and rendered on the client only (canvas visuals pull in
 * MapLibre / deck.gl / React Flow / ECharts). Server render and hydration show a skeleton.
 * Next.js apps may override these with `next/dynamic(…, { ssr: false })` via `registerBlock`.
 */
export function lazyBlock<P, D>(
  load: () => Promise<{ default: ComponentType<BlockComponentProps<P, D>> }>,
  fallbackTitle: string,
): ComponentType<BlockComponentProps<P, D>> {
  const Lazy = lazy(load);
  function LazyBlock(props: BlockComponentProps<P, D>) {
    const isClient = useIsClient();
    const skeleton = <BlockSkeleton title={props.envelope.title || fallbackTitle} />;
    if (!isClient) return skeleton;
    return (
      <Suspense fallback={skeleton}>
        <Lazy {...props} />
      </Suspense>
    );
  }
  LazyBlock.displayName = `Lazy(${fallbackTitle})`;
  return LazyBlock;
}

/** Placeholder while a canvas visual (or its data) loads. */
export function BlockSkeleton({ title, className }: { title: string; className?: string | undefined }) {
  return <Skeleton className={className ?? "h-28 w-full rounded-lg"} label={`Loading ${title}`} />;
}
