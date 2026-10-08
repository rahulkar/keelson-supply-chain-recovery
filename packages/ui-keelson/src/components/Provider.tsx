"use client";

import { MotionConfig } from "motion/react";
import type { ReactNode } from "react";
import { Toaster } from "./Toast";
import { TooltipProvider } from "./Tooltip";

export type UiProviderProps = {
  children: ReactNode;
  /** Mount the toast viewport (default true). */
  toaster?: boolean | undefined;
};

/**
 * Wrap the app once: Motion honours the OS reduced-motion setting (`reducedMotion="user"`),
 * tooltips share delays, and the toast viewport is mounted.
 */
export function UiProvider({ children, toaster = true }: UiProviderProps) {
  return (
    <MotionConfig reducedMotion="user">
      <TooltipProvider delay={500}>
        {children}
        {toaster ? <Toaster /> : null}
      </TooltipProvider>
    </MotionConfig>
  );
}
