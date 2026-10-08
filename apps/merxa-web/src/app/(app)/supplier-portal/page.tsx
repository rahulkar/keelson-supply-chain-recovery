import type { Metadata } from "next";
import Link from "next/link";

import { SupplierInbox } from "@/components/supplier/SupplierInbox";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { oneOf, param, type SearchParams } from "@/lib/params";
import { apiGetAll, getMe, load } from "@/lib/server-api";
import type { Supplier, SupplierMessage } from "@/lib/types";

export const metadata: Metadata = { title: "Supplier Portal" };

type Props = { searchParams: Promise<SearchParams> };

export default async function SupplierPortalPage({ searchParams }: Props) {
  const sp = await searchParams;
  const supplier = param(sp, "supplier_code");
  const status = oneOf(sp, "status", ["new", "read"] as const);
  const [me, messages, suppliers] = await Promise.all([
    getMe(),
    load(apiGetAll<SupplierMessage>("/v1/supplier-messages", { supplier_code: supplier, status }, { max: 2000 })),
    load(apiGetAll<Supplier>("/v1/suppliers")),
  ]);
  const supplierNames = Object.fromEntries((suppliers.ok ? suppliers.data.items : []).map((s) => [s.code, s.name]));
  // API order is oldest first; the inbox shows newest first.
  const items = messages.ok ? [...messages.data.items].reverse() : [];

  return (
    <>
      <PageHeader
        title="Supplier Portal"
        subtitle="Messages from suppliers and factories. Message content comes from external senders."
      />
      <form className="mx-toolbar" method="get" aria-label="Filter supplier messages">
        <label className="mx-field">
          <span className="mx-field__label">Supplier</span>
          <select className="mx-select" name="supplier_code" defaultValue={supplier ?? ""}>
            <option value="">All suppliers</option>
            {(suppliers.ok ? suppliers.data.items : []).map((s) => (
              <option key={s.code} value={s.code}>
                {s.code} · {s.name}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Status</span>
          <select className="mx-select" name="status" defaultValue={status ?? ""}>
            <option value="">All</option>
            <option value="new">Unread</option>
            <option value="read">Read</option>
          </select>
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {(supplier || status) && (
          <Link className="mx-btn mx-btn--ghost" href="/supplier-portal">
            Reset
          </Link>
        )}
      </form>
      {messages.ok ? (
        <SupplierInbox
          messages={items}
          supplierNames={supplierNames}
          canMarkRead={me.user.scopes.includes("changes:preview")}
          initialSelected={param(sp, "message") ?? null}
        />
      ) : (
        <ProblemPanel error={messages.error} what="supplier messages" />
      )}
    </>
  );
}
