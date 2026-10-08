"use client";

import { Tooltip as BaseTooltip } from "@base-ui/react/tooltip";
import type { ReactElement, ReactNode } from "react";
import { cn } from "../lib/cn";

export type TooltipProps = {
  /** Tooltip text (supplementary — never the only accessible name). */
  content: ReactNode;
  /** The trigger element; it receives the tooltip props via Base UI's `render`. */
  children: ReactElement;
  side?: "top" | "bottom" | "left" | "right" | undefined;
  delay?: number | undefined;
  className?: string | undefined;
};

/** Hover/focus tooltip (Base UI). */
export function Tooltip({ content, children, side = "top", delay = 500, className }: TooltipProps) {
  return (
    <BaseTooltip.Root>
      <BaseTooltip.Trigger delay={delay} render={children} />
      <BaseTooltip.Portal>
        <BaseTooltip.Positioner side={side} sideOffset={6} className="z-50">
          <BaseTooltip.Popup
            className={cn(
              "max-w-72 rounded-sm bg-fg px-2.5 py-1.5 text-caption text-fg-inverse shadow-md",
              "origin-(--transform-origin) transition-[opacity,transform] duration-150",
              "data-starting-style:scale-95 data-starting-style:opacity-0 data-ending-style:opacity-0",
              className,
            )}
          >
            {content}
          </BaseTooltip.Popup>
        </BaseTooltip.Positioner>
      </BaseTooltip.Portal>
    </BaseTooltip.Root>
  );
}

/** Shares hover delays between tooltips (wrap the app once). */
export const TooltipProvider = BaseTooltip.Provider;
