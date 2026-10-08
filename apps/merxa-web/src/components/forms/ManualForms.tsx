"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent, type ReactNode } from "react";

import { formatInt } from "@/lib/format";
import { parseQty, saveErrorMessage, seg, transferOrderBody, versionNotice } from "@/lib/manual-change";
import type { SkuQty } from "@/lib/params";
import type {
  Allocation,
  AllocationUpdated,
  BookingChanged,
  CapacityBookingDetail,
  PoLine,
  PoLineUpdated,
  TransferOrderCreated,
} from "@/lib/types";
import { useIdempotentSave } from "./useIdempotentSave";

type Outcome = { tone: "ok" | "error"; text: ReactNode };

function OutcomeBanner({ outcome }: { outcome: Outcome | null }) {
  if (!outcome) return null;
  return (
    <div className={`mx-banner ${outcome.tone === "ok" ? "mx-banner--info" : "mx-banner--danger"}`} role={outcome.tone === "ok" ? "status" : "alert"}>
      <div className="mx-banner__body">{outcome.text}</div>
    </div>
  );
}

function Warning({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <div className="mx-banner mx-banner--warning" role="note">
      <div className="mx-banner__body">{text}</div>
    </div>
  );
}

/** Reload server data after a 412 so the next attempt sends the current version. */
function useFail(setOutcome: (o: Outcome) => void) {
  const router = useRouter();
  return (err: unknown) => {
    setOutcome({ tone: "error", text: saveErrorMessage(err) });
    if (err && typeof err === "object" && "status" in err && (err as { status: number }).status === 412) router.refresh();
  };
}

