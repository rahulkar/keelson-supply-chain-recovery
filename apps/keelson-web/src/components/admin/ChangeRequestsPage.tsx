"use client";

import { Badge, Button, Card, Dialog, EmptyState, Field, Select, Table, Textarea, formatJsonValue, toast } from "@keelson/ui";
import { Check, Inbox, RotateCw, X } from "lucide-react";
import { useEffect, useState, useSyncExternalStore } from "react";
import { isApiError } from "@/lib/api/client";
import {
  CHANGE_STATUS_META,
  DECISION_REASON_MAX,
  approveState,
  canReject,
  decisionBody,
  decisionErrorText,
  decisionToast,
  effectiveStatus,
  expiresIn,
  kindLabel,
  reasonLabel,
  sortRequests,
  type ChangeRequest,
} from "@/lib/admin/changeRequests";
import { adminError, when } from "@/lib/admin/format";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { AdminPageHeader, ResourceView } from "./common";

type Diff = ChangeRequest["diff"][number];

const OP_LABELS: Record<Diff["op"], string> = { add: "Added", remove: "Removed", replace: "Changed" };

/** JSON pointer → "policy › auto_max_lines" (unescaped `~1` / `~0`). */
export function pointerLabel(path: string): string {
  if (!path) return "(whole document)";
  return path
    .split("/")
    .slice(1)
    .map((p) => p.replace(/~1/g, "/").replace(/~0/g, "~"))
    .join(" › ");
}

function DiffTable({ request }: { request: ChangeRequest }) {
  return (
    <Table<Diff & { i: number }>
      caption={`Changes requested in ${kindLabel(request.kind)}`}
      hideCaption
      rows={request.diff.map((d, i) => ({ ...d, i }))}
      rowKey={(d) => `${d.i}`}
      columns={[
        { id: "path", header: "Setting", rowHeader: true, cell: (d) => <span className="font-mono text-caption break-all">{pointerLabel(d.path)}</span> },
        { id: "op", header: "Change", nowrap: true, cell: (d) => OP_LABELS[d.op] },
        { id: "before", header: "Before", cell: (d) => <span className="font-mono text-caption break-all text-fg-muted">{d.op === "add" ? "—" : formatJsonValue(d.before)}</span> },
        { id: "after", header: "After", cell: (d) => <span className="font-mono text-caption break-all">{d.op === "remove" ? "—" : formatJsonValue(d.after)}</span> },
      ]}
    />
  );
}

function DecideDialog({
  request,
  decision,
  own,
  onOpenChange,
  onDecided,
}: {
  request: ChangeRequest;
  decision: "approve" | "reject";
  /** The viewer asked for it: rejecting withdraws it. */
  own: boolean;
  onOpenChange: (open: boolean) => void;
  onDecided: (updated: ChangeRequest | null) => void;
}) {
  const admin = useAdminApi();
  const [reason, setReason] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const approve = decision === "approve";
  const body = decisionBody(reason, decision);
  const reasonError = reason.trim().length > DECISION_REASON_MAX ? `At most ${DECISION_REASON_MAX} characters.` : !approve && touched && !body ? "Say why: the requester sees it." : undefined;

  const submit = async () => {
    setTouched(true);
    if (!body) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await admin.decideChangeRequest(request.id, decision, body.reason);
      toast.success(decisionToast(updated, decision, own), { description: `${kindLabel(request.kind)} · ${request.target}` });
      onDecided(updated);
      onOpenChange(false);
    } catch (err) {
      const known = isApiError(err) ? decisionErrorText(err.code, err.status) : null;
      setError(known ?? adminError(err));
      // Someone else decided it (or it expired) meanwhile: show the list as it is now.
      if (isApiError(err) && (err.code === "not_pending" || err.code === "expired")) onDecided(null);
    } finally {
      setBusy(false);
    }
  };

  const who = request.requested_by.name || request.requested_by.username;
  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title={approve ? "Approve this change?" : own ? "Withdraw your request?" : "Reject this change?"}
      description={
        approve
          ? `${kindLabel(request.kind)} for ${request.target}: it is checked again against the current settings and applied right away.`
          : own
            ? `${kindLabel(request.kind)} for ${request.target}: nothing changes and the request is closed.`
            : `${kindLabel(request.kind)} for ${request.target}: nothing changes; ${who} sees your reason.`
      }
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button size="sm" variant={approve ? "primary" : "danger"} loading={busy} disabled={reason.trim().length > DECISION_REASON_MAX} onClick={() => void submit()}>
            {approve ? "Approve and apply" : own ? "Withdraw request" : "Reject"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <Field label={approve ? "Note (optional)" : "Reason"} required={!approve} withCounter error={reasonError}>
          <Textarea value={reason} maxLength={DECISION_REASON_MAX} rows={3} onChange={setReason} />
        </Field>
        {error ? (
          <p role="alert" className="text-ui text-danger">
            {error}
          </p>
        ) : null}
      </div>
    </Dialog>
  );
}

