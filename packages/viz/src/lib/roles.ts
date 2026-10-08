/** Human labels for product roles (`10-auth.md`); unknown roles are spelled out from their slug. */
const ROLE_LABELS: Record<string, string> = {
  planner: "Planner",
  approver: "Approver",
  compliance: "Compliance",
  "tenant-admin": "Tenant admin",
  "platform-admin": "Platform admin",
  "demo-presenter": "Demo presenter",
  "merch-planner": "Merchandise planner",
  "ops-director": "Operations director",
};

export function roleLabel(role: string): string {
  return (Object.hasOwn(ROLE_LABELS, role) ? ROLE_LABELS[role] : undefined) ?? role.replace(/[-_]/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** "Approver and Compliance" / "Approver, Compliance and Planner". */
export function roleList(roles: readonly string[]): string {
  const labels = roles.map(roleLabel);
  if (labels.length <= 1) return labels[0] ?? "";
  return `${labels.slice(0, -1).join(", ")} and ${labels.at(-1)}`;
}
