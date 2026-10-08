import Link from "next/link";

import { formatDate, formatInt, humanize } from "@/lib/format";
import { daysBetween, limitTracks, placeOrders, poLineHref, weekOf } from "@/lib/m2";
import type { CalendarDay, FactorySchedule } from "@/lib/types";
import { Card, EmptyState } from "../ui/Card";
import { StatusBadge } from "../ui/StatusBadge";

const WEEKDAY = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
/** Order tracks drawn per line; further overlapping orders are summarised as "+N" per day. */
export const MAX_TRACKS = 6;

function weekday(day: string): string {
  return WEEKDAY[new Date(`${day}T00:00:00Z`).getUTCDay()] ?? "";
}

/** Consecutive days grouped by ISO week: [{week, start (1-based column), span}]. */
export function weekSegments(days: readonly string[]): { week: string; start: number; span: number }[] {
  const out: { week: string; start: number; span: number }[] = [];
  days.forEach((d, i) => {
    const w = weekOf(d) ?? "";
    const last = out[out.length - 1];
    if (last && last.week === w) last.span += 1;
    else out.push({ week: w, start: i + 1, span: 1 });
  });
  return out;
}

/**
 * Factory schedule: one card per factory line with a calendar strip (open / reduced /
 * closed days from the line calendar), production orders as bars on the strip (linked to
 * their PO line) and the order list.
 */
