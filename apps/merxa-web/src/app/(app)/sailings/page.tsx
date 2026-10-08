import type { Metadata } from "next";
import Link from "next/link";

import { CapacityTable } from "@/components/logistics/CapacityTable";
import { BookingGrid, LaneGrid, RateCardGrid, SailingGrid, type LaneRow } from "@/components/logistics/LogisticsGrids";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { buildQuery } from "@/lib/http";
import { capacityMatrix, isIsoWeek, laneWeekCapacity, weekRange } from "@/lib/m2";
import { oneOf, param, type SearchParams } from "@/lib/params";
import { apiGetAll, getSimWeek, load } from "@/lib/server-api";
import type { CapacityBooking, Lane, RateCard, Sailing, Shipment } from "@/lib/types";

export const metadata: Metadata = { title: "Sailings & Lanes" };

const TABS = ["sailings", "lanes", "rates", "capacity"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABELS: Record<Tab, string> = {
  sailings: "Sailings",
  lanes: "Lanes",
  rates: "Rate cards",
  capacity: "Capacity",
};
const SAILING_STATUSES = ["scheduled", "departed", "arrived", "cancelled"] as const;
const MODES = ["sea", "sea_air", "air", "road"] as const;
const BOOKING_STATUSES = ["held", "confirmed", "released"] as const;
const CAPACITY_WEEKS = 8;

type Props = { searchParams: Promise<SearchParams> };

export default async function SailingsPage({ searchParams }: Props) {
  const sp = await searchParams;
  const tab: Tab = oneOf(sp, "tab", TABS) ?? "sailings";
  const laneCode = param(sp, "lane_code");
  const status = oneOf(sp, "status", SAILING_STATUSES);
  const mode = oneOf(sp, "mode", MODES);
  const bookingStatus = oneOf(sp, "booking_status", BOOKING_STATUSES);
  const weekParam = param(sp, "week");

  const [lanes, simWeek] = await Promise.all([load(apiGetAll<Lane>("/v1/lanes", { mode })), getSimWeek()]);
  const startWeek = isIsoWeek(weekParam) ? weekParam : simWeek;
  const laneItems = lanes.ok ? lanes.data.items : [];
  const tabHref = (t: Tab) => `/sailings${buildQuery({ tab: t })}`;

  return (
    <>
      <PageHeader
        title="Sailings & Lanes"
        subtitle={
          <>
            Transport network: scheduled sailings, lanes with carriers and transit times, rate cards and weekly capacity.
            {simWeek && (
              <>
                {" "}
                Current planning week <span className="mx-mono">{simWeek}</span>.
              </>
            )}
          </>
        }
      />
      <nav className="mx-tabs" aria-label="Logistics views">
        {TABS.map((t) => (
          <Link key={t} href={tabHref(t)} className="mx-tab" aria-current={tab === t ? "page" : undefined}>
            {TAB_LABELS[t]}
          </Link>
        ))}
      </nav>

      {tab === "sailings" && <SailingsTab laneCode={laneCode} status={status} lanes={laneItems} />}
      {tab === "lanes" &&
        (lanes.ok ? <LanesTab lanes={laneItems} mode={mode} week={simWeek} /> : <ProblemPanel error={lanes.error} what="lanes" />)}
      {tab === "rates" && <RatesTab laneCode={laneCode} lanes={laneItems} />}
      {tab === "capacity" &&
        (lanes.ok ? (
          <CapacityTab lanes={laneItems} startWeek={startWeek} simWeek={simWeek} laneCode={laneCode} bookingStatus={bookingStatus} />
        ) : (
          <ProblemPanel error={lanes.error} what="lanes" />
        ))}
    </>
  );
}

function LaneSelect({ lanes, value, modes }: { lanes: Lane[]; value: string | undefined; modes?: readonly string[] }) {
  return (
    <label className="mx-field">
      <span className="mx-field__label">Lane</span>
      <select className="mx-select" name="lane_code" defaultValue={value ?? ""}>
        <option value="">All lanes</option>
        {lanes
          .filter((l) => !modes || modes.includes(l.mode))
          .map((l) => (
            <option key={l.code} value={l.code}>
              {l.code} · {l.origin} → {l.dest}
            </option>
          ))}
      </select>
    </label>
  );
}

async function SailingsTab({ laneCode, status, lanes }: { laneCode?: string; status?: string; lanes: Lane[] }) {
  const [sailings, shipments] = await Promise.all([
    load(apiGetAll<Sailing>("/v1/sailings", { lane_code: laneCode, status })),
    load(apiGetAll<Shipment>("/v1/shipments", {}, { max: 5000 })),
  ]);
  const counts: Record<string, number> = {};
  for (const s of shipments.ok ? shipments.data.items : []) if (s.sailing_code) counts[s.sailing_code] = (counts[s.sailing_code] ?? 0) + 1;
  return (
    <>
      <form className="mx-toolbar" method="get" aria-label="Filter sailings">
        <input type="hidden" name="tab" value="sailings" />
        <LaneSelect lanes={lanes} value={laneCode} modes={["sea", "air"]} />
        <label className="mx-field">
          <span className="mx-field__label">Status</span>
          <select className="mx-select" name="status" defaultValue={status ?? ""}>
            <option value="">All statuses</option>
            {SAILING_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {(laneCode || status) && (
          <Link className="mx-btn mx-btn--ghost" href="/sailings">
            Reset
          </Link>
        )}
      </form>
      {sailings.ok ? (
        <SailingGrid rows={sailings.data.items} shipmentCounts={counts} />
      ) : (
        <ProblemPanel error={sailings.error} what="sailings" />
      )}
    </>
  );
}

async function LanesTab({ lanes, mode, week }: { lanes: Lane[]; mode?: string; week: string | null }) {
  const [rates, bookings] = await Promise.all([
    load(apiGetAll<RateCard>("/v1/rate-cards")),
    week ? load(apiGetAll<CapacityBooking>("/v1/capacity-bookings", { iso_week: week })) : Promise.resolve(null),
  ]);
  const rateItems = rates.ok ? rates.data.items : [];
  const bookingItems = bookings?.ok ? bookings.data.items : [];
  // Without the week's bookings the remaining capacity is unknown: never show the full
  // rate-card capacity as "remaining" when the bookings could not be read.
  const bookingsKnown = bookings?.ok === true && !bookings.data.truncated;
  const rows: LaneRow[] = lanes.map((lane) => {
    const c = week ? laneWeekCapacity(lane.code, week, rateItems, bookingItems) : null;
    return {
      ...lane,
      rate_cents: c?.rate?.rate_cents ?? null,
      basis: c?.rate?.basis ?? null,
      currency: c?.rate?.currency ?? null,
      weekly_capacity: c?.capacity ?? null,
      remaining: bookingsKnown ? (c?.remaining ?? null) : null,
    };
  });
  return (
    <>
      <form className="mx-toolbar" method="get" aria-label="Filter lanes">
        <input type="hidden" name="tab" value="lanes" />
        <label className="mx-field">
          <span className="mx-field__label">Mode</span>
          <select className="mx-select" name="mode" defaultValue={mode ?? ""}>
            <option value="">All modes</option>
            {MODES.map((m) => (
              <option key={m} value={m}>
                {m.replace("_", "-")}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {mode && (
          <Link className="mx-btn mx-btn--ghost" href="/sailings?tab=lanes">
            Reset
          </Link>
        )}
      </form>
      {!rates.ok && <ProblemPanel error={rates.error} what="rate cards" />}
      {bookings && !bookings.ok && <ProblemPanel error={bookings.error} what="capacity bookings (remaining capacity is not shown)" />}
      <LaneGrid rows={rows} week={week} />
    </>
  );
}

async function RatesTab({ laneCode, lanes }: { laneCode?: string; lanes: Lane[] }) {
  const rates = await load(apiGetAll<RateCard>("/v1/rate-cards", { lane_code: laneCode }));
  return (
    <>
      <form className="mx-toolbar" method="get" aria-label="Filter rate cards">
        <input type="hidden" name="tab" value="rates" />
        <LaneSelect lanes={lanes} value={laneCode} />
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {laneCode && (
          <Link className="mx-btn mx-btn--ghost" href="/sailings?tab=rates">
            Reset
          </Link>
        )}
      </form>
      {rates.ok ? <RateCardGrid rows={rates.data.items} lanes={lanes} /> : <ProblemPanel error={rates.error} what="rate cards" />}
    </>
  );
}

async function CapacityTab({
  lanes,
  startWeek,
  simWeek,
  laneCode,
  bookingStatus,
}: {
  lanes: Lane[];
  startWeek: string | null;
  simWeek: string | null;
  laneCode?: string;
  bookingStatus?: string;
}) {
  const weeks = startWeek ? weekRange(startWeek, CAPACITY_WEEKS) : [];
  const [rates, bookings] = await Promise.all([
    load(apiGetAll<RateCard>("/v1/rate-cards")),
    load(apiGetAll<CapacityBooking>("/v1/capacity-bookings", {}, { max: 10_000 })),
  ]);
  const bookingItems = bookings.ok ? bookings.data.items : [];
  // Lanes without a rate card in the window offer no capacity and are left out of the matrix.
  const shownLanes = laneCode ? lanes.filter((l) => l.code === laneCode) : lanes;
  const matrix = rates.ok ? capacityMatrix(shownLanes, weeks, rates.data.items, bookingItems) : [];
  const listed = bookingItems
    .filter((b) => (!laneCode || b.lane_code === laneCode) && (!bookingStatus || b.status === bookingStatus))
    .sort((a, b) => b.iso_week.localeCompare(a.iso_week) || a.code.localeCompare(b.code));
  return (
    <>
      <form className="mx-toolbar" method="get" aria-label="Capacity window">
        <input type="hidden" name="tab" value="capacity" />
        <label className="mx-field">
          <span className="mx-field__label">From week</span>
          <input
            className="mx-input mx-mono"
            name="week"
            defaultValue={startWeek ?? ""}
            placeholder="YYYY-Www"
            pattern="\d{4}-W\d{2}"
            maxLength={8}
            style={{ width: 110 }}
          />
        </label>
        <LaneSelect lanes={lanes} value={laneCode} />
        <label className="mx-field">
          <span className="mx-field__label">Booking status</span>
          <select className="mx-select" name="booking_status" defaultValue={bookingStatus ?? ""}>
            <option value="">All</option>
            {BOOKING_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="mx-btn">
          Show
        </button>
        <Link className="mx-btn mx-btn--ghost" href="/sailings?tab=capacity">
          Current week
        </Link>
        <Link className="mx-btn" href="/capacity-bookings/new">
          New booking
        </Link>
      </form>
      {!startWeek && (
        <div className="mx-banner mx-banner--info" role="note">
          <div className="mx-banner__body">Enter a start week (e.g. 2026-W41) to see weekly capacity.</div>
        </div>
      )}
      {rates.ok ? (
        weeks.length > 0 && (
          <CapacityTable
            rows={matrix}
            weeks={weeks}
            currentWeek={simWeek}
            // Remaining = capacity − bookings: unknown when the bookings are missing or cut off.
            bookingsUnavailable={!bookings.ok || bookings.data.truncated}
          />
        )
      ) : (
        <ProblemPanel error={rates.error} what="rate cards" />
      )}
      <h2 className="mx-section-title">Capacity bookings</h2>
      {bookings.ok ? <BookingGrid rows={listed} /> : <ProblemPanel error={bookings.error} what="capacity bookings" />}
    </>
  );
}
