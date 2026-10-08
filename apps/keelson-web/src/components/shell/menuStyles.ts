/** Shared Tailwind classes for Base UI menus in the app header (tokens only). */
export const menuPopup =
  "z-50 min-w-60 max-w-[calc(100vw-1.5rem)] rounded-md border border-border bg-surface p-1 text-ui text-fg shadow-lg outline-none " +
  "origin-[var(--transform-origin)] transition-[opacity,scale] duration-150 ease-standard data-starting-style:scale-95 data-starting-style:opacity-0 data-ending-style:opacity-0";

export const menuItem =
  "flex w-full cursor-default items-center gap-2 rounded-sm px-3 py-2 text-left outline-none select-none " +
  "data-highlighted:bg-surface-2 data-disabled:opacity-55";

export const menuLabel = "px-3 pt-2 pb-1 text-caption font-medium text-fg-muted";

export const menuSeparator = "my-1 h-px bg-border";
