"use client";

import { Tabs as BaseTabs } from "@base-ui/react/tabs";
import type { ReactNode } from "react";
import { cn } from "../lib/cn";

export type TabItem = {
  id: string;
  label: ReactNode;
  /** Small count/status next to the label (e.g. evidence count). */
  badge?: ReactNode | undefined;
  disabled?: boolean | undefined;
};

export type TabsProps = {
  tabs: TabItem[];
  value: string;
  onValueChange: (id: string) => void;
  /** Panel content for the active tab (render-prop receives the active id). */
  children?: (ReactNode | ((active: string) => ReactNode)) | undefined;
  /** Accessible name for the tab list. */
  label?: string | undefined;
  /** Extra controls on the right of the tab list (e.g. a close button). */
  actions?: ReactNode | undefined;
  className?: string | undefined;
  headerClassName?: string | undefined;
  listClassName?: string | undefined;
  panelClassName?: string | undefined;
};

/** Controlled tabs (Base UI): keyboard arrows move focus, Enter/Space activate. */
export function Tabs({
  tabs,
  value,
  onValueChange,
  children,
  label,
  actions,
  className,
  headerClassName,
  listClassName,
  panelClassName,
}: TabsProps) {
  return (
    <BaseTabs.Root
      value={value}
      onValueChange={(next) => {
        if (typeof next === "string" && next !== value) onValueChange(next);
      }}
      className={cn("flex min-h-0 flex-col", className)}
    >
      {actions ? (
        <div className={cn("flex items-center gap-2", headerClassName)}>
          <TabList tabs={tabs} label={label} className={listClassName} />
          <div className="ml-auto flex items-center gap-1">{actions}</div>
        </div>
      ) : (
        <TabList tabs={tabs} label={label} className={cn(headerClassName, listClassName)} />
      )}
      <BaseTabs.Panel value={value} className={cn(
          "min-h-0 flex-1 rounded-sm outline-none focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus",
          panelClassName,
        )}>
        {typeof children === "function" ? children(value) : children}
      </BaseTabs.Panel>
    </BaseTabs.Root>
  );
}

/** The pill tab list on its own (must sit inside a `Tabs` root). */
export function TabList({ tabs, label, className }: { tabs: TabItem[]; label?: string | undefined; className?: string | undefined }) {
  return (
    <BaseTabs.List
      aria-label={label}
      className={cn("relative flex items-center gap-1 rounded-pill bg-surface-2 p-1", className)}
    >
      {tabs.map((tab) => (
        <BaseTabs.Tab
          key={tab.id}
          value={tab.id}
          disabled={tab.disabled}
          className={cn(
            "relative z-[1] inline-flex h-8 items-center gap-1.5 rounded-pill px-3 text-ui font-medium text-fg-muted",
            "transition-colors duration-150 hover:text-fg",
            "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus",
            "data-active:text-fg data-disabled:opacity-50",
          )}
        >
          {tab.label}
          {tab.badge !== undefined ? (
            <span className="rounded-pill bg-surface-3 px-1.5 text-caption text-fg-muted">{tab.badge}</span>
          ) : null}
        </BaseTabs.Tab>
      ))}
      <BaseTabs.Indicator
        className={cn(
          "absolute top-1/2 left-0 z-0 h-8 -translate-y-1/2 rounded-pill bg-surface shadow-sm",
          "w-(--active-tab-width) translate-x-(--active-tab-left) transition-[width,translate] duration-200 ease-standard",
        )}
      />
    </BaseTabs.List>
  );
}