// ───────────────────────── transfer order ─────────────────────────
export function CreateTransferOrder({
  from,
  to,
  shipDate,
  lines,
  problem,
}: {
  from: string;
  to: string;
  shipDate: string;
  lines: readonly SkuQty[];
  /** Why the planner cannot submit yet (null = ready). */
  problem: string | null;
}) {
  const router = useRouter();
  const { save, busy } = useIdempotentSave();
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [created, setCreated] = useState<TransferOrderCreated | null>(null);
  const fail = useFail(setOutcome);

  async function submit() {
    setOutcome(null);
    try {
      const res = await save<TransferOrderCreated>("/v1/transfer-orders", {
        method: "POST",
        body: transferOrderBody(from, to, shipDate, lines),
      });
      setCreated(res);
      setOutcome({
        tone: "ok",
        text: (
          <>
            Transfer order{" "}
            <Link className="mx-mono" href={`/transfer-orders?code=${seg(res.code)}`}>
              {res.code}
            </Link>{" "}
            created: {formatInt(res.units)} units {res.from_dc} → {res.to_dc}, status {res.status}.
          </>
        ),
      });
      router.refresh();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <div className="mx-form" role="group" aria-label="Create transfer order">
      <OutcomeBanner outcome={outcome} />
      {problem && !created && <p className="mx-muted">{problem}</p>}
      <div>
        <button
          type="button"
          className="mx-btn mx-btn--primary"
          disabled={busy || problem !== null || created !== null}
          onClick={() => void submit()}
        >
          {busy ? "Creating…" : created ? `Created ${created.code}` : "Create transfer order"}
        </button>
      </div>
    </div>
  );
}

// ───────────────────────── allocation ─────────────────────────
export function AllocationQtyForm({
  allocation,
  initialQty,
  planned,
}: {
  allocation: Allocation;
  /** Pre-filled from the guide link (`?qty=`), else the current quantity. */
  initialQty: number | null;
  planned: number | null;
}) {
  const router = useRouter();
  const { save, busy } = useIdempotentSave();
  const [qty, setQty] = useState(String(initialQty ?? allocation.qty));
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const fail = useFail(setOutcome);
  const parsed = parseQty(qty);
  const editable = allocation.status === "planned" || allocation.status === "released";

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (parsed === null) return;
    setOutcome(null);
    try {
      const res = await save<AllocationUpdated>(`/v1/allocations/${seg(allocation.code)}`, {
        method: "PATCH",
        body: { qty: parsed },
        ifMatch: allocation.version,
      });
      setOutcome({ tone: "ok", text: `Saved: quantity ${formatInt(res.qty_before)} → ${formatInt(res.qty)} (version ${res.version}).` });
      router.refresh();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <form className="mx-form" onSubmit={(e) => void submit(e)} aria-label="Edit allocation quantity">
      <Warning text={versionNotice(planned, allocation.version)} />
      {!editable && <Warning text={`Allocations in status ${allocation.status} cannot be changed.`} />}
      <OutcomeBanner outcome={outcome} />
      <div className="mx-toolbar">
        <label className="mx-field">
          <span className="mx-field__label">Quantity (current {formatInt(allocation.qty)})</span>
          <input
            className="mx-input"
            inputMode="numeric"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            aria-invalid={parsed === null}
            required
          />
        </label>
        <button type="submit" className="mx-btn mx-btn--primary" disabled={busy || parsed === null || !editable}>
          {busy ? "Saving…" : "Save quantity"}
        </button>
      </div>
      {parsed === null && <p className="mx-muted">Enter a whole number (0 or more).</p>}
    </form>
  );
}

// ───────────────────────── PO line ─────────────────────────
export type PoLineAction = "reschedule" | "change-qty";

export function PoLineEditForm({
  poCode,
  line,
  action,
  initialEta,
  initialQty,
  planned,
}: {
  poCode: string;
  line: PoLine;
  action: PoLineAction;
  initialEta: string | null;
  initialQty: number | null;
  planned: number | null;
}) {
  const router = useRouter();
  const { save, busy } = useIdempotentSave();
  const [mode, setMode] = useState<PoLineAction>(action);
  const [eta, setEta] = useState(initialEta ?? line.eta_dc ?? "");
  const [qty, setQty] = useState(String(initialQty ?? line.qty_ordered));
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const fail = useFail(setOutcome);
  const parsedQty = parseQty(qty);
  const valid = mode === "reschedule" ? /^\d{4}-\d{2}-\d{2}$/.test(eta) : parsedQty !== null && parsedQty >= line.qty_received;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!valid) return;
    setOutcome(null);
    const body = mode === "reschedule" ? { eta_dc: eta } : { qty_ordered: parsedQty };
    try {
      const res = await save<PoLineUpdated>(`/v1/purchase-orders/${seg(poCode)}/lines/${line.line_no}`, {
        method: "PATCH",
        body,
        ifMatch: line.version,
      });
      setOutcome({
        tone: "ok",
        text:
          res.action === "reschedule"
            ? `Saved: line ${line.line_no} now arrives ${res.eta_dc} (version ${res.version}).`
            : `Saved: line ${line.line_no} ordered quantity is ${formatInt(res.qty_ordered)} (version ${res.version}).`,
      });
      router.refresh();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <form className="mx-form" onSubmit={(e) => void submit(e)} aria-label={`Edit line ${line.line_no}`}>
      <Warning text={versionNotice(planned, line.version)} />
      <OutcomeBanner outcome={outcome} />
      <div className="mx-toolbar">
        <label className="mx-field">
          <span className="mx-field__label">Change</span>
          <select className="mx-select" value={mode} onChange={(e) => setMode(e.target.value as PoLineAction)}>
            <option value="reschedule">Reschedule (ETA DC)</option>
            <option value="change-qty">Change ordered quantity</option>
          </select>
        </label>
        {mode === "reschedule" ? (
          <label className="mx-field">
            <span className="mx-field__label">New ETA DC (now {line.eta_dc ?? "not set"})</span>
            <input className="mx-input" type="date" value={eta} onChange={(e) => setEta(e.target.value)} required />
          </label>
        ) : (
          <label className="mx-field">
            <span className="mx-field__label">Ordered quantity (now {formatInt(line.qty_ordered)})</span>
            <input
              className="mx-input"
              inputMode="numeric"
              value={qty}
              onChange={(e) => setQty(e.target.value)}
              aria-invalid={!valid}
              required
            />
          </label>
        )}
        <button type="submit" className="mx-btn mx-btn--primary" disabled={busy || !valid}>
          {busy ? "Saving…" : `Save line ${line.line_no}`}
        </button>
      </div>
      {mode === "change-qty" && !valid && (
        <p className="mx-muted">Enter a whole number of at least {formatInt(line.qty_received)} (already received).</p>
      )}
    </form>
  );
}

// ───────────────────────── capacity bookings ─────────────────────────
export type LaneChoice = { code: string; label: string };