function RequestCard({ request, now, highlighted, onDecide }: { request: ChangeRequest; now: Date; highlighted: boolean; onDecide: (d: "approve" | "reject") => void }) {
  const { me } = useSession();
  const status = effectiveStatus(request, now);
  const meta = CHANGE_STATUS_META[status];
  const state = approveState(request, me.user.sub, now, me.user.roles);
  const rejectable = canReject(request, now);
  const headingId = `cr-${request.id}`;
  const expiry = expiresIn(request, now);
  return (
    <Card padding="sm" labelledBy={headingId} className={highlighted ? "flex flex-col gap-3 ring-2 ring-accent" : "flex flex-col gap-3"}>
      <div data-change-request={request.id} data-status={status} className="flex flex-col gap-3">
        <div className="flex flex-wrap items-start gap-x-3 gap-y-1">
          <div className="min-w-0 flex-1 basis-64">
            <h2 id={headingId} className="text-body font-semibold text-fg">
              {kindLabel(request.kind)} <span className="font-mono text-caption font-normal break-all text-fg-muted">{request.target}</span>
            </h2>
            <p className="text-caption text-fg-muted">
              Asked by {request.requested_by.name || request.requested_by.username} · {when(request.requested_at)}
              {request.base_version ? ` · based on ${request.base_version}` : ""}
              {expiry ? ` · ${expiry}` : ""}
            </p>
          </div>
          <Badge size="sm" tone={meta.tone} dot>
            {meta.label}
          </Badge>
        </div>
        <div>
          <p className="text-caption font-medium text-fg-muted">Why it needs a second admin</p>
          <ul className="list-disc pl-5 text-ui">
            {request.reasons.map((r, i) => (
              <li key={`${r.code}-${i}`}>
                <span className="font-medium">{reasonLabel(r.code)}</span>
                <span className="text-fg-muted">: {r.reason}</span>
              </li>
            ))}
          </ul>
        </div>
        <DiffTable request={request} />
        {request.approver && request.decided_at ? (
          <p className="text-caption text-fg-muted">
            {status === "rejected" ? "Rejected" : "Approved"} by {request.approver.name || request.approver.username} ({request.approver.role === "platform-admin" ? "platform admin" : "tenant admin"}) ·{" "}
            {when(request.decided_at)}
            {request.decision_reason ? ` · “${request.decision_reason}”` : ""}
          </p>
        ) : null}
        {state.can ? (
          <div className="flex flex-wrap gap-2" role="group" aria-label={`Decide on ${kindLabel(request.kind)} ${request.target}`}>
            <Button size="sm" variant="primary" icon={<Check className="size-4" />} onClick={() => onDecide("approve")}>
              Approve
            </Button>
            <Button size="sm" variant="secondary" icon={<X className="size-4" />} onClick={() => onDecide("reject")}>
              Reject
            </Button>
          </div>
        ) : state.why === "own" ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <p className="text-caption text-fg-muted" data-own-request="">
              You asked for this change: another tenant admin or a platform admin must approve it.
            </p>
            {rejectable ? (
              <Button size="sm" variant="ghost" icon={<X className="size-4" />} aria-label={`Withdraw your request: ${kindLabel(request.kind)} ${request.target}`} onClick={() => onDecide("reject")}>
                Withdraw
              </Button>
            ) : null}
          </div>
        ) : state.why === "platform_only" ? (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <p className="text-caption text-fg-muted" data-platform-only="">
              This change affects every workspace: only a platform admin can approve it.
            </p>
            {rejectable ? (
              <Button size="sm" variant="secondary" icon={<X className="size-4" />} onClick={() => onDecide("reject")}>
                Reject
              </Button>
            ) : null}
          </div>
        ) : null}
      </div>
    </Card>
  );
}

