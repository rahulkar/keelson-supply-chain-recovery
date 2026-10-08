/** Connector tool classification: labels, guarded reclassification and review state (70 §3). */
import { hasAnyRole } from "../roles";
import {
  CONTENT_TRUST,
  EGRESS,
  IDEMPOTENCY,
  TOOL_ACCESS,
  TOOL_STATUS,
  type Classification,
  type ConnectorTool,
} from "./types";

export const ACCESS_LABELS: Record<string, string> = { read: "Read", write: "Write", destructive: "Destructive" };
export const TRUST_LABELS: Record<string, string> = { trusted_data: "Trusted data", untrusted_text: "Untrusted text" };
export const EGRESS_LABELS: Record<string, string> = { internal: "Internal", external: "External" };
export const IDEMPOTENCY_LABELS: Record<string, string> = { enforced: "Enforced", best_effort: "Best effort", none: "None" };
export const STATUS_LABELS: Record<string, string> = { needs_review: "Needs review", approved: "Approved", disabled: "Disabled", removed: "Removed" };

/**
 * A tool the server no longer offers (engine status `removed`, still listed by `GET …/tools` for
 * history). It can't be reclassified, approved or disabled (the engine answers 409 `tool_removed`).
 */
export function isRemovedTool(tool: Pick<ConnectorTool, "status">): boolean {
  return tool.status === "removed";
}

export const CLASSIFICATION_FIELDS = [
  { key: "access", label: "Access", values: TOOL_ACCESS, labels: ACCESS_LABELS },
  { key: "content_trust", label: "Content", values: CONTENT_TRUST, labels: TRUST_LABELS },
  { key: "egress", label: "Egress", values: EGRESS, labels: EGRESS_LABELS },
  { key: "idempotency", label: "Idempotency", values: IDEMPOTENCY, labels: IDEMPOTENCY_LABELS },
] as const;

export type ClassificationKey = (typeof CLASSIFICATION_FIELDS)[number]["key"];

export const STATUS_VALUES = TOOL_STATUS;

export function label(map: Record<string, string>, value: string | null | undefined): string {
  if (!value) return "—";
  return (Object.hasOwn(map, value) ? map[value] : undefined) ?? value.replace(/_/g, " ");
}

type Guardable = Pick<ConnectorTool, "access" | "content_trust"> & { suggested?: Classification | null | undefined };

/** The server's suggestion, or the stored classification when there is none (as the engine does). */
function suggestedOf(tool: Guardable): { access: string | undefined; content_trust: string | undefined } {
  return {
    access: tool.suggested?.access ?? tool.access,
    content_trust: tool.suggested?.content_trust ?? tool.content_trust,
  };
}

/**
 * Reclassifying a write/destructive tool as `read` needs platform-admin (70 §3). Like the engine,
 * a tool counts as write when either its stored classification or the server's suggestion (from
 * the MCP annotations) says so.
 */
export function readDowngradeLocked(current: Guardable, user: { roles: readonly string[] }): boolean {
  if (hasAnyRole(user, ["platform-admin"])) return false;
  return current.access !== "read" || suggestedOf(current).access !== "read";
}

/** Marking untrusted free text as trusted data needs platform-admin (engine `update_tool`). */
export function trustUpgradeLocked(current: Guardable, user: { roles: readonly string[] }): boolean {
  if (hasAnyRole(user, ["platform-admin"])) return false;
  return current.content_trust !== "trusted_data" || suggestedOf(current).content_trust !== "trusted_data";
}

/**
 * Why approving `tool` as it is classified now needs a second admin, or `null`. Approving a tool
 * that is stored as read / trusted data while the server suggested write / untrusted text would
 * make it reachable by agents on one tenant admin's word alone: M7 turns it into a change request
 * (before M7 the engine answered 403).
 */
export function approveBlockedReason(tool: Guardable, user: { roles: readonly string[] }): string | null {
  if (hasAnyRole(user, ["platform-admin"])) return null;
  const s = suggestedOf(tool);
  if (tool.access === "read" && s.access !== "read") return `Suggested as ${label(ACCESS_LABELS, s.access).toLowerCase()}: approving it as read needs a second admin.`;
  if (tool.content_trust === "trusted_data" && s.content_trust !== "trusted_data") {
    return "Suggested as untrusted text: approving it as trusted data needs a second admin.";
  }
  return null;
}

/** Fields where the stored classification differs from the server's suggestion. */
export function differsFromSuggestion(tool: ConnectorTool): ClassificationKey[] {
  const s: Classification = tool.suggested ?? {};
  return CLASSIFICATION_FIELDS.map((f) => f.key).filter((k) => s[k] !== undefined && s[k] !== tool[k]);
}

export function statusTone(status: string): "warning" | "success" | "neutral" {
  return status === "needs_review" ? "warning" : status === "approved" ? "success" : "neutral";
}

export function connectorStatusTone(status: string): "success" | "info" | "neutral" | "warning" {
  switch (status) {
    case "active":
      return "success";
    case "tested":
      return "info";
    case "disabled":
      return "neutral";
    default:
      return "warning";
  }
}

export const CONNECTOR_STATUS_LABELS: Record<string, string> = { draft: "Draft", tested: "Tested", active: "Active", disabled: "Disabled" };

export const TIER_INFO: Record<string, string> = {
  A: "Tier A: plain tools",
  B: "Tier B: snapshot export",
  C: "Tier C: change feed",
  W: "Tier W: change sets (write)",
};

export const AUTH_KIND_LABELS: Record<string, string> = {
  none: "No authentication",
  static_header: "API key header",
  oauth_client_credentials: "OAuth client credentials",
  oauth_platform_merxa: "Merxa platform (managed)",
};
