"use client";

import { Badge, Button, ConfirmDialog, Dialog, Field, Meter, Table, TextInput, Textarea, cn, toast, type BadgeTone } from "@keelson/ui";
import { BookOpen, FileUp, Plus, RefreshCw, Trash2, Upload } from "lucide-react";
import { useEffect, useEffectEvent, useId, useRef, useState, type DragEvent, type FormEvent, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { isApiError } from "@/lib/api/client";
import { adminError, int, when } from "@/lib/admin/format";
import {
  KB_UPLOAD,
  kbFileProblem,
  type EmbeddingProfile,
  type KbDocument,
  type KnowledgeBase,
  type ReembedJob,
} from "@/lib/admin/types";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { hasAnyRole } from "@/lib/roles";
import { AdminPageHeader, AdminSection, Facts, InlineError, PlatformOnlyNote, ResourceView } from "./common";
import { announceChangeRequest } from "./SecondAdmin";

/** How often running work (re-embed jobs, documents still processing) is re-read. */
export const KB_POLL_MS = 3000;

const DOC_STATUS: Record<string, { label: string; tone: BadgeTone }> = {
  processing: { label: "Processing", tone: "info" },
  ready: { label: "Ready", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
};

const JOB_STATUS: Record<string, { label: string; tone: BadgeTone }> = {
  running: { label: "Running", tone: "info" },
  succeeded: { label: "Done", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
};

const PROFILE_STATUS: Record<string, { label: string; tone: BadgeTone }> = {
  active: { label: "Active", tone: "success" },
  building: { label: "Building", tone: "info" },
  retired: { label: "Retired", tone: "neutral" },
  failed: { label: "Failed", tone: "danger" },
};

function StatusBadge({ map, status }: { map: Record<string, { label: string; tone: BadgeTone }>; status: string }) {
  const meta = map[status] ?? { label: status.replace(/_/g, " "), tone: "neutral" as BadgeTone };
  return (
    <Badge size="sm" tone={meta.tone} dot>
      {meta.label}
    </Badge>
  );
}

const sizeFmt = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1 });

/** "820 B", "12.4 KB", "3.1 MB". */
export function fileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${sizeFmt.format(bytes / 1024)} KB`;
  return `${sizeFmt.format(bytes / (1024 * 1024))} MB`;
}

/** A sentence for a failed upload (the engine's codes, `docs/contracts/90` §7). */
export function uploadError(err: unknown): string {
  if (isApiError(err)) {
    switch (err.code) {
      case "file_too_large":
        return "The file is larger than 5 MB.";
      case "unsupported_media_type":
        return "Only Markdown (.md), text (.txt) and PDF files can be added.";
      case "empty_file":
        return "The file is empty.";
      case "invalid_text":
        return "The file isn't readable text. Save it as UTF-8 Markdown or plain text, then try again.";
      case "invalid_pdf":
        return "The PDF couldn't be read. Check it opens, then try again.";
      case "encrypted_pdf":
        return "Password-protected PDFs can't be added. Remove the password, then try again.";
      case "no_text":
        return "The file has no text that can be searched (a scanned PDF, for example).";
      case "too_many_pages":
      case "too_many_chunks":
        return "The document is too long. Split it into smaller files.";
      case "file_required":
      case "invalid_multipart":
        return "The file didn't arrive. Try again.";
    }
    if (err.status === 502 || err.status === 503) return "The document was stored but couldn't be indexed yet. Use Re-embed documents to retry.";
  }
  return adminError(err);
}

/** Drop zone + "Choose file" (keyboard path) + optional title; validates before uploading. */
function UploadArea({ kb, onUploaded }: { kb: KnowledgeBase; onUploaded: () => void }) {
  const admin = useAdminApi();
  const inputRef = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const hintId = useId();
  const [title, setTitle] = useState("");
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const upload = async (files: FileList | File[] | null) => {
    const list = files ? [...files] : [];
    if (list.length === 0 || busy) return;
    if (list.length > 1) {
      setError("Add one file at a time.");
      return;
    }
    const file = list[0]!;
    const problem = kbFileProblem(file);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    setBusy(file.name);
    try {
      const doc = await admin.uploadKbDocument(kb.id, file, title);
      if (doc.duplicate) toast(`Already in ${kb.name}`, { description: `${doc.title} has the same content.` });
      else if (doc.status === "failed") toast.error(`${doc.title} couldn't be indexed`, { description: doc.error ?? undefined });
      else toast.success(`Added ${doc.title}`, { description: `${int(doc.chunks)} passages indexed.` });
      setTitle("");
      onUploaded();
    } catch (err) {
      setError(uploadError(err));
      onUploaded(); // a 502/503 still stores the document as failed
    } finally {
      setBusy(null);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  const onDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setDragging(false);
    void upload(e.dataTransfer?.files ?? null);
  };

  return (
    <div className="flex flex-col gap-2">
      <div
        data-dropzone=""
        data-dragging={dragging ? "" : undefined}
        onDragOver={(e) => {
          e.preventDefault();
          if (!dragging) setDragging(true);
        }}
        onDragLeave={(e) => {
          // Moving over the zone's own children fires dragleave on the zone: not a real leave.
          if (e.relatedTarget instanceof Node && e.currentTarget.contains(e.relatedTarget)) return;
          setDragging(false);
        }}
        onDrop={onDrop}
        className={cn(
          "flex flex-col items-center gap-2 rounded-md border-2 border-dashed px-4 py-5 text-center transition-colors duration-150",
          dragging ? "border-accent bg-accent-soft" : "border-border-strong bg-surface-2",
        )}
      >
        <FileUp aria-hidden="true" className="size-6 text-fg-muted" />
        <p className="text-ui text-fg">
          {busy ? (
            <span role="status">Uploading {busy}…</span>
          ) : (
            <>Drag a file here, or choose one</>
          )}
        </p>
        <p id={hintId} className="text-caption text-fg-muted">
          Markdown, text or PDF, up to 5 MB. Documents are split into passages and indexed for the Playbook analyst.
        </p>
        <input
          ref={inputRef}
          type="file"
          accept={KB_UPLOAD.accept}
          className="sr-only"
          tabIndex={-1}
          aria-hidden="true"
          aria-label="Choose a file to add"
          onChange={(e) => void upload(e.target.files)}
          data-testid="kb-file-input"
        />
        <Button
          size="sm"
          variant="secondary"
          icon={<Upload className="size-4" />}
          loading={busy !== null}
          aria-describedby={hintId}
          onClick={() => inputRef.current?.click()}
        >
          Choose file
        </Button>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex min-w-0 flex-1 basis-64 flex-col gap-1">
          <label htmlFor={titleId} className="text-ui font-medium text-fg">
            Title <span className="font-normal text-fg-muted">(optional, for the next upload)</span>
          </label>
          <TextInput id={titleId} value={title} maxLength={200} placeholder="Defaults to the file name" onChange={(e) => setTitle(e.target.value)} />
        </div>
      </div>
      {error ? <InlineError message={error} /> : null}
    </div>
  );
}

