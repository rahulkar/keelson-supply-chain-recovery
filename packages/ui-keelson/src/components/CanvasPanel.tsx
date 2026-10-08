"use client";

import { springs } from "@keelson/tokens";
import { X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useLayoutEffect, useRef, type ReactNode, type RefObject } from "react";
import { cn } from "../lib/cn";
import { CANVAS_SIDE_QUERY, useMediaQuery } from "../lib/hooks";
import { IconButton } from "./Button";
import { Sheet } from "./Dialog";
import { Tabs, type TabItem } from "./Tabs";

export type CanvasTab = TabItem;

export type CanvasPanelProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Usually Result / Evidence / Trace. */
  tabs: CanvasTab[];
  activeTab: string;
  onTabChange: (id: string) => void;
  /** Content of the active tab. */
  children?: ReactNode | undefined;
  /** Accessible name of the panel (default "Canvas"). */
  title?: string | undefined;
  /**
   * `auto` (default): 55 % side panel at ≥ 1280 px, bottom sheet below.
   * `side` / `sheet` force a layout (tests, embedding).
   */
  mode?: "auto" | "side" | "sheet" | undefined;
  /**
   * Where focus goes on close when the element that opened the panel is gone (or unknown, e.g.
   * the panel opened itself, or Safari did not focus the clicked button). Usually the canvas toggle.
   */
  returnFocusRef?: RefObject<HTMLElement | null> | undefined;
  className?: string | undefined;
};

function focusable(el: HTMLElement | null | undefined): el is HTMLElement {
  if (!el || !el.isConnected || el.closest("[inert]")) return false;
  return !(el as HTMLButtonElement).disabled;
}

/**
 * The Canvas: where complexity lives (maps, graphs, plan diff, evidence). A non-modal
 * complementary region beside the thread on wide screens (place it as a flex sibling of the
 * thread column), a modal bottom sheet on narrow screens.
 */
export function CanvasPanel({
  open,
  onOpenChange,
  tabs,
  activeTab,
  onTabChange,
  children,
  title = "Canvas",
  mode = "auto",
  returnFocusRef,
  className,
}: CanvasPanelProps) {
  const wide = useMediaQuery(CANVAS_SIDE_QUERY, true);
  const side = mode === "side" || (mode === "auto" && wide);

  // Focus returns to the opener on close (WCAG 2.4.3). The opener is whatever had focus when
  // `open` became true (outside the panel); `returnFocusRef` is the fallback.
  const panelRef = useRef<HTMLElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);
  const returnTarget = (): HTMLElement | null => {
    if (focusable(opener.current)) return opener.current;
    const fallback = returnFocusRef?.current;
    return focusable(fallback) ? fallback : null;
  };
  useLayoutEffect(() => {
    if (open) {
      if (!wasOpen.current) {
        const active = document.activeElement;
        opener.current =
          active instanceof HTMLElement && active !== document.body && !panelRef.current?.contains(active) ? active : null;
      }
      wasOpen.current = true;
      return;
    }
    if (!wasOpen.current) return;
    wasOpen.current = false;
    // The modal sheet hands focus back itself (Base UI `finalFocus`, below).
    if (!side) return;
    // Only when focus is in the closing panel (or was dropped with it): a close triggered from
    // elsewhere (the toggle, a keyboard shortcut) leaves focus where the person put it.
    const active = document.activeElement;
    const lost = !active || active === document.body || !!panelRef.current?.contains(active);
    if (lost) returnTarget()?.focus({ preventScroll: true });
  }, [open, side]); // returnTarget only reads refs

  const body = (
    <Tabs
      tabs={tabs}
      value={activeTab}
      onValueChange={onTabChange}
      label={`${title} sections`}
      className="h-full"
      headerClassName={side ? "px-4 pt-3 pb-2" : "pb-3"}
      panelClassName={cn("overflow-y-auto", side && "px-4 pb-6")}
      actions={
        side ? (
          <IconButton label={`Close ${title.toLowerCase()}`} size="sm" icon={<X className="size-4" />} onClick={() => onOpenChange(false)} />
        ) : undefined
      }
    >
      {children}
    </Tabs>
  );

  if (!side) {
    return (
      <Sheet
        open={open}
        onOpenChange={onOpenChange}
        side="bottom"
        title={title}
        hideTitle
        className={className}
        bodyClassName="flex flex-col"
        finalFocus={() => returnTarget() ?? true}
      >
        {body}
      </Sheet>
    );
  }

  return (
    <AnimatePresence initial={false}>
      {open ? (
        <motion.aside
          ref={panelRef}
          key="keelson-canvas"
          aria-label={title}
          initial={{ opacity: 0, x: 32 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 32 }}
          transition={springs.canvasReveal}
          className={cn(
            "flex h-full min-h-0 shrink-0 grow-0 basis-[55%] flex-col overflow-hidden border-l border-border bg-surface",
            className,
          )}
          data-mode="side"
        >
          {body}
        </motion.aside>
      ) : null}
    </AnimatePresence>
  );
}
