/**
 * Users of a tenant (`docs/contracts/110` §2): list / invite / roles / disable through the engine's
 * Keycloak admin routes. The rules are a port of contracts-py `admin.grantable_roles`,
 * `user_change_violations` and `user_change_guard_reasons` (the engine enforces them; the UI only
 * hides what would be refused and says when a change needs a second admin).
 */
import {
  PRODUCT_ROLES,
  adminUserCreateSchema,
  adminUserCreatedSchema,
  adminUserSchema,
  type AdminUser,
  type AdminUserCreated,
  type ProductRole,
} from "@keelson/contracts";
import { z } from "zod";

export type User = AdminUser;
export type UserCreated = AdminUserCreated;

export const userSchema = adminUserSchema.strip();
export const userCreatedSchema = z.object({ user: userSchema, setup: adminUserCreatedSchema.shape.setup.strip() }).strip();

/** Valid users of a list (`{items}` or a bare array); malformed entries are skipped. */
export const userListSchema = z
  .union([z.array(z.unknown()), z.object({ items: z.array(z.unknown()) }).loose()])
  .transform((v) => {
    const items = Array.isArray(v) ? v : v.items;
    const out: User[] = [];
    for (const item of items) {
      const parsed = userSchema.safeParse(item);
      if (parsed.success) out.push(parsed.data);
    }
    return out;
  });

/** `DELETE` (disable) / `PATCH` may answer the updated user, `{user}` or nothing (204). */
export const userAnswerSchema = z
  .union([z.object({ user: userSchema }).loose().transform((v) => v.user), userSchema, z.object({}).loose().transform(() => null)])
  .nullable();

export const ADMIN_ROLES: readonly ProductRole[] = ["tenant-admin", "platform-admin"];

export const ROLE_DESCRIPTIONS: Record<ProductRole, string> = {
  planner: "Starts cases and works on plans",
  approver: "Approves changes before they are applied",
  compliance: "Reads cases and co-approves where a policy asks for it",
  "tenant-admin": "Manages this workspace's settings and users",
  "platform-admin": "Manages every workspace on the platform",
  "demo-presenter": "Runs demo scenarios and resets",
};

type Actor = { roles: readonly string[]; tenants: readonly string[] };

/** Product roles the actor may grant or revoke (none for non-admins). */
export function grantableRoles(actorRoles: readonly string[]): ProductRole[] {
  if (actorRoles.includes("platform-admin")) return [...PRODUCT_ROLES];
  if (actorRoles.includes("tenant-admin")) return PRODUCT_ROLES.filter((r) => r !== "platform-admin");
  return [];
}

/**
 * Why the actor may not make this change (empty = allowed). `target` = the user as it is now
 * (`null` for a new user, created in `activeTenant`); `newRoles` / `enabled` = what changes.
 */
export function userChangeProblems(
  actor: Actor,
  target: Pick<User, "roles" | "tenants"> | null,
  change: { newRoles?: readonly string[] | undefined; enabled?: boolean | undefined },
  activeTenant: string | null,
): string[] {
  const problems: string[] = [];
  const allowed = new Set<string>(grantableRoles(actor.roles));
  if (allowed.size === 0) return ["Only tenant admins and platform admins manage users."];
  const platform = actor.roles.includes("platform-admin");
  const targetRoles = target?.roles ?? [];
  const targetTenants = target ? target.tenants : activeTenant ? [activeTenant] : [];
  if (!platform) {
    if (targetTenants.length === 0) problems.push("This user belongs to none of your workspaces: only a platform admin can change it.");
    else {
      const outside = targetTenants.filter((t) => !actor.tenants.includes(t)).sort();
      if (outside.length) problems.push(`This user also belongs to ${outside.join(", ")}: only a platform admin can change it.`);
    }
  }
  if (targetRoles.includes("platform-admin") && !platform && (change.newRoles !== undefined || change.enabled !== undefined)) {
    problems.push("Only a platform admin can change a platform admin's account.");
  }
  if (change.newRoles !== undefined) {
    const product = new Set<string>(PRODUCT_ROLES);
    const unknown = change.newRoles.filter((r) => !product.has(r));
    if (unknown.length) problems.push(`Not product roles: ${unknown.join(", ")}.`);
    const changed = new Set([...change.newRoles.filter((r) => !targetRoles.includes(r as ProductRole)), ...targetRoles.filter((r) => !change.newRoles!.includes(r))]);
    const forbidden = [...changed].filter((r) => !allowed.has(r) && product.has(r)).sort();
    if (forbidden.length) problems.push(`You can't grant or remove ${forbidden.map(roleLabelOf).join(", ")}.`);
  }
  return problems;
}

/**
 * Admin roles this change grants for a tenant admin (a guarded `user_roles` change request: a
 * second admin must approve). Empty for a platform admin, whose user changes apply directly.
 */
export function guardedRoleGrants(
  actorRoles: readonly string[],
  target: Pick<User, "roles" | "enabled"> | null,
  change: { newRoles?: readonly string[] | undefined; enabled?: boolean | undefined },
): string[] {
  if (actorRoles.includes("platform-admin")) return [];
  const before = (target?.roles ?? []).filter((r) => ADMIN_ROLES.includes(r));
  const after = change.newRoles !== undefined ? change.newRoles.filter((r) => (ADMIN_ROLES as readonly string[]).includes(r)) : before;
  const out = after.filter((r) => !before.includes(r as ProductRole)).sort();
  if (change.enabled === true && target?.enabled === false) for (const r of after) if (before.includes(r as ProductRole) && !out.includes(r)) out.push(r);
  return out;
}

const LABELS: Record<string, string> = {
  planner: "Planner",
  approver: "Approver",
  compliance: "Compliance",
  "tenant-admin": "Tenant admin",
  "platform-admin": "Platform admin",
  "demo-presenter": "Demo presenter",
};

export function roleLabelOf(role: string): string {
  return LABELS[role] ?? role;
}

export type InviteDraft = { username: string; email: string; name: string; roles: ProductRole[] };

/** Field errors of the invite form (the contract's patterns; the engine validates again). */
export function inviteProblems(d: InviteDraft): Partial<Record<keyof InviteDraft, string>> {
  const shape = adminUserCreateSchema.shape;
  const out: Partial<Record<keyof InviteDraft, string>> = {};
  if (!shape.username.safeParse(d.username).success) out.username = "2–63 lower-case letters, digits, dots, hyphens or underscores; starts with a letter or digit.";
  if (!shape.email.safeParse(d.email.trim()).success) out.email = "Enter a valid email address.";
  if (!shape.name.safeParse(d.name.trim()).success) out.name = d.name.trim() ? "At most 120 characters." : "Enter the person's name.";
  if (d.roles.length === 0) out.roles = "Choose at least one role.";
  return out;
}
