import type { Metadata } from "next";
import Link from "next/link";

import { ProductionSchedule } from "@/components/production/ProductionSchedule";
import { EmptyState } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { buildQuery } from "@/lib/http";
import { addDays, isIsoDate, scheduleWindow } from "@/lib/m2";
import { param, type SearchParams } from "@/lib/params";
import { apiGet, apiGetAll, load } from "@/lib/server-api";
import type { Factory, FactoryLine, FactorySchedule } from "@/lib/types";

export const metadata: Metadata = { title: "Production Schedule" };

/** merxa-api's default window is [today − 7 days, today + 42 days] on the simulation clock. */
const DEFAULT_LOOKBACK_DAYS = 7;

type Props = { searchParams: Promise<SearchParams> };

export default async function ProductionSchedulePage({ searchParams }: Props) {
  const sp = await searchParams;
  const fromParam = param(sp, "from");
  const toParam = param(sp, "to");
  // A lone, reversed or over-long window is normalised so merxa-api does not reject it.
  const { from, to } = scheduleWindow(isIsoDate(fromParam) ? fromParam : undefined, isIsoDate(toParam) ? toParam : undefined);

  const [factories, lines] = await Promise.all([
    load(apiGetAll<Factory>("/v1/factories")),
    load(apiGetAll<FactoryLine>("/v1/factory-lines")),
  ]);
  const withLines = new Set((lines.ok ? lines.data.items : []).map((l) => l.factory_code));
  const options = (factories.ok ? factories.data.items : []).filter((f) => withLines.has(f.code));
  const requested = param(sp, "factory");
  const factory = options.find((f) => f.code === requested) ?? options[0];

  const schedule = factory
    ? await load(apiGet<FactorySchedule>(`/v1/factories/${encodeURIComponent(factory.code)}/schedule`, { from, to }))
    : null;
  // Without an explicit window the API centres on the simulation's today.
  const today = schedule?.ok && !from && !to ? addDays(schedule.data.from, DEFAULT_LOOKBACK_DAYS) : null;

  return (
    <>
      <PageHeader
        title="Production Schedule"
        subtitle="Factory lines with their working calendar and the production orders planned on them."
      />
      {factories.ok && options.length > 0 && (
        <nav className="mx-tabs" aria-label="Factories">
          {options.map((f) => (
            <Link
              key={f.code}
              href={`/production-schedule${buildQuery({ factory: f.code, from, to })}`}
              className="mx-tab"
              aria-current={factory?.code === f.code ? "page" : undefined}
              title={`${f.name} (${f.country})`}
            >
              <span className="mx-mono">{f.code}</span>
            </Link>
          ))}
        </nav>
      )}
      <form className="mx-toolbar" method="get" aria-label="Schedule window">
        {factory && <input type="hidden" name="factory" value={factory.code} />}
        <label className="mx-field">
          <span className="mx-field__label">From</span>
          <input className="mx-input" type="date" name="from" defaultValue={from ?? (schedule?.ok ? schedule.data.from : "")} />
        </label>
        <label className="mx-field">
          <span className="mx-field__label">To</span>
          <input className="mx-input" type="date" name="to" defaultValue={to ?? (schedule?.ok ? schedule.data.to : "")} />
        </label>
        <button type="submit" className="mx-btn">
          Show
        </button>
        {(from || to) && (
          <Link className="mx-btn mx-btn--ghost" href={`/production-schedule${buildQuery({ factory: factory?.code })}`}>
            Default window
          </Link>
        )}
        {factory && (
          <span className="mx-muted" style={{ alignSelf: "center" }}>
            {factory.name} · {factory.country} · supplier <span className="mx-mono">{factory.supplier_code}</span>
          </span>
        )}
      </form>
      {!factories.ok && <ProblemPanel error={factories.error} what="factories" />}
      {!lines.ok && <ProblemPanel error={lines.error} what="factory lines" />}
      {factories.ok && lines.ok && !factory && (
        <div className="mx-card">
          <EmptyState title="No factory lines">No factory has production lines in Merxa.</EmptyState>
        </div>
      )}
      {schedule &&
        (schedule.ok ? (
          <ProductionSchedule schedule={schedule.data} today={today} />
        ) : (
          <ProblemPanel error={schedule.error} what="the production schedule" />
        ))}
    </>
  );
}
