"use client";

import { IconAlertTriangle, IconMail, IconMailOpened } from "@tabler/icons-react";
import { useRouter } from "next/navigation";
import { useCallback, useMemo, useState } from "react";

import { apiFetch } from "@/lib/client-api";
import { formatDateTime } from "@/lib/format";
import { ApiError, errorMessage } from "@/lib/http";
import type { SupplierMessage } from "@/lib/types";
import { EmptyState } from "../ui/Card";
import { StatusBadge } from "../ui/StatusBadge";

/**
 * Supplier-portal inbox (list + reading pane). Message bodies are untrusted free text from
 * external parties: always rendered as plain text (never HTML/Markdown, no auto-links).
 */
export function SupplierInbox({
  messages,
  supplierNames,
  canMarkRead,
  initialSelected,
}: {
  messages: SupplierMessage[];
  supplierNames: Record<string, string>;
  canMarkRead: boolean;
  initialSelected: string | null;
}) {
  const router = useRouter();
  const [items, setItems] = useState(messages);
  // After `router.refresh()` the server list (current versions) replaces the local one.
  const [serverMessages, setServerMessages] = useState(messages);
  if (serverMessages !== messages) {
    setServerMessages(messages);
    setItems(messages);
  }
  const [selectedId, setSelectedId] = useState<string | null>(initialSelected ?? messages[0]?.id ?? null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const selected = useMemo(() => items.find((m) => m.id === selectedId) ?? null, [items, selectedId]);

  const setStatus = useCallback(
    async (msg: SupplierMessage, status: "new" | "read") => {
      if (!canMarkRead || msg.status === status) return;
      setError(null);
      setBusy(true);
      try {
        const updated = await apiFetch<SupplierMessage>(`/v1/supplier-messages/${encodeURIComponent(msg.id)}`, {
          method: "PATCH",
          body: { status },
          ifMatch: msg.version,
        });
        setItems((prev) => prev.map((m) => (m.id === msg.id ? { ...m, status: updated.status, version: updated.version } : m)));
      } catch (err) {
        if (err instanceof ApiError && (err.status === 412 || err.status === 404)) {
          // Changed (or removed) elsewhere: reload so the next attempt sends the current version
          // instead of failing with the stale one forever.
          setError(
            err.status === 412
              ? "The message was changed elsewhere. The inbox has been reloaded: try again."
              : "The message no longer exists. The inbox has been reloaded.",
          );
          router.refresh();
        } else {
          setError(errorMessage(err));
        }
      } finally {
        setBusy(false);
      }
    },
    [canMarkRead, router],
  );

  // Opening an unread message marks it read (optimistic concurrency via If-Match).
  const open = useCallback(
    (msg: SupplierMessage) => {
      setSelectedId(msg.id);
      if (msg.status === "new") void setStatus(msg, "read");
    },
    [setStatus],
  );

  if (items.length === 0) {
    return (
      <div className="mx-card">
        <EmptyState title="No supplier messages">Messages from supplier portals and EDI mailboxes appear here.</EmptyState>
      </div>
    );
  }

  const unread = items.filter((m) => m.status === "new").length;
  return (
    <>
      <p className="mx-muted" style={{ margin: 0 }} aria-live="polite" data-testid="inbox-count">
        {items.length} message{items.length === 1 ? "" : "s"} · {unread} unread
      </p>
      <div className="mx-card mx-inbox">
        <ul className="mx-inbox__list" aria-label="Messages">
          {items.map((m) => (
            <li key={m.id}>
              <button
                type="button"
                className={`mx-inbox__row${m.status === "new" ? " mx-inbox__row--new" : ""}`}
                aria-current={m.id === selectedId ? "true" : undefined}
                onClick={() => open(m)}
              >
                <span className="mx-inbox__top">
                  <span>
                    {m.status === "new" ? <IconMail size={13} aria-label="Unread" /> : <IconMailOpened size={13} aria-label="Read" />}{" "}
                    {supplierNames[m.supplier_code] ?? m.supplier_code}
                  </span>
                  <span>{formatDateTime(m.received_at)}</span>
                </span>
                <span className="mx-inbox__subject" style={{ display: "block" }}>
                  {m.subject}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <article className="mx-inbox__reader" aria-label="Message">
          {selected ? (
            <>
              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <h2 style={{ fontSize: "var(--mx-text-lg)", flex: 1 }}>{selected.subject}</h2>
                <StatusBadge status={selected.status} label={selected.status === "new" ? "Unread" : "Read"} />
                {/* The message shown first (or opened by a link) is not marked read automatically. */}
                {canMarkRead && (
                  <button
                    type="button"
                    className="mx-btn mx-btn--sm"
                    disabled={busy}
                    onClick={() => void setStatus(selected, selected.status === "new" ? "read" : "new")}
                  >
                    {selected.status === "new" ? "Mark read" : "Mark unread"}
                  </button>
                )}
              </div>
              <div className="mx-muted" style={{ marginTop: 4 }}>
                From <strong>{supplierNames[selected.supplier_code] ?? selected.supplier_code}</strong>{" "}
                <span className="mx-mono">({selected.supplier_code})</span> · received {formatDateTime(selected.received_at)}
              </div>
              <div className="mx-banner mx-banner--warning" role="note" style={{ marginTop: 12 }}>
                <span className="mx-banner__icon" aria-hidden="true">
                  <IconAlertTriangle size={16} />
                </span>
                <div className="mx-banner__body">
                  External message: content is supplied by the sender and has not been verified by Merxa.
                </div>
              </div>
              <pre className="mx-message-body">{selected.body}</pre>
              {error && (
                <div className="mx-banner mx-banner--danger" role="alert" style={{ marginTop: 12 }}>
                  Could not update the message status: {error}
                </div>
              )}
            </>
          ) : (
            <EmptyState title="Select a message" />
          )}
        </article>
      </div>
    </>
  );
}
