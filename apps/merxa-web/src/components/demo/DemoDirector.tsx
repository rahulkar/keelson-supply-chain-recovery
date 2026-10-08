"use client";

import { IconClockPlay, IconMailForward, IconRefresh, IconShip, IconTruckDelivery } from "@tabler/icons-react";
import { useRouter } from "next/navigation";
import { useRef, useState, type ReactNode } from "react";

import { apiFetch } from "@/lib/client-api";
import { errorMessage } from "@/lib/http";
import { idempotencyKeyFor, isRetryable, parseQty, type PendingAttempt } from "@/lib/manual-change";
import { Card } from "../ui/Card";

// Shared with the Guide-mode forms (lib/manual-change); re-exported for existing callers.
export { idempotencyKeyFor, isRetryable, type PendingAttempt };

type Outcome = { tone: "ok" | "error"; text: string };

/** "30" (%) -> 3000 basis points; whole percentages 0..1000 only. */
export function percentToBp(text: string): number | null {
  const pct = parseQty(text);
  return pct === null || pct > 1000 ? null : pct * 100;
}

function describe(result: unknown): string {
  if (!result || typeof result !== "object") return "Done.";
  const r = result as Record<string, unknown>;
  if (r.status === "reset") return `Scenario reset (profile ${String(r.profile ?? "")}).`;
  if (r.kind === "supplier_message") return "Supplier message delivered to the Supplier Portal.";
  if (r.kind === "po_eta_change") return `ETA moved on ${String(r.po_code)} (${Array.isArray(r.lines) ? r.lines.length : 0} line(s)).`;
  if (r.kind === "lane_capacity")
    return `Capacity on ${String(r.lane_code)} set to ${Number(r.factor_bp) / 100}% for ${String(r.from_week)}–${String(r.to_week)} (now ${String(r.effective_capacity_units)} units/week).`;
  if (typeof r.today === "string") return `Simulation clock is now ${r.today} (${String(r.iso_week ?? "")}).`;
  return "Done.";
}