export function CapacityBookingForm({
  lanes,
  initialLane,
  initialWeek,
  initialQty,
  remainingByLaneWeek,
}: {
  lanes: readonly LaneChoice[];
  initialLane: string;
  initialWeek: string;
  initialQty: number | null;
  /** `<lane>|<week>` → remaining units (computed server-side like merxa-api). */
  remainingByLaneWeek: Readonly<Record<string, number | null>>;
}) {
  const router = useRouter();
  const { save, busy } = useIdempotentSave();
  const [lane, setLane] = useState(initialLane);
  const [week, setWeek] = useState(initialWeek);
  const [qty, setQty] = useState(initialQty === null ? "" : String(initialQty));
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const fail = useFail(setOutcome);
  const parsed = parseQty(qty, { min: 1 });
  const weekOk = /^\d{4}-W\d{2}$/.test(week);
  const known = Object.prototype.hasOwnProperty.call(remainingByLaneWeek, `${lane}|${week}`);
  const remaining = known ? remainingByLaneWeek[`${lane}|${week}`] : undefined;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (parsed === null || !lane || !weekOk) return;
    setOutcome(null);
    try {
      const res = await save<BookingChanged>("/v1/capacity-bookings", {
        method: "POST",
        body: { lane_code: lane, iso_week: week, qty_units: parsed },
      });
      setOutcome({
        tone: "ok",
        text: (
          <>
            Booked{" "}
            <Link className="mx-mono" href={`/capacity-bookings/${seg(res.code)}`}>
              {res.code}
            </Link>
            : {formatInt(res.qty_units)} units on {res.lane_code} in {res.iso_week} ({res.status}).
          </>
        ),
      });
      router.refresh();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <form className="mx-form" onSubmit={(e) => void submit(e)} aria-label="Book lane capacity">
      <OutcomeBanner outcome={outcome} />
      <div className="mx-toolbar">
        <label className="mx-field">
          <span className="mx-field__label">Lane</span>
          <select className="mx-select" value={lane} onChange={(e) => setLane(e.target.value)} required>
            <option value="">Select…</option>
            {lanes.map((l) => (
              <option key={l.code} value={l.code}>
                {l.label}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">ISO week</span>
          <input
            className="mx-input mx-mono"
            value={week}
            onChange={(e) => setWeek(e.target.value.trim().toUpperCase())}
            placeholder="2026-W44"
            aria-invalid={!weekOk}
            required
          />
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Units</span>
          <input
            className="mx-input"
            inputMode="numeric"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            aria-invalid={parsed === null}
            required
          />
        </label>
        <button type="submit" className="mx-btn mx-btn--primary" disabled={busy || parsed === null || !lane || !weekOk}>
          {busy ? "Booking…" : "Book capacity"}
        </button>
      </div>
      <p className="mx-muted" aria-live="polite">
        {remaining === undefined
          ? "Remaining capacity is checked by Merxa when you book."
          : remaining === null
            ? "No rate card offers capacity on this lane in that week."
            : `Remaining capacity in ${week}: ${formatInt(remaining)} units.`}
      </p>
    </form>
  );
}

export function CancelBookingForm({ booking, planned }: { booking: CapacityBookingDetail; planned: number | null }) {
  const router = useRouter();
  const { save, busy } = useIdempotentSave();
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const fail = useFail(setOutcome);
  const cancellable = booking.status === "held" || booking.status === "confirmed";
  const linked = booking.shipments.filter((s) => s.status !== "cancelled");

  async function cancel() {
    const warn = linked.length ? ` It is referenced by shipment(s) ${linked.map((s) => s.code).join(", ")}.` : "";
    if (!window.confirm(`Cancel capacity booking ${booking.code} (${formatInt(booking.qty_units)} units)?${warn}`)) return;
    setOutcome(null);
    try {
      const res = await save<BookingChanged>(`/v1/capacity-bookings/${seg(booking.code)}:cancel`, {
        method: "POST",
        ifMatch: booking.version,
      });
      setOutcome({ tone: "ok", text: `Booking ${res.code} cancelled (status ${res.status}, version ${res.version}).` });
      router.refresh();
    } catch (err) {
      fail(err);
    }
  }

  return (
    <div className="mx-form" role="group" aria-label="Cancel booking">
      <Warning text={versionNotice(planned, booking.version)} />
      <OutcomeBanner outcome={outcome} />
      {!cancellable && <p className="mx-muted">This booking is {booking.status}; there is nothing to cancel.</p>}
      <div>
        <button type="button" className="mx-btn mx-btn--danger" disabled={busy || !cancellable} onClick={() => void cancel()}>
          {busy ? "Cancelling…" : "Cancel booking"}
        </button>
      </div>
    </div>
  );
}
