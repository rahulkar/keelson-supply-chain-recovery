import { basisUnit, formatInt, modeLabel } from "@/lib/format";
import { utilisation, type CapacityRow } from "@/lib/m2";
import { EmptyState } from "../ui/Card";

const LABEL = { ok: "Available", tight: "Tight (80% or more booked)", full: "Fully booked", none: "No rate card" } as const;

/** Lanes × ISO weeks: remaining weekly capacity (rate card capacity minus active bookings). */
export function CapacityTable({
  rows,
  weeks,
  currentWeek,
  bookingsUnavailable = false,
}: {
  rows: CapacityRow[];
  weeks: string[];
  currentWeek: string | null;
  /** The capacity bookings could not be (fully) read: remaining capacity is unknown. */
  bookingsUnavailable?: boolean;
}) {
  if (bookingsUnavailable) {
    // Showing the rate-card capacity as "remaining" would overstate what can still be booked.
    return (
      <div className="mx-banner mx-banner--warning" role="note">
        <div className="mx-banner__body">
          Remaining lane capacity cannot be shown because the capacity bookings could not be fully loaded. Reload the page to try
          again.
        </div>
      </div>
    );
  }
  if (rows.length === 0) {
    return (
      <div className="mx-card">
        <EmptyState title="No lane capacity in this window">No rate card is valid in the selected weeks.</EmptyState>
      </div>
    );
  }
  return (
    <div className="mx-card mx-table-wrap">
      <table className="mx-table mx-table--tight mx-capacity" aria-label="Weekly lane capacity">
        <thead>
          <tr>
            <th scope="col">Lane</th>
            <th scope="col">Mode</th>
            <th scope="col">Unit</th>
            {weeks.map((w) => (
              <th key={w} scope="col" className="mx-num" aria-current={w === currentWeek ? "date" : undefined}>
                {w}
                {w === currentWeek && <span className="mx-capacity__now"> · now</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ lane, weeks: cells }) => {
            const unit = basisUnit(cells.find((c) => c.rate)?.rate?.basis);
            return (
              <tr key={lane.code}>
                <th scope="row" className="mx-mono">
                  {lane.code}
                  <span className="mx-muted mx-capacity__route">
                    {lane.origin} → {lane.dest}
                  </span>
                </th>
                <td>{modeLabel(lane.mode)}</td>
                <td>{unit}</td>
                {cells.map((c) => {
                  const state = utilisation(c);
                  const used = c.capacity && c.remaining !== null ? Math.min(100, Math.max(0, ((c.capacity - c.remaining) / c.capacity) * 100)) : 0;
                  return (
                    <td
                      key={c.week}
                      className={`mx-num mx-capacity__cell mx-capacity__cell--${state}`}
                      title={
                        c.capacity === null
                          ? LABEL.none
                          : `${LABEL[state]}: ${formatInt(c.remaining)} of ${formatInt(c.capacity)} ${unit} left` +
                            (c.booked ? ` (${formatInt(c.booked)} booked)` : "")
                      }
                      data-state={state}
                    >
                      {c.capacity === null ? (
                        <>
                          <span aria-hidden="true">—</span>
                          <span className="mx-visually-hidden">{LABEL.none}</span>
                        </>
                      ) : (
                        <>
                          <span className="mx-capacity__value">{formatInt(c.remaining)}</span>
                          <span className="mx-muted"> / {formatInt(c.capacity)}</span>
                          {/* The state is otherwise only colour and a hover title. */}
                          {state !== "ok" && <span className="mx-visually-hidden"> {LABEL[state]}</span>}
                          <span className="mx-capacity__bar" aria-hidden="true">
                            <span style={{ width: `${used}%` }} />
                          </span>
                        </>
                      )}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