/** Demo-director actions (`POST /v1/admin/scenario:*`, each with a fresh Idempotency-Key). */
export function DemoDirector({ openPoCodes, laneCodes = [] }: { openPoCodes: string[]; laneCodes?: string[] }) {
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [days, setDays] = useState(7);
  const [poCode, setPoCode] = useState(openPoCodes[0] ?? "");
  const [poDays, setPoDays] = useState(7);
  const [lane, setLane] = useState(laneCodes[0] ?? "");
  const [pct, setPct] = useState("30");
  const [laneWeeks, setLaneWeeks] = useState(4);
  const [note, setNote] = useState("");
  const factorBp = percentToBp(pct);

  // A retry of the same action after an ambiguous failure (network error, 5xx, request still
  // in flight) reuses its Idempotency-Key, so merxa-api replays instead of running it twice.
  const pending = useRef<PendingAttempt | null>(null);

  async function run(id: string, path: string, body: unknown, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(id);
    setOutcome(null);
    const key = idempotencyKeyFor(pending.current, path, body);
    try {
      const result = await apiFetch<unknown>(path, { method: "POST", body, idempotencyKey: key });
      pending.current = null;
      setOutcome({ tone: "ok", text: describe(result) });
      router.refresh();
    } catch (err) {
      pending.current = isRetryable(err) ? { path, body: JSON.stringify(body), key } : null;
      setOutcome({ tone: "error", text: errorMessage(err) });
    } finally {
      setBusy(null);
    }
  }

  const action = (id: string, icon: ReactNode, title: string, text: string, control: ReactNode) => (
    <Card title={title} key={id}>
      <div style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}>
        <span aria-hidden="true">{icon}</span>
        <p style={{ margin: 0, flex: 1, minWidth: 220 }} className="mx-muted">
          {text}
        </p>
        {control}
      </div>
    </Card>
  );

  return (
    <>
      {outcome && (
        <div className={`mx-banner ${outcome.tone === "ok" ? "mx-banner--info" : "mx-banner--danger"}`} role="status">
          {outcome.text}
        </div>
      )}
      <div className="mx-grid-2">
        {action(
          "trim",
          <IconMailForward size={22} />,
          "Inject trim delay",
          "Delivers the 'trims delayed 7 days' email from Meghna Trims Ltd (TRIM-BD-01) to the Supplier Portal.",
          <button
            type="button"
            className="mx-btn mx-btn--primary"
            disabled={busy !== null}
            onClick={() => run("trim", "/v1/admin/scenario:inject", { kind: "trim_delay", payload: {} })}
          >
            Inject
          </button>,
        )}
        {action(
          "eta",
          <IconTruckDelivery size={22} />,
          "Delay a purchase order",
          "Moves the ETA of every open line on the order by the given number of days.",
          <>
            <label className="mx-field">
              <span className="mx-field__label">PO</span>
              <select className="mx-select" value={poCode} onChange={(e) => setPoCode(e.target.value)}>
                {openPoCodes.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="mx-field">
              <span className="mx-field__label">Days</span>
              <input
                className="mx-input"
                type="number"
                min={-60}
                max={90}
                value={poDays}
                onChange={(e) => setPoDays(Number(e.target.value))}
                style={{ minWidth: 70, width: 80 }}
              />
            </label>
            <button
              type="button"
              className="mx-btn"
              disabled={busy !== null || !poCode || !Number.isInteger(poDays) || poDays === 0}
              onClick={() => run("eta", "/v1/admin/scenario:inject", { kind: "po_eta_change", payload: { po_code: poCode, days: poDays } })}
            >
              Apply delay
            </button>
          </>,
        )}
        {action(
          "lane",
          <IconShip size={22} />,
          "Cut lane capacity",
          "Scales the weekly capacity of a lane from the current week (e.g. sea-air to 30 %). Bookings stay; Keelson receives lane.capacity_changed.",
          <>
            <label className="mx-field">
              <span className="mx-field__label">Lane</span>
              <select className="mx-select" value={lane} onChange={(e) => setLane(e.target.value)}>
                {laneCodes.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <label className="mx-field">
              <span className="mx-field__label">Capacity %</span>
              <input
                className="mx-input"
                inputMode="numeric"
                value={pct}
                onChange={(e) => setPct(e.target.value)}
                aria-invalid={factorBp === null}
                style={{ minWidth: 70, width: 80 }}
              />
            </label>
            <label className="mx-field">
              <span className="mx-field__label">Weeks</span>
              <input
                className="mx-input"
                type="number"
                min={1}
                max={52}
                value={laneWeeks}
                onChange={(e) => setLaneWeeks(Number(e.target.value))}
                style={{ minWidth: 70, width: 80 }}
              />
            </label>
            <label className="mx-field">
              <span className="mx-field__label">Carrier note (optional)</span>
              <input className="mx-input" value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} />
            </label>
            <button
              type="button"
              className="mx-btn"
              disabled={busy !== null || !lane || factorBp === null || !Number.isInteger(laneWeeks) || laneWeeks < 1 || laneWeeks > 52}
              onClick={() =>
                run("lane", "/v1/admin/scenario:inject", {
                  kind: "lane_capacity",
                  payload: {
                    lane_code: lane,
                    factor_bp: factorBp,
                    weeks: laneWeeks,
                    ...(note.trim() ? { carrier_note: note.trim() } : {}),
                  },
                })
              }
            >
              Apply cut
            </button>
          </>,
        )}
        {action(
          "clock",
          <IconClockPlay size={22} />,
          "Advance the clock",
          "Moves the simulation date forward (1 to 90 days).",
          <>
            <label className="mx-field">
              <span className="mx-field__label">Days</span>
              <input
                className="mx-input"
                type="number"
                min={1}
                max={90}
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                style={{ minWidth: 70, width: 80 }}
              />
            </label>
            <button
              type="button"
              className="mx-btn"
              disabled={busy !== null || !Number.isInteger(days) || days < 1 || days > 90}
              onClick={() => run("clock", "/v1/admin/scenario:advance-clock", { days })}
            >
              Advance
            </button>
          </>,
        )}
        {action(
          "reset",
          <IconRefresh size={22} />,
          "Reset scenario",
          "Re-seeds every Merxa record from the golden dataset. The audit log is kept.",
          <button
            type="button"
            className="mx-btn mx-btn--danger"
            disabled={busy !== null}
            onClick={() =>
              run("reset", "/v1/admin/scenario:reset", {}, "Reset the Merxa demo scenario? All records return to the seed state.")
            }
          >
            Reset
          </button>,
        )}
      </div>
    </>
  );
}
