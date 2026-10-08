"use client";

/**
 * Shared alert vocabulary (M6, `docs/contracts/100` §8): the severity chip, the kind icon and the
 * DEMO badge, used by the `alert_origin@1` card and by the app's Alerts inbox so both read the same.
 * Severity is always spelled out (never colour alone); icons are decorative.
 */
import type { AlertSeverity, SignalKind } from "@keelson/contracts";
import { Badge, type BadgeTone } from "@keelson/ui";
import {
  Bell,
  CloudLightning,
  Factory,
  FlaskConical,
  Globe,
  Mountain,
  Scale,
  ShieldAlert,
  Ship,
  TrendingUp,
  Users,
  type LucideIcon,
} from "lucide-react";

export const SEVERITY_META: Record<AlertSeverity, { label: string; tone: BadgeTone; rank: number }> = {
  low: { label: "Low", tone: "neutral", rank: 0 },
  medium: { label: "Medium", tone: "info", rank: 1 },
  high: { label: "High", tone: "warning", rank: 2 },
  critical: { label: "Critical", tone: "danger", rank: 3 },
};

export const KIND_META: Record<SignalKind, { label: string; icon: LucideIcon }> = {
  natural_hazard: { label: "Natural hazard", icon: Mountain },
  weather: { label: "Weather", icon: CloudLightning },
  geopolitical: { label: "Geopolitical", icon: Globe },
  labor: { label: "Labour action", icon: Users },
  logistics: { label: "Logistics", icon: Ship },
  regulatory: { label: "Regulatory", icon: Scale },
  cyber: { label: "Cyber", icon: ShieldAlert },
  supplier: { label: "Supplier", icon: Factory },
  demand: { label: "Demand", icon: TrendingUp },
  hypothetical: { label: "Hypothetical", icon: FlaskConical },
};

/** Severity order for sorting (critical first when sorted descending). */
export function severityRank(severity: string): number {
  return Object.hasOwn(SEVERITY_META, severity) ? SEVERITY_META[severity as AlertSeverity].rank : -1;
}

/** "Natural hazard"; an unknown kind is humanised ("port_issue" → "Port issue"). */
export function kindLabel(kind: string): string {
  if (Object.hasOwn(KIND_META, kind)) return KIND_META[kind as SignalKind].label;
  return humanize(kind);
}

/** "tropical_cyclone" → "Tropical cyclone". */
export function humanize(value: string): string {
  const s = value.replace(/[_.-]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : value;
}

/** Severity chip: "High" with a dot, toned (the word carries the meaning). */
export function SeverityBadge({ severity, size = "md" }: { severity: AlertSeverity; size?: "sm" | "md" | undefined }) {
  const meta = SEVERITY_META[severity] ?? SEVERITY_META.low;
  return (
    <Badge tone={meta.tone} size={size} dot title={`Severity: ${meta.label}`}>
      {meta.label}
    </Badge>
  );
}

/**
 * The kind's icon in a soft circle. Decorative by default (the kind is named next to it);
 * pass `label` when the icon stands alone.
 */
export function KindIcon({ kind, label, className }: { kind: string; label?: string | undefined; className?: string | undefined }) {
  const Icon = Object.hasOwn(KIND_META, kind) ? KIND_META[kind as SignalKind].icon : Bell;
  return (
    <span
      className={`inline-flex size-8 shrink-0 items-center justify-center rounded-pill bg-surface-2 text-fg-muted ${className ?? ""}`}
      {...(label ? { role: "img", "aria-label": label } : { "aria-hidden": true })}
    >
      <Icon className="size-4" aria-hidden="true" />
    </span>
  );
}

/** "DEMO" — the alert comes from a replayed scenario pack (`simulated: true`). */
export function DemoBadge({ size = "sm" }: { size?: "sm" | "md" | undefined }) {
  return (
    <Badge tone="accent" variant="outline" size={size} title="Simulated: replayed from a demo scenario pack">
      DEMO
    </Badge>
  );
}
