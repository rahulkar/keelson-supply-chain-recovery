import type { Metadata } from "next";
import Link from "next/link";

import { Webhooks } from "@/components/admin/Webhooks";
import { EmptyState } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { errorMessage } from "@/lib/http";
import { redactSubscriptionUrls } from "@/lib/m2";
import { oneOf, param, type SearchParams } from "@/lib/params";
import { apiGet, getMe, load } from "@/lib/server-api";
import type { WebhookDelivery, WebhookSubscription } from "@/lib/types";

export const metadata: Metadata = { title: "Webhooks" };

const DELIVERY_STATUSES = ["pending", "delivered", "failed"] as const;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DELIVERY_LIMIT = 200;

type Props = { searchParams: Promise<SearchParams> };

/** Outbound webhook subscriptions and their recent deliveries. merxa-admin only. */
export default async function WebhooksPage({ searchParams }: Props) {
  const sp = await searchParams;
  const me = await getMe();
  const isAdmin = me.user.roles.includes("merxa-admin");
  const header = (
    <PageHeader
      title="Webhooks"
      subtitle="Merxa pushes domain events (change batches, ETA changes, supplier messages, stock adjustments, sailings) to subscribed endpoints."
    />
  );
  if (!isAdmin) {
    return (
      <>
        {header}
        <div className="mx-card">
          <EmptyState title="Not available">Managing webhooks needs the Merxa Administrator role.</EmptyState>
        </div>
      </>
    );
  }
  const status = oneOf(sp, "status", DELIVERY_STATUSES);
  const subParam = param(sp, "subscription");
  const subscription = subParam && UUID.test(subParam) ? subParam : undefined;
  const [subs, deliveries] = await Promise.all([
    load(apiGet<{ items: WebhookSubscription[] }>("/v1/webhook-subscriptions")),
    load(
      apiGet<{ items: WebhookDelivery[] }>(
        subscription ? `/v1/webhook-subscriptions/${encodeURIComponent(subscription)}/deliveries` : "/v1/webhook-deliveries",
        { status, limit: DELIVERY_LIMIT },
      ),
    ),
  ]);
  if (!subs.ok) {
    return (
      <>
        {header}
        <ProblemPanel error={subs.error} what="webhook subscriptions" />
      </>
    );
  }
  return (
    <>
      {header}
      <form className="mx-toolbar" method="get" aria-label="Filter deliveries">
        <label className="mx-field">
          <span className="mx-field__label">Delivery status</span>
          <select className="mx-select" name="status" defaultValue={status ?? ""}>
            <option value="">All</option>
            {DELIVERY_STATUSES.map((s) => (
              <option key={s} value={s}>
                {s === "pending" ? "pending / retrying" : s}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Subscription</span>
          <select className="mx-select" name="subscription" defaultValue={subscription ?? ""}>
            <option value="">All subscriptions</option>
            {subs.data.items.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {(status || subscription) && (
          <Link className="mx-btn mx-btn--ghost" href="/webhooks">
            Reset
          </Link>
        )}
        <span className="mx-muted" style={{ alignSelf: "center" }}>
          Showing the newest {DELIVERY_LIMIT} deliveries.
        </span>
      </form>
      <Webhooks
        subscriptions={redactSubscriptionUrls(subs.data.items)}
        deliveries={deliveries.ok ? deliveries.data.items : []}
        deliveriesError={deliveries.ok ? null : errorMessage(deliveries.error)}
      />
    </>
  );
}
