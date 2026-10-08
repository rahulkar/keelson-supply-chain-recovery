"use client";

import { PRODUCT_ROLES, type ProductRole } from "@keelson/contracts";
import { Badge, Button, Checkbox, ConfirmDialog, Dialog, Field, Table, TextInput, toast } from "@keelson/ui";
import { Copy, Pencil, Plus, UserX } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { adminError, when } from "@/lib/admin/format";
import {
  ROLE_DESCRIPTIONS,
  grantableRoles,
  guardedRoleGrants,
  inviteProblems,
  roleLabelOf,
  userChangeProblems,
  type InviteDraft,
  type User,
  type UserCreated,
} from "@/lib/admin/users";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { AdminPageHeader, AdminSection, InlineError, ResourceView } from "./common";
import { SecondAdminBanner, announceChangeRequest } from "./SecondAdmin";

function RolePicker({
  legend,
  value,
  onChange,
  grantable,
  error,
}: {
  legend: string;
  value: readonly ProductRole[];
  onChange: (roles: ProductRole[]) => void;
  grantable: readonly ProductRole[];
  error?: string | undefined;
}) {
  return (
    <fieldset className="flex flex-col gap-2">
      <legend className="mb-1 text-ui font-medium text-fg">{legend}</legend>
      <div className="grid gap-2 sm:grid-cols-2">
        {PRODUCT_ROLES.map((r) => {
          const checked = value.includes(r);
          const allowed = grantable.includes(r);
          return (
            <Checkbox
              key={r}
              checked={checked}
              disabled={!allowed}
              onCheckedChange={(on) => onChange(on ? [...value, r] : value.filter((x) => x !== r))}
              label={roleLabelOf(r)}
              description={ROLE_DESCRIPTIONS[r]}
              disabledReason={allowed ? undefined : "Only a platform admin can grant or remove this role."}
            />
          );
        })}
      </div>
      {error ? (
        <p className="text-caption font-medium text-danger" role="alert">
          {error}
        </p>
      ) : null}
    </fieldset>
  );
}