function CreateKbDialog({ open, onOpenChange, onCreated }: { open: boolean; onOpenChange: (o: boolean) => void; onCreated: (kb: KnowledgeBase) => void }) {
  const admin = useAdminApi();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameError = !name.trim() ? "Enter a name." : name.trim().length > 120 ? "At most 120 characters." : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (nameError) return;
    setSaving(true);
    setError(null);
    try {
      const kb = await admin.createKb({ name: name.trim(), description: description.trim() || undefined });
      toast.success(`Created ${kb.name}`);
      onCreated(kb);
      onOpenChange(false);
    } catch (err) {
      setError(isApiError(err, "name_taken") ? "A knowledge base with this name already exists." : adminError(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="New knowledge base" description="A collection of documents (playbooks, contracts, SOPs, rate cards) agents can search.">
      <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)} noValidate>
        <Field label="Name" required error={submitted ? nameError : null}>
          <TextInput value={name} required maxLength={120} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="Description">
          <Textarea value={description} maxLength={2000} rows={3} onChange={(v) => setDescription(v)} />
        </Field>
        {error ? <InlineError message={error} /> : null}
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" type="submit" variant="primary" loading={saving}>
            Create
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function DocumentsSection({ kb, onChanged }: { kb: KnowledgeBase; onChanged: () => void }) {
  const admin = useAdminApi();
  const docs = useResource(() => admin.kbDocuments(kb.id), `kb-docs:${kb.id}`);
  const [toDelete, setToDelete] = useState<KbDocument | null>(null);
  const processing = (docs.data ?? []).some((d) => d.status === "processing");
  const poll = useEffectEvent(() => docs.reload());
  useEffect(() => {
    if (!processing) return;
    const timer = setInterval(() => poll(), KB_POLL_MS);
    return () => clearInterval(timer);
  }, [processing]);

  const refresh = () => {
    docs.reload();
    onChanged();
  };

  return (
    <AdminSection
      id="kb-documents"
      title={`Documents in ${kb.name}`}
      description={kb.description || "Text from these documents is untrusted: agents only see a screened, structured extract."}
    >
      <UploadArea kb={kb} onUploaded={refresh} />
      <ResourceView resource={docs} label="documents">
        {(rows) => (
          <Table<KbDocument>
            caption={`Documents in ${kb.name}`}
            hideCaption
            rows={rows}
            rowKey={(d) => d.id}
            rowTone={(d) => (d.status === "failed" ? "danger" : undefined)}
            empty="No documents yet. Add a playbook, contract or policy above."
            columns={[
              {
                id: "title",
                header: "Document",
                rowHeader: true,
                cell: (d) => (
                  <span className="flex flex-col text-left">
                    <span className="font-medium text-fg">{d.title}</span>
                    {d.filename && d.filename !== d.title ? <span className="font-mono text-caption font-normal break-all text-fg-muted">{d.filename}</span> : null}
                  </span>
                ),
              },
              {
                id: "status",
                header: "Status",
                cell: (d) => (
                  <span className="flex flex-col gap-0.5">
                    <StatusBadge map={DOC_STATUS} status={d.status} />
                    {d.status === "failed" && d.error ? <span className="text-caption text-fg-muted">{d.error.replace(/_/g, " ")}</span> : null}
                  </span>
                ),
              },
              { id: "size", header: "Size", align: "end", nowrap: true, cell: (d) => fileSize(d.bytes) },
              { id: "chunks", header: "Passages", align: "end", nowrap: true, cell: (d) => int(d.chunks) },
              {
                id: "added",
                header: "Added",
                cell: (d) => (
                  <span className="flex flex-col text-caption">
                    <span>{when(d.created_at)}</span>
                    {d.created_by ? <span className="text-fg-muted">by {d.created_by}</span> : null}
                  </span>
                ),
              },
              {
                id: "actions",
                header: "Actions",
                srOnlyHeader: true,
                align: "end",
                cell: (d) => (
                  <Button size="sm" variant="ghost" icon={<Trash2 className="size-4" />} aria-label={`Delete ${d.title}`} onClick={() => setToDelete(d)}>
                    Delete
                  </Button>
                ),
              },
            ]}
          />
        )}
      </ResourceView>
      {toDelete ? (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setToDelete(null)}
          title={`Delete ${toDelete.title}?`}
          description="Its passages are removed from the index at once. Cases that already cited it keep their evidence."
          confirmLabel="Delete document"
          tone="danger"
          onConfirm={async () => {
            try {
              await admin.deleteKbDocument(kb.id, toDelete.id);
              toast.success(`Deleted ${toDelete.title}`);
              setToDelete(null);
              refresh();
              return true;
            } catch (err) {
              toast.error("Couldn't delete the document", { description: adminError(err) });
              return false;
            }
          }}
        />
      ) : null}
    </AdminSection>
  );
}

function ProfileFacts({ profile, platform }: { profile: EmbeddingProfile; platform: boolean }) {
  const items: [string, ReactNode][] = [
    ["Model alias", <span key="a" className="font-mono text-caption">{profile.alias}</span>],
    ["Dimensions", int(profile.dims)],
    ["Storage", profile.storage ? <span key="s" className="font-mono text-caption">{profile.storage}</span> : "—"],
    ["Version", profile.version !== undefined ? `v${profile.version}` : "—"],
    ["Status", <StatusBadge key="st" map={PROFILE_STATUS} status={profile.status ?? (profile.active ? "active" : "unknown")} />],
  ];
  if (platform && profile.provider_model) items.splice(1, 0, ["Provider model", <span key="p" className="font-mono text-caption break-all">{profile.provider_model}</span>]);
  return <Facts items={items} />;
}

function JobRow({ job }: { job: ReembedJob }) {
  const total = Math.max(job.chunks_total, job.chunks_done);
  return (
    <li className="flex flex-col gap-1.5 rounded-sm border border-border px-3 py-2" data-job-status={job.status}>
      <div className="flex flex-wrap items-center gap-2 text-ui">
        <StatusBadge map={JOB_STATUS} status={job.status} />
        <span className="text-fg">{job.scope === "platform" ? "New embedding profile" : "Re-embed this workspace"}</span>
        <span className="text-caption text-fg-muted">
          {when(job.created_at)}
          {job.requested_by ? ` · ${job.requested_by}` : ""}
        </span>
      </div>
      {job.status === "running" && total > 0 ? (
        <Meter
          label="Passages re-embedded"
          value={job.chunks_done}
          max={total}
          valueText={`${int(job.chunks_done)} of ${int(total)} passages`}
          warnAt={2}
          dangerAt={2}
          showLabel
        />
      ) : job.status !== "running" ? (
        <span className="text-caption text-fg-muted">
          {int(job.chunks_done)} of {int(job.chunks_total)} passages{job.error ? ` · ${job.error.replace(/_/g, " ")}` : ""}
        </span>
      ) : null}
    </li>
  );
}

function NewProfileDialog({ open, onOpenChange, current, onStarted }: { open: boolean; onOpenChange: (o: boolean) => void; current: EmbeddingProfile | null; onStarted: () => void }) {
  const admin = useAdminApi();
  const [alias, setAlias] = useState(current?.alias ?? "");
  const [dims, setDims] = useState(current ? String(current.dims) : "");
  const [submitted, setSubmitted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dimsValue = Number(dims);
  const errors = {
    alias: alias.trim() ? null : "Enter the embedding model alias.",
    dims: /^\d+$/.test(dims.trim()) && dimsValue >= 1 && dimsValue <= 4000 ? null : "A whole number from 1 to 4,000.",
  };
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitted(true);
    if (errors.alias || errors.dims) return;
    setSaving(true);
    setError(null);
    try {
      await admin.reembed({ scope: "platform", alias: alias.trim(), dims: dimsValue });
      toast.success("Building the new embedding profile", { description: "Search keeps using the current one until it is ready." });
      onStarted();
      onOpenChange(false);
    } catch (err) {
      setError(isApiError(err, "reembed_running") ? "A re-embed job is already running. Wait for it to finish." : adminError(err));
    } finally {
      setSaving(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title="New embedding profile" description="Re-embeds every workspace's documents into a new index, then switches search over. Platform-wide.">
      <form className="flex flex-col gap-3" onSubmit={(e) => void submit(e)} noValidate>
        <Field label="Model alias" required error={submitted ? errors.alias : null} description="An embedding alias from the AI Gateway.">
          <TextInput value={alias} mono required onChange={(e) => setAlias(e.target.value)} />
        </Field>
        <Field label="Dimensions" required error={submitted ? errors.dims : null} description="Over 2,000 dimensions are stored at half precision.">
          <TextInput value={dims} inputMode="numeric" required onChange={(e) => setDims(e.target.value)} />
        </Field>
        {error ? <InlineError message={error} /> : null}
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" type="submit" variant="primary" loading={saving}>
            Build profile
          </Button>
        </div>
      </form>
    </Dialog>
  );
}

function EmbeddingSection({ profile, platform, onChanged }: { profile: EmbeddingProfile | null; platform: boolean; onChanged: () => void }) {
  const admin = useAdminApi();
  const jobs = useResource(() => admin.reembedJobs(), "kb-reembed");
  const [confirm, setConfirm] = useState(false);
  const [newProfile, setNewProfile] = useState(false);
  const running = (jobs.data?.items ?? []).some((j) => j.status === "running");
  const poll = useEffectEvent(() => jobs.reload());
  const finished = useEffectEvent(() => onChanged());
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => poll(), KB_POLL_MS);
    return () => clearInterval(timer);
  }, [running]);
  // When a job finishes, the profile and document counts may have changed.
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) finished();
    wasRunning.current = running;
  }, [running]);

  return (
    <AdminSection
      id="kb-embedding"
      title="Embedding profile"
      description="The model that turns passages into vectors for search. Changing it re-embeds every document."
      actions={
        <>
          <Button size="sm" icon={<RefreshCw className="size-4" />} disabled={running || !profile} onClick={() => setConfirm(true)}>
            Re-embed documents
          </Button>
          {platform ? (
            <Button size="sm" variant="secondary" disabled={running} onClick={() => setNewProfile(true)}>
              New profile
            </Button>
          ) : (
            <PlatformOnlyNote>Changing the model requires a platform admin</PlatformOnlyNote>
          )}
        </>
      }
    >
      {profile ? <ProfileFacts profile={profile} platform={platform} /> : <p className="text-ui text-fg-muted">{"No embedding profile is active yet; documents can't be searched until one is."}</p>}
      <ResourceView resource={jobs} label="re-embed jobs" rows={1}>
        {(data) =>
          data.items.length > 0 ? (
            <section aria-label="Recent re-embed jobs" className="flex flex-col gap-1.5">
              <h3 className="text-caption font-medium tracking-wide text-fg-subtle uppercase">Recent jobs</h3>
              <ul className="flex flex-col gap-1.5">
                {data.items.slice(0, 5).map((j) => (
                  <JobRow key={j.id} job={j} />
                ))}
              </ul>
            </section>
          ) : null
        }
      </ResourceView>
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title="Re-embed documents?"
        description="Indexes every passage in this workspace that has no vector for the active profile (for example after a failed upload). Search keeps working meanwhile."
        confirmLabel="Re-embed"
        onConfirm={async () => {
          try {
            await admin.reembed({ scope: "tenant" });
            toast.success("Re-embedding started");
            jobs.reload();
            return true;
          } catch (err) {
            toast.error("Couldn't start re-embedding", {
              description: isApiError(err, "reembed_running") ? "A re-embed job is already running." : adminError(err),
            });
            return false;
          }
        }}
      />
      {newProfile ? <NewProfileDialog open onOpenChange={setNewProfile} current={profile} onStarted={jobs.reload} /> : null}
    </AdminSection>
  );
}

