"use client";

import { Avatar as BaseAvatar } from "@base-ui/react/avatar";
import type { ReactNode } from "react";
import { cn } from "../lib/cn";
import { hashIndex, initials } from "../lib/format";

/**
 * Background + initials colour per tone. Contrast ≥ 4.5:1 in both themes is asserted in
 * @keelson/tokens tests ("avatar … on chart-N"): light chart-3/5 are light enough to need dark
 * text; in dark mode every chart colour is light and `fg-inverse` is near-black.
 */
const TONES = [
  "bg-chart-1 text-fg-inverse",
  "bg-chart-2 text-fg-inverse",
  "bg-chart-3 text-fg dark:text-fg-inverse",
  "bg-chart-4 text-fg-inverse",
  "bg-chart-5 text-fg dark:text-fg-inverse",
  "bg-chart-6 text-fg-inverse",
] as const;

const SIZES = {
  xs: "size-5 text-[0.5625rem]",
  sm: "size-6 text-[0.625rem]",
  md: "size-8 text-caption",
  lg: "size-10 text-ui",
} as const;

export type AvatarSize = keyof typeof SIZES;

export type AvatarProps = {
  name: string;
  src?: string | null | undefined;
  size?: AvatarSize | undefined;
  /** Custom glyph instead of initials (e.g. an agent icon). */
  icon?: ReactNode | undefined;
  /** Decorative avatars (next to a visible name) are hidden from assistive tech. */
  decorative?: boolean | undefined;
  className?: string | undefined;
};

/** Round avatar with image + initials fallback (Base UI Avatar). */
export function Avatar({ name, src, size = "md", icon, decorative = false, className }: AvatarProps) {
  const tone = TONES[hashIndex(name, TONES.length)];
  return (
    <BaseAvatar.Root
      aria-hidden={decorative || undefined}
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : name}
      className={cn(
        "inline-flex shrink-0 items-center justify-center overflow-hidden rounded-pill font-semibold select-none",
        SIZES[size],
        tone,
        className,
      )}
    >
      {src ? <BaseAvatar.Image src={src} alt="" className="size-full object-cover" /> : null}
      <BaseAvatar.Fallback className="inline-flex size-full items-center justify-center [&>svg]:size-[60%]">
        {icon ?? initials(name)}
      </BaseAvatar.Fallback>
    </BaseAvatar.Root>
  );
}

export type AvatarPerson = { id: string; name: string; src?: string | null | undefined };
export type AvatarAgent = { id: string; name: string; state?: "running" | "done" | "error" | undefined; icon?: ReactNode | undefined };

export type AvatarStackProps = (
  | { people: AvatarPerson[]; agents?: never }
  | { agents: AvatarAgent[]; people?: never }
) & {
  /** Max avatars before "+N" (default 3). */
  max?: number | undefined;
  size?: AvatarSize | undefined;
  /** Accessible group label (default "People" / "Agents consulted"). */
  label?: string | undefined;
  className?: string | undefined;
};

/** Overlapping avatars with "+N" overflow; announced as one labelled group. */
export function AvatarStack(props: AvatarStackProps) {
  const { max = 3, size = "sm", className } = props;
  const items: (AvatarPerson | AvatarAgent)[] = props.people ?? props.agents ?? [];
  const isAgents = props.agents !== undefined;
  const shown = items.slice(0, max);
  const overflow = items.length - shown.length;
  const label =
    props.label ?? `${isAgents ? "Agents consulted" : "People"}: ${items.map((i) => i.name).join(", ") || "none"}`;
  return (
    <div role="group" aria-label={label} className={cn("flex items-center", className)}>
      {shown.map((item, idx) => {
        const agent = isAgents ? (item as AvatarAgent) : null;
        return (
          <span
            key={item.id}
            className={cn("relative rounded-pill ring-2 ring-surface", idx > 0 && "-ml-1.5")}
            style={{ zIndex: shown.length - idx }}
            data-state={agent?.state}
          >
            <Avatar
              name={item.name}
              src={"src" in item ? item.src : undefined}
              icon={agent?.icon}
              size={size}
              decorative
            />
            {agent?.state === "running" ? (
              <span
                aria-hidden="true"
                className="absolute -right-0.5 -bottom-0.5 size-2 animate-pulse-soft rounded-pill bg-accent ring-2 ring-surface"
              />
            ) : null}
          </span>
        );
      })}
      {overflow > 0 ? (
        <span
          aria-hidden="true"
          title={items.slice(max).map((i) => i.name).join(", ")}
          data-overflow={overflow}
          className={cn(
            "-ml-1.5 inline-flex items-center justify-center rounded-pill bg-surface-3 font-medium text-fg-muted ring-2 ring-surface",
            SIZES[size],
          )}
        >
          +{overflow}
        </span>
      ) : null}
    </div>
  );
}