export function ProductionSchedule({ schedule, today }: { schedule: FactorySchedule; today: string | null }) {
  const days = daysBetween(schedule.from, schedule.to);
  if (schedule.lines.length === 0) {
    return (
      <div className="mx-card">
        <EmptyState title="No production lines">This factory has no lines in Merxa.</EmptyState>
      </div>
    );
  }
  const segments = weekSegments(days);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--mx-gap)" }}>
      {schedule.lines.map((line) => {
        const byDay = new Map<string, CalendarDay>(line.calendar.map((c) => [c.day, c]));
        const { visible: placed, hiddenPerDay } = limitTracks(placeOrders(line.orders, days), MAX_TRACKS, days.length);
        const overflow = hiddenPerDay.some((n) => n > 0);
        const tracks = Math.max(1, ...placed.map((p) => p.track)) + (overflow ? 1 : 0);
        const held = line.orders.filter((o) => o.status === "held");
        const closedDays = line.calendar.filter((c) => c.status === "closed").length;
        return (
          <Card
            key={line.code}
            title={
              <>
                <span className="mx-mono">{line.code}</span>{" "}
                <span className="mx-muted" style={{ fontWeight: 400 }}>
                  {line.capabilities.join(", ")}
                </span>
              </>
            }
            meta={`${formatInt(line.std_minutes_day)} std min/day · ${line.orders.length} order${line.orders.length === 1 ? "" : "s"} · ${closedDays} closed day${closedDays === 1 ? "" : "s"}`}
            flush
          >
            {held.length > 0 && (
              <div className="mx-banner mx-banner--warning" role="note" style={{ margin: 8 }}>
                <div className="mx-banner__body">
                  <div className="mx-banner__title">
                    {held.length} production order{held.length === 1 ? " is" : "s are"} on hold
                  </div>
                  {held.map((o) => (
                    <div key={o.code}>
                      <span className="mx-mono">{o.code}</span> ({o.po_line_ref}): {o.hold_reason || "no reason given"}
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="mx-strip" role="group" aria-label={`Calendar of ${line.code}, ${formatDate(schedule.from)} to ${formatDate(schedule.to)}`}>
              <div
                className="mx-strip__grid"
                style={{ gridTemplateColumns: `repeat(${days.length}, minmax(22px, 1fr))`, gridTemplateRows: `20px 30px repeat(${tracks}, 22px)` }}
              >
                {segments.map((s) => (
                  <div key={s.week} className="mx-strip__week" style={{ gridColumn: `${s.start} / span ${s.span}`, gridRow: 1 }}>
                    {s.span >= 3 ? s.week : s.week.slice(-3)}
                  </div>
                ))}
                {days.map((d, i) => {
                  const c = byDay.get(d);
                  const status = c?.status ?? "unknown";
                  const label = c
                    ? `${weekday(d)} ${formatDate(d)}: ${status}, ${formatInt(c.available_minutes)} min available`
                    : `${weekday(d)} ${formatDate(d)}: no calendar`;
                  return (
                    <div
                      key={d}
                      className={`mx-strip__day mx-strip__day--${status}${d === today ? " mx-strip__day--today" : ""}`}
                      style={{ gridColumn: i + 1, gridRow: 2 }}
                      title={label}
                      data-day={d}
                      data-status={status}
                      /* aria-label is not allowed on a role-less div: as an image the label is announced. */
                      role="img"
                      aria-label={label}
                      aria-current={d === today ? "date" : undefined}
                    >
                      <span className="mx-strip__dow">{weekday(d).charAt(0)}</span>
                      <span>{Number(d.slice(8))}</span>
                    </div>
                  );
                })}
                {placed.map((p) => {
                  const href = poLineHref(p.order.po_line_ref);
                  const title = `${p.order.code} · ${p.order.po_line_ref} · ${formatDate(p.order.planned_start)} – ${formatDate(p.order.planned_end)} · ${humanize(p.order.status)}${p.order.hold_reason ? ` · ${p.order.hold_reason}` : ""}`;
                  const cls = `mx-strip__bar mx-strip__bar--${p.order.status}${p.clippedStart ? " is-clipped-start" : ""}${p.clippedEnd ? " is-clipped-end" : ""}`;
                  const style = { gridColumn: `${p.colStart} / ${p.colEnd}`, gridRow: p.track + 2 };
                  return href ? (
                    <Link key={p.order.code} href={href} className={cls} style={style} title={title} data-order={p.order.code} data-cols={`${p.colStart}/${p.colEnd}`}>
                      {p.order.code}
                    </Link>
                  ) : (
                    <span key={p.order.code} className={cls} style={style} title={title} data-order={p.order.code} data-cols={`${p.colStart}/${p.colEnd}`}>
                      {p.order.code}
                    </span>
                  );
                })}
                {hiddenPerDay.map((n, i) =>
                  n > 0 ? (
                    <span
                      key={`more-${days[i]}`}
                      className="mx-strip__more"
                      style={{ gridColumn: i + 1, gridRow: tracks + 2 }}
                      title={`${n} more order${n === 1 ? "" : "s"} on ${formatDate(days[i])}; see the order list below`}
                      data-more-day={days[i]}
                    >
                      +{n}
                      <span className="mx-visually-hidden"> more on {formatDate(days[i])}</span>
                    </span>
                  ) : null,
                )}
              </div>
            </div>
            {line.orders.length > 0 && (
              <details className="mx-strip__details">
                <summary>Production orders ({line.orders.length})</summary>
                <div className="mx-table-wrap">
                  <table className="mx-table mx-table--tight">
                    <thead>
                      <tr>
                        <th scope="col" className="mx-num">
                          Seq
                        </th>
                        <th scope="col">Order</th>
                        <th scope="col">PO line</th>
                        <th scope="col">Start</th>
                        <th scope="col">End</th>
                        <th scope="col">Status</th>
                        <th scope="col">Hold reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {line.orders.map((o) => {
                        const href = poLineHref(o.po_line_ref);
                        return (
                          <tr key={o.code}>
                            <td className="mx-num mx-mono">{o.seq}</td>
                            <td className="mx-mono">{o.code}</td>
                            <td className="mx-mono">{href ? <Link href={href}>{o.po_line_ref}</Link> : o.po_line_ref}</td>
                            <td>{formatDate(o.planned_start)}</td>
                            <td>{formatDate(o.planned_end)}</td>
                            <td>
                              <StatusBadge status={o.status} />
                            </td>
                            <td>{o.hold_reason ?? "—"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </details>
            )}
          </Card>
        );
      })}
      <div className="mx-strip__legend mx-muted" aria-label="Legend">
        <span>
          <i className="mx-strip__swatch mx-strip__day--open" /> Open
        </span>
        <span>
          <i className="mx-strip__swatch mx-strip__day--reduced" /> Reduced
        </span>
        <span>
          <i className="mx-strip__swatch mx-strip__day--closed" /> Closed
        </span>
        <span>
          <i className="mx-strip__swatch mx-strip__bar--planned" /> Planned
        </span>
        <span>
          <i className="mx-strip__swatch mx-strip__bar--running" /> Running
        </span>
        <span>
          <i className="mx-strip__swatch mx-strip__bar--done" /> Done
        </span>
        <span>
          <i className="mx-strip__swatch mx-strip__bar--held" /> Held
        </span>
      </div>
    </div>
  );
}