/** Admin → Knowledge (`docs/contracts/90` §7, plan §17). */
export function KnowledgePage() {
  const admin = useAdminApi();
  const { me } = useSession();
  const platform = hasAnyRole(me.user, ["platform-admin"]);
  const list = useResource(() => admin.kbs(), `kbs:${me.user.active_tenant}`);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<KnowledgeBase | null>(null);
  const router = useRouter();
  const kbs = list.data?.items ?? [];
  const current = kbs.find((k) => k.id === selected) ?? kbs[0] ?? null;

  return (
    <>
      <AdminPageHeader
        title="Knowledge"
        description="Playbooks, contracts, SOPs and rate cards the Playbook analyst can search."
        actions={
          <Button size="sm" variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
            New knowledge base
          </Button>
        }
      />
      <AdminSection title="Knowledge bases">
        <ResourceView resource={list} label="knowledge bases">
          {(data) => (
            <Table<KnowledgeBase>
              caption="Knowledge bases"
              hideCaption
              rows={data.items}
              rowKey={(k) => k.id}
              rowTone={(k) => (k.id === current?.id ? "accent" : undefined)}
              empty="No knowledge bases yet. Create one, then add documents."
              columns={[
                {
                  id: "name",
                  header: "Name",
                  rowHeader: true,
                  cell: (k) => (
                    <span className="flex flex-col text-left">
                      <span className="font-medium text-fg">{k.name}</span>
                      {k.description ? <span className="text-caption font-normal text-fg-muted">{k.description}</span> : null}
                    </span>
                  ),
                },
                {
                  id: "docs",
                  header: "Documents",
                  nowrap: true,
                  cell: (k) => (k.ready_documents === k.documents ? int(k.documents) : `${int(k.ready_documents)} of ${int(k.documents)} ready`),
                },
                { id: "chunks", header: "Passages", align: "end", nowrap: true, cell: (k) => int(k.chunks) },
                { id: "created", header: "Created", nowrap: true, cell: (k) => <span className="text-caption">{when(k.created_at)}</span> },
                {
                  id: "open",
                  header: "Actions",
                  srOnlyHeader: true,
                  align: "end",
                  cell: (k) => (
                    <span className="flex flex-wrap items-center justify-end gap-1">
                      {k.id === current?.id ? (
                        <Badge size="sm" tone="accent">
                          Showing
                        </Badge>
                      ) : (
                        <Button size="sm" variant="ghost" icon={<BookOpen className="size-4" />} aria-label={`Show documents in ${k.name}`} onClick={() => setSelected(k.id)}>
                          Documents
                        </Button>
                      )}
                      <Button size="sm" variant="ghost" icon={<Trash2 className="size-4" />} aria-label={`Delete ${k.name}`} onClick={() => setDeleting(k)}>
                        Delete
                      </Button>
                    </span>
                  ),
                },
              ]}
            />
          )}
        </ResourceView>
      </AdminSection>
      {current ? <DocumentsSection key={current.id} kb={current} onChanged={list.reload} /> : null}
      {list.data ? (
        <EmbeddingSection key={me.user.active_tenant ?? ""} profile={list.data.profile} platform={platform} onChanged={list.reload} />
      ) : null}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(o) => (o ? undefined : setDeleting(null))}
        title={`Delete ${deleting?.name ?? "this knowledge base"}?`}
        description={
          platform
            ? "Its documents and passages are deleted. A knowledge base an agent still uses can't be deleted; remove it from the agent first."
            : "Its documents and passages are deleted. Deleting needs a second admin: this sends a request for approval. A knowledge base an agent still uses can't be deleted."
        }
        confirmLabel={platform ? "Delete knowledge base" : "Request deletion"}
        tone="danger"
        onConfirm={async () => {
          if (!deleting) return;
          try {
            const out = await admin.deleteKb(deleting.id);
            if (out.kind === "change_request") {
              announceChangeRequest(out.request, router.push);
            } else {
              toast.success(`${deleting.name} deleted`);
              if (selected === deleting.id) setSelected(null);
              list.reload();
            }
            setDeleting(null);
          } catch (err) {
            toast.error(`Couldn't delete ${deleting.name}`, { description: adminError(err) });
            return false;
          }
        }}
      />
      {creating ? (
        <CreateKbDialog
          open
          onOpenChange={setCreating}
          onCreated={(kb) => {
            setSelected(kb.id);
            list.reload();
          }}
        />
      ) : null}
    </>
  );
}
