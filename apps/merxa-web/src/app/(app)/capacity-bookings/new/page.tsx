import { IconListCheck } from "@tabler/icons-react";
import type { Metadata } from "next";
import Link from "next/link";

import { CapacityBookingForm, type LaneChoice } from "@/components/forms/ManualForms";
import { Card } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { parseQty } from "@/lib/manual-change";
import { isIsoWeek, laneWeekCapacity, weekRange } from "@/lib/m2";
import { param, type SearchParams } from "@/lib/params";
import { apiGetAll, getSimWeek, load } from "@/lib/server-api";
import type { CapacityBooking, Lane, RateCard } from "@/lib/types";

export const metadata: Metadata = { title: "New capacity booking" };

/** Weeks offered with a pre-computed remaining capacity (from the guide's week, else the planning week). */
const WEEKS = 16;

type Props = { searchParams: Promise<SearchParams> };

/**
 * Book lane capacity (per-unit lanes). Operator-guide deep links (`?lane=&week=&qty=`) open
 * it pre-filled; saving sends `POST /v1/capacity-bookings` with an Idempotency-Key (merxa-api
 * checks the remaining capacity again and emits `capacity_booking.created`).
 */
export default async function NewCapacityBookingPage({ searchParams }: Props) {
  const sp = await searchParams;
  const laneParam = param(sp, "lane");
  const weekParam = param(sp, "week");
  const qty = parseQty(param(sp, "qty") ?? "", { min: 1 });

  const [lanes, rates, bookings, simWeek] = await Promise.all([
    load(apiGetAll<Lane>("/v1/lanes")),
    load(apiGetAll<RateCard>("/v1/rate-cards")),
    load(apiGetAll<CapacityBooking>("/v1/capacity-bookings", {}, { max: 50_000 })),
    getSimWeek(),
  ]);
  const rateItems = rates.ok ? rates.data.items : [];
  const perUnit = new Set(rateItems.filter((r) => r.basis === "per_unit").map((r) => r.lane_code));
  const choices: LaneChoice[] = (lanes.ok ? lanes.data.items : [])
    .filter((l) => perUnit.has(l.code) || l.code === laneParam)
    .map((l) => ({ code: l.code, label: `${l.code} · ${l.origin} → ${l.dest} (${l.mode.replace("_", "-")})` }));
  const startWeek = isIsoWeek(weekParam) ? weekParam : simWeek;
  // Remaining capacity is only shown when every booking was read (never the full rate-card
  // capacity as "remaining" when bookings are missing).
  const known = rates.ok && bookings.ok && !bookings.data.truncated && !rates.data.truncated;
  const remaining: Record<string, number | null> = {};
  if (known && startWeek) {
    const weeks = [...new Set([...(isIsoWeek(weekParam) ? [weekParam] : []), ...weekRange(startWeek, WEEKS)])];
    for (const lane of choices) {
      for (const w of weeks) remaining[`${lane.code}|${w}`] = laneWeekCapacity(lane.code, w, rateItems, bookings.data.items).remaining;
    }
  }

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Sailings & Lanes", href: "/sailings?tab=capacity" }, { label: "New capacity booking" }]}
        title="New capacity booking"
        subtitle="Reserve weekly capacity on a per-unit lane (e.g. sea-air)."
      />
      {laneParam && (
        <div className="mx-banner mx-banner--info" role="note">
          <span className="mx-banner__icon" aria-hidden="true">
            <IconListCheck size={18} />
          </span>
          <div className="mx-banner__body">
            <div className="mx-banner__title">Operator guide step</div>
            The booking below is pre-filled from your guide. Check it and choose Book capacity.
          </div>
        </div>
      )}
      {lanes.ok ? null : <ProblemPanel error={lanes.error} what="lanes" />}
      {rates.ok ? null : <ProblemPanel error={rates.error} what="rate cards" />}
      <Card title="Booking">
        <CapacityBookingForm
          lanes={choices}
          initialLane={laneParam ?? ""}
          initialWeek={isIsoWeek(weekParam) ? weekParam : (simWeek ?? "")}
          initialQty={qty}
          remainingByLaneWeek={remaining}
        />
      </Card>
      <p className="mx-muted">
        <Link href="/sailings?tab=capacity">Back to capacity</Link>
      </p>
    </>
  );
}
