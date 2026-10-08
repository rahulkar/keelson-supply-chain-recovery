"use client";

import { Badge, Button, ConfirmDialog, Dialog, Field, Select, Table, TextInput, toast } from "@keelson/ui";
import { Pause, Play, Plus } from "lucide-react";
import { useState, type FormEvent } from "react";
import { adminError, when } from "@/lib/admin/format";
import { adminsWithoutAccess, isSuspended, type Pack, type Tenant } from "@/lib/admin/types";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { SLUG_PATTERN, slugify } from "@/lib/admin/wizard";
import { AdminPageHeader, AdminSection, InlineError, ResourceView } from "./common";

const USERNAME = /^[A-Za-z0-9._@-]+$/;
/** `POST /api/admin/tenants` accepts at most this many admin usernames. */
export const MAX_TENANT_ADMINS = 20;

export function parseUsernames(text: string): { names: string[]; invalid: string[] } {
  const names = [...new Set(text.split(/[\s,;]+/).map((s) => s.trim()).filter(Boolean))];
  return { names, invalid: names.filter((n) => !USERNAME.test(n)) };
}

function CreateTenantDialog({ open, onOpenChange, packs, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; packs: Pack[]; onCreated: () => void }) {
  const admin = useAdminApi();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [pack, setPack] = useState(packs[0]?.name ?? "");
  const [admins, setAdmins] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const users = parseUsernames(admins);
  const errors = {
    name: name.trim() ? null : "Enter the workspace name.",
    slug: SLUG_PATTERN.test(slug) ? null : "Lower-case letters, digits and hyphens.",
    pack: pack ? null : "Choose a template pack.",
    admins: users.invalid.length
      ? `Not a username: ${users.invalid.join(", ")}`
      : users.names.length > MAX_TENANT_ADMINS
        ? `At most ${MAX_TENANT_ADMINS} admins; add more later.`
        : users.names.length
          ? null
          : "Name at least one admin.",
  };
  const selectedPack = packs.find((p) => p.name === pack);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (Object.values(errors).some(Boolean)) return;
    setSaving(true);
    setError(null);
    try {
      const out = await admin.createTenant({ slug, name: name.trim(), pack, admin_usernames: users.names });
      const label = out?.name ?? name.trim();
      if (out?.created === false) toast.success(`${label} already existed`, { description: `Re-provisioned from the ${pack} pack.` });
      else toast.success(`Created ${label}`, { description: `From the ${pack} pack.` });
      // Say who did NOT get access (unknown username, identity provider failure): the tenant exists
      // either way, so this must not read as a full success.
      const missing = adminsWithoutAccess(out);
      if (missing.length > 0) {
        toast.error(`${missing.length === 1 ? "1 admin was" : `${missing.length} admins were`} not given access`, {
          description: missing.map((a) => `${a.username} (${a.status === "not_found" ? "no such user" : "couldn't update the user"})`).join(", "),
        });
      }
      onCreated();
      onOpenChange(false);
    } catch (err) {
      setError(adminError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="Create tenant from pack" description="Creates the workspace, its gateway team and key, the pack's connectors (as drafts) and agent settings v1.">
      <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)} noValidate>
        <Field label="Name" required error={submitted ? errors.name : null}>
          <TextInput
            value={name}
            required
            onChange={(e) => {
              setName(e.target.value);
              if (!slugTouched) setSlug(slugify(e.target.value));
            }}
          />
        </Field>
        <Field label="Slug" required error={submitted ? errors.slug : null} description="Can't be changed later.">
          <TextInput
            value={slug}
            mono
            required
            onChange={(e) => {
              setSlugTouched(true);
              setSlug(e.target.value);
            }}
          />
        </Field>
        <Field label="Template pack" required error={submitted ? errors.pack : null} description={selectedPack?.description ?? undefined}>
          <Select value={pack} placeholder="Choose a pack…" onChange={setPack} options={packs.map((p) => ({ value: p.name, label: p.name }))} />
        </Field>
        <Field label="Admin usernames" required error={submitted ? errors.admins : null} description="Comma-separated Keelson usernames; they get access to the new workspace.">
          <TextInput value={admins} mono required placeholder="sam, nora" onChange={(e) => setAdmins(e.target.value)} />
        </Field>
        {error ? <InlineError message={error} /> : null}
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" type="submit" variant="primary" loading={saving}>
            Create tenant
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

export function TenantsPage() {
  const admin = useAdminApi();
  const tenants = useResource(() => admin.tenants(), "tenants");
  const packs = useResource(() => admin.packs(), "packs");
  const [open, setOpen] = useState(false);
  const [suspending, setSuspending] = useState<{ tenant: Tenant; suspend: boolean } | null>(null);
  return (
    <>
      <AdminPageHeader
        title="Tenants"
        description="Every workspace on this platform. Platform admins only; every action is audited."
        actions={
          <Button size="sm" variant="primary" icon={<Plus className="size-4" />} disabled={!packs.data?.length} onClick={() => setOpen(true)}>
            Create tenant from pack
          </Button>
        }
      />
      <AdminSection title="Workspaces">
        <ResourceView resource={tenants} label="tenants">
          {(rows) => (
            <Table<Tenant>
              caption="Workspaces"
              hideCaption
              rows={rows}
              rowKey={(t) => t.slug}
              empty="No tenants yet."
              columns={[
                { id: "name", header: "Name", rowHeader: true, cell: (t) => t.name },
                { id: "slug", header: "Slug", cell: (t) => <span className="font-mono text-caption">{t.slug}</span> },
                { id: "pack", header: "Pack", cell: (t) => t.pack ?? "—" },
                { id: "created", header: "Created", nowrap: true, cell: (t) => when(t.created_at) },
                {
                  id: "status",
                  header: "Status",
                  nowrap: true,
                  cell: (t) => (
                    <Badge size="sm" tone={isSuspended(t) ? "danger" : "success"} dot>
                      {isSuspended(t) ? "Suspended" : "Active"}
                    </Badge>
                  ),
                },
                {
                  id: "actions",
                  header: "Actions",
                  srOnlyHeader: true,
                  nowrap: true,
                  cell: (t) =>
                    isSuspended(t) ? (
                      <Button size="sm" variant="ghost" icon={<Play className="size-3.5" />} aria-label={`Resume ${t.name}`} onClick={() => setSuspending({ tenant: t, suspend: false })}>
                        Resume
                      </Button>
                    ) : (
                      <Button size="sm" variant="ghost" icon={<Pause className="size-3.5" />} aria-label={`Suspend ${t.name}`} onClick={() => setSuspending({ tenant: t, suspend: true })}>
                        Suspend
                      </Button>
                    ),
                },
              ]}
            />
          )}
        </ResourceView>
      </AdminSection>
      <AdminSection title="Template packs" description="Starting points for new workspaces (data/packs).">
        <ResourceView resource={packs} label="packs">
          {(rows) => (
            <Table<Pack>
              caption="Template packs"
              hideCaption
              rows={rows}
              rowKey={(p) => p.name}
              empty="No packs found."
              columns={[
                { id: "name", header: "Pack", rowHeader: true, cell: (p) => <span className="font-mono text-caption">{p.name}</span> },
                { id: "desc", header: "Description", cell: (p) => p.description ?? "—" },
                { id: "conn", header: "Connectors", align: "end", cell: (p) => String(p.connectors?.length ?? 0) },
              ]}
            />
          )}
        </ResourceView>
      </AdminSection>
      {open && packs.data ? <CreateTenantDialog open onOpenChange={setOpen} packs={packs.data} onCreated={tenants.reload} /> : null}
      <ConfirmDialog
        open={suspending !== null}
        onOpenChange={(o) => (o ? undefined : setSuspending(null))}
        title={suspending?.suspend ? `Suspend ${suspending.tenant.name}?` : `Resume ${suspending?.tenant.name ?? ""}?`}
        description={
          suspending?.suspend
            ? "Its users can't sign in and its API calls are refused until it is resumed. Data, connectors and settings are kept."
            : "Its users can sign in again."
        }
        confirmLabel={suspending?.suspend ? "Suspend tenant" : "Resume tenant"}
        tone={suspending?.suspend ? "danger" : "default"}
        onConfirm={async () => {
          if (!suspending) return;
          const { tenant, suspend } = suspending;
          try {
            await admin.setTenantSuspended(tenant.slug, suspend);
            tenants.setData((prev) => prev?.map((x) => (x.slug === tenant.slug ? { ...x, suspended: suspend, status: suspend ? "suspended" : "active" } : x)));
            toast.success(suspend ? `${tenant.name} is suspended` : `${tenant.name} is active again`);
            setSuspending(null);
            tenants.reload();
          } catch (err) {
            toast.error(suspend ? "Couldn't suspend the tenant" : "Couldn't resume the tenant", { description: adminError(err) });
            return false;
          }
        }}
      />
    </>
  );
}