/** One-time setup instructions after an invite: the password is never shown; a link may be. */
function SetupResult({ created, onClose }: { created: UserCreated; onClose: () => void }) {
  const { setup, user } = created;
  return (
    <Dialog
      open
      onOpenChange={(o) => (o ? undefined : onClose())}
      title={`${user.name || user.username} was invited`}
      description="Share these instructions once. The temporary password is never shown here; the person sets their own at first sign-in."
      footer={
        <Button size="sm" variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      <div className="flex flex-col gap-3 text-ui">
        <p>{setup.instructions}</p>
        {setup.link ? (
          <div className="flex flex-col gap-1">
            <span className="text-caption font-medium text-fg-muted">One-time setup link{setup.link_expires_at ? ` (expires ${when(setup.link_expires_at)})` : ""}</span>
            <div className="flex items-center gap-2">
              <code className="min-w-0 flex-1 rounded-sm bg-surface-2 px-2 py-1 font-mono text-caption break-all" data-setup-link="">
                {setup.link}
              </code>
              <Button
                size="sm"
                variant="secondary"
                icon={<Copy className="size-4" />}
                onClick={() => {
                  void navigator.clipboard?.writeText(setup.link ?? "").then(
                    () => toast.success("Link copied"),
                    () => toast.error("Couldn't copy the link"),
                  );
                }}
              >
                Copy
              </Button>
            </div>
          </div>
        ) : null}
        <p className="text-caption text-fg-muted">At first sign-in they must choose a new password.</p>
      </div>
    </Dialog>
  );
}

function InviteDialog({ onOpenChange, onDone }: { onOpenChange: (o: boolean) => void; onDone: (created: UserCreated | null) => void }) {
  const admin = useAdminApi();
  const router = useRouter();
  const { me } = useSession();
  const grantable = grantableRoles(me.user.roles);
  const [d, setD] = useState<InviteDraft>({ username: "", email: "", name: "", roles: ["planner"] });
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const problems = inviteProblems(d);
  const policy = userChangeProblems({ roles: me.user.roles, tenants: me.user.tenants ?? [] }, null, { newRoles: d.roles }, me.user.active_tenant ?? null);
  const guarded = guardedRoleGrants(me.user.roles, null, { newRoles: d.roles });

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (Object.keys(problems).length || policy.length) return;
    setBusy(true);
    setError(null);
    try {
      const out = await admin.createUser({ username: d.username, email: d.email.trim(), name: d.name.trim(), roles: [...d.roles].sort() });
      if (out.kind === "change_request") {
        announceChangeRequest(out.request, router.push);
        onDone(null);
      } else {
        onDone(out.value);
      }
      onOpenChange(false);
    } catch (err) {
      setError(adminError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={onOpenChange} title="Invite a user" description={`They get access to ${me.user.active_tenant ?? "this workspace"} and set their own password at first sign-in.`}>
      <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)} noValidate>
        <Field label="Name" required error={submitted ? problems.name : undefined}>
          <TextInput value={d.name} required maxLength={120} onChange={(e) => setD({ ...d, name: e.target.value })} />
        </Field>
        <Field label="Email" required error={submitted ? problems.email : undefined}>
          <TextInput type="email" value={d.email} required autoComplete="off" onChange={(e) => setD({ ...d, email: e.target.value })} />
        </Field>
        <Field label="Username" required error={submitted ? problems.username : undefined} description="Used to sign in; can't be changed later.">
          <TextInput value={d.username} mono required autoComplete="off" spellCheck={false} onChange={(e) => setD({ ...d, username: e.target.value.toLowerCase() })} />
        </Field>
        <RolePicker legend="Roles" value={d.roles} onChange={(roles) => setD({ ...d, roles })} grantable={grantable} error={submitted ? (problems.roles ?? policy[0]) : undefined} />
        <SecondAdminBanner reasons={guarded.map((r) => `Grants the ${roleLabelOf(r)} role`)} />
        {error ? <InlineError message={error} /> : null}
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" type="submit" variant="primary" loading={busy}>
            {guarded.length ? "Request invite" : "Invite"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function EditRolesDialog({ user, onOpenChange, onSaved }: { user: User; onOpenChange: (o: boolean) => void; onSaved: (u: User | null) => void }) {
  const admin = useAdminApi();
  const router = useRouter();
  const { me } = useSession();
  const [roles, setRoles] = useState<ProductRole[]>([...user.roles]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const actor = { roles: me.user.roles, tenants: me.user.tenants ?? [] };
  const problems = userChangeProblems(actor, user, { newRoles: roles }, me.user.active_tenant ?? null);
  const guarded = guardedRoleGrants(me.user.roles, user, { newRoles: roles });
  const dirty = [...roles].sort().join() !== [...user.roles].sort().join();

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const out = await admin.patchUser(user.id, { roles: [...roles].sort(), enabled: null });
      if (out.kind === "change_request") {
        announceChangeRequest(out.request, router.push);
        onSaved(null);
      } else {
        toast.success(`Roles updated for ${user.name || user.username}`);
        onSaved(out.value ?? { ...user, roles });
      }
      onOpenChange(false);
    } catch (err) {
      setError(adminError(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title={`Roles of ${user.name || user.username}`}
      description="Product roles only. Changes apply at their next sign-in or token refresh."
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" variant="primary" loading={busy} disabled={!dirty || problems.length > 0 || roles.length === 0} onClick={() => void save()}>
            {guarded.length ? "Request change" : "Save roles"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <RolePicker legend="Roles" value={roles} onChange={setRoles} grantable={grantableRoles(me.user.roles)} error={dirty ? (problems[0] ?? (roles.length ? undefined : "Keep at least one role.")) : undefined} />
        <SecondAdminBanner reasons={guarded.map((r) => `Grants the ${roleLabelOf(r)} role`)} />
        {error ? <InlineError message={error} /> : null}
      </div>
    </Dialog>
  );
}

/**
 * **Users** (`docs/contracts/110` §2): the workspace's users with their product roles; invite
 * (Keycloak account with a temporary password + UPDATE_PASSWORD — the password is never shown),
 * change roles, disable / re-enable. A tenant admin can't grant platform admin, can't change
 * users who also belong to other workspaces, and granting an admin role needs a second admin.
 */
export function UsersPage() {
  const admin = useAdminApi();
  const router = useRouter();
  const { me } = useSession();
  const users = useResource(() => admin.users(), "users");
  const [inviting, setInviting] = useState(false);
  const [created, setCreated] = useState<UserCreated | null>(null);
  const [editing, setEditing] = useState<User | null>(null);
  const [disabling, setDisabling] = useState<User | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const actor = { roles: me.user.roles, tenants: me.user.tenants ?? [] };

  const replace = (u: User) => users.setData((prev) => prev?.map((x) => (x.id === u.id ? u : x)));

  const enable = async (u: User) => {
    setBusy(u.id);
    try {
      const out = await admin.patchUser(u.id, { enabled: true, roles: null });
      if (out.kind === "change_request") announceChangeRequest(out.request, router.push);
      else {
        replace(out.value ?? { ...u, enabled: true });
        toast.success(`${u.name || u.username} can sign in again`);
      }
    } catch (err) {
      toast.error("Couldn't enable the account", { description: adminError(err) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <AdminPageHeader
        title="Users"
        description="People with access to this workspace and their roles. Every change is audited."
        actions={
          <Button size="sm" variant="primary" icon={<Plus className="size-4" />} onClick={() => setInviting(true)}>
            Invite user
          </Button>
        }
      />
      <AdminSection title="Users" description={`Accounts holding the ${me.user.active_tenant ?? "workspace"} attribute.`}>
        <ResourceView resource={users} label="users">
          {(rows) => (
            <Table<User>
              caption="Users"
              hideCaption
              rows={[...rows].sort((a, b) => Number(b.enabled) - Number(a.enabled) || a.username.localeCompare(b.username))}
              rowKey={(u) => u.id}
              rowTone={(u) => (u.enabled ? undefined : "muted")}
              rowProps={(u) => ({ "data-user": u.username })}
              empty="No users yet."
              columns={[
                {
                  id: "name",
                  header: "Name",
                  rowHeader: true,
                  cell: (u) => (
                    <span className="flex flex-col text-left">
                      <span className="font-medium">{u.name || u.username}</span>
                      <span className="font-mono text-caption font-normal text-fg-muted">{u.username}</span>
                    </span>
                  ),
                },
                { id: "email", header: "Email", cell: (u) => <span className="break-all">{u.email ?? "—"}</span> },
                {
                  id: "roles",
                  header: "Roles",
                  cell: (u) => (
                    <span className="flex flex-wrap gap-1">
                      {u.roles.length ? u.roles.map((r) => <Badge key={r} size="sm" tone={r.endsWith("admin") ? "accent" : "neutral"}>{roleLabelOf(r)}</Badge>) : <span className="text-fg-muted">No role</span>}
                    </span>
                  ),
                },
                {
                  id: "status",
                  header: "Status",
                  nowrap: true,
                  cell: (u) => (
                    <Badge size="sm" tone={u.enabled ? "success" : "neutral"} dot>
                      {u.enabled ? "Active" : "Disabled"}
                    </Badge>
                  ),
                },
                { id: "tenants", header: "Workspaces", cell: (u) => <span className="font-mono text-caption">{u.tenants.join(", ") || "—"}</span> },
                {
                  id: "actions",
                  header: "Actions",
                  srOnlyHeader: true,
                  nowrap: true,
                  cell: (u) => {
                    const self = u.username === me.user.username;
                    const blocked = userChangeProblems(actor, u, { enabled: !u.enabled }, me.user.active_tenant ?? null);
                    const why = self ? "You can't change your own account here." : (blocked[0] ?? null);
                    return (
                      <span className="flex flex-wrap items-center gap-1">
                        <Button size="sm" variant="ghost" icon={<Pencil className="size-3.5" />} disabled={why !== null} aria-label={`Edit roles of ${u.username}`} onClick={() => setEditing(u)}>
                          Roles
                        </Button>
                        {u.enabled ? (
                          <Button size="sm" variant="ghost" icon={<UserX className="size-3.5" />} disabled={why !== null} aria-label={`Disable ${u.username}`} onClick={() => setDisabling(u)}>
                            Disable
                          </Button>
                        ) : (
                          <Button size="sm" variant="ghost" disabled={why !== null} loading={busy === u.id} aria-label={`Enable ${u.username}`} onClick={() => void enable(u)}>
                            Enable
                          </Button>
                        )}
                        {why ? <span className="basis-full text-caption whitespace-normal text-fg-subtle">{why}</span> : null}
                      </span>
                    );
                  },
                },
              ]}
            />
          )}
        </ResourceView>
      </AdminSection>

      {inviting ? (
        <InviteDialog
          onOpenChange={setInviting}
          onDone={(c) => {
            if (c) setCreated(c);
            users.reload();
          }}
        />
      ) : null}
      {created ? <SetupResult created={created} onClose={() => setCreated(null)} /> : null}
      {editing ? (
        <EditRolesDialog
          user={editing}
          onOpenChange={(o) => (o ? undefined : setEditing(null))}
          onSaved={(u) => {
            if (u) replace(u);
          }}
        />
      ) : null}
      <ConfirmDialog
        open={disabling !== null}
        onOpenChange={(o) => (o ? undefined : setDisabling(null))}
        title={`Disable ${disabling?.name || disabling?.username || ""}?`}
        description="They can't sign in until the account is enabled again. Nothing they did is removed."
        confirmLabel="Disable account"
        tone="danger"
        onConfirm={async () => {
          if (!disabling) return;
          try {
            const u = await admin.disableUser(disabling.id);
            replace(u ?? { ...disabling, enabled: false });
            toast.success(`${disabling.name || disabling.username} was disabled`);
            setDisabling(null);
          } catch (err) {
            toast.error("Couldn't disable the account", { description: adminError(err) });
            return false;
          }
        }}
      />
    </>
  );
}
