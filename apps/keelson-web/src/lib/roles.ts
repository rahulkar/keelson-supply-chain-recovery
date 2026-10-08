/** Product roles (realm `keelson`, claim `roles`) and what they unlock in the UI (`10-auth.md`). */
import type { MeUser } from "./api/types";

/** May read cases (engine `READ_ROLES`). */
export const READ_ROLES = ["planner", "approver", "compliance", "tenant-admin"] as const;
/** May start cases, send messages, act on plans (engine `WRITE_ROLES`). */
export const WRITE_ROLES = ["planner", "approver", "tenant-admin"] as const;
/** May open the admin back-office (plan §17). */
export const ADMIN_ROLES = ["tenant-admin", "platform-admin"] as const;

type User = Pick<MeUser, "roles" | "active_tenant">;

export function hasAnyRole(user: { roles: readonly string[] }, roles: readonly string[]): boolean {
  return user.roles.some((r) => roles.includes(r));
}

export function canReadCases(user: User): boolean {
  return !!user.active_tenant && hasAnyRole(user, READ_ROLES);
}

export function canWriteCases(user: User): boolean {
  return !!user.active_tenant && hasAnyRole(user, WRITE_ROLES);
}

export function isAdmin(user: Pick<MeUser, "roles">): boolean {
  return hasAnyRole(user, ADMIN_ROLES);
}

const ROLE_LABELS: Record<string, string> = {
  planner: "Planner",
  approver: "Approver",
  compliance: "Compliance",
  "tenant-admin": "Tenant admin",
  "platform-admin": "Platform admin",
  "demo-presenter": "Demo presenter",
};

/** "Planner · Approver" for the user menu. */
export function roleSummary(roles: readonly string[]): string {
  const known = roles.filter((r) => r in ROLE_LABELS).map((r) => ROLE_LABELS[r] as string);
  return known.length ? known.join(" · ") : "No role";
}