const noopSubscribe = () => () => {};

/**
 * **Pending changes** (`docs/contracts/110` §1): guarded changes waiting for a second admin. A
 * different tenant admin or a platform admin approves (the change is applied atomically,
 * re-validated: `409` when the settings moved meanwhile) or rejects with a reason.
 * Self-approval is impossible — your own requests offer only "Withdraw". Platform-wide kinds
 * (the embedding profile) are approved by platform admins only.
 */
export function ChangeRequestsPage() {
  const admin = useAdminApi();
  const { me } = useSession();
  const [scope, setScope] = useState<"pending" | "all">("pending");
  const list = useResource(() => admin.changeRequests(scope), `change-requests:${scope}`);
  const [deciding, setDeciding] = useState<{ request: ChangeRequest; decision: "approve" | "reject" } | null>(null);
  // `?request=<id>` (from a "Sent for approval" toast) highlights that request; read on the client only.
  const highlight = useSyncExternalStore(
    noopSubscribe,
    () => new URLSearchParams(window.location.search).get("request"),
    () => null,
  );
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    if (!highlight || !list.data) return;
    document.querySelector(`[data-change-request="${CSS.escape(highlight)}"]`)?.scrollIntoView({ block: "center" });
  }, [highlight, list.data]);

  return (
    <>
      <AdminPageHeader
        title="Pending changes"
        description="Guarded changes need a second admin: another tenant admin or a platform admin approves before they apply. Requests expire after 72 hours."
        actions={
          <>
            <Field label="Show" hideLabel>
              <Select
                value={scope}
                onChange={(v) => setScope(v === "all" ? "all" : "pending")}
                options={[
                  { value: "pending", label: "Waiting for approval" },
                  { value: "all", label: "All requests" },
                ]}
              />
            </Field>
            <Button
              size="sm"
              variant="secondary"
              icon={<RotateCw className="size-4" />}
              loading={list.loading && list.data !== undefined}
              onClick={() => {
                setNow(new Date());
                list.reload();
              }}
            >
              Refresh
            </Button>
          </>
        }
      />
      <ResourceView resource={list} label="pending changes">
        {(rows) => {
          const sorted = sortRequests(rows, now);
          const shown = scope === "pending" ? sorted.filter((r) => effectiveStatus(r, now) === "pending") : sorted;
          if (shown.length === 0) {
            return (
              <EmptyState
                icon={<Inbox />}
                title={scope === "pending" ? "Nothing waiting for approval" : "No change requests"}
                description="When a tenant admin saves a guarded change (raising an automatic-apply limit, reclassifying a write tool as read, granting an admin role…), it waits here for a second admin."
              />
            );
          }
          return (
            <ul aria-label="Change requests" className="flex flex-col gap-3">
              {shown.map((r) => (
                <li key={r.id}>
                  <RequestCard request={r} now={now} highlighted={highlight === r.id} onDecide={(decision) => setDeciding({ request: r, decision })} />
                </li>
              ))}
            </ul>
          );
        }}
      </ResourceView>
      {deciding ? (
        <DecideDialog
          request={deciding.request}
          decision={deciding.decision}
          own={deciding.request.requested_by.sub === me.user.sub}
          onOpenChange={(open) => (open ? undefined : setDeciding(null))}
          onDecided={(updated) => {
            if (updated) list.setData((prev) => prev?.map((x) => (x.id === updated.id ? updated : x)));
            setNow(new Date());
            list.reload();
          }}
        />
      ) : null}
    </>
  );
}
