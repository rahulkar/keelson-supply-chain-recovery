import type { Metadata } from "next";
import Link from "next/link";

import { InventoryGrid, type InventoryRow } from "@/components/stock/InventoryGrid";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { externallyChangedEntities, trackLatestChange } from "@/lib/attribution";
import { apiGetAll, apiWalk, load } from "@/lib/server-api";
import { param, parseCodeList, type SearchParams } from "@/lib/params";
import type { AuditRow, Dc, InventoryPosition, Store, Style } from "@/lib/types";

export const metadata: Metadata = { title: "Inventory" };

type Props = { searchParams: Promise<SearchParams> };

export default async function InventoryPage({ searchParams }: Props) {
  const sp = await searchParams;
  const typeParam = param(sp, "location_type");
  const locationType = typeParam === "store" ? "store" : typeParam === "all" ? undefined : "dc";
  const dc = param(sp, "dc");
  const location = param(sp, "location");
  const skus = parseCodeList(param(sp, "sku"));
  const style = param(sp, "style");

  const [dcs, styles] = await Promise.all([load(apiGetAll<Dc>("/v1/dcs")), load(apiGetAll<Style>("/v1/styles"))]);

  // Location filter: an explicit location wins; otherwise a DC narrows DC rows to itself
  // and store rows to the stores it serves.
  let locationCodes: string[] | undefined;
  let storeLookupError = null;
  if (location) {
    locationCodes = [location];
  } else if (dc) {
    if (locationType === "dc") {
      locationCodes = [dc];
    } else {
      const stores = await load(apiGetAll<Store>("/v1/stores", { dc_code: dc }));
      if (stores.ok) locationCodes = [...(locationType ? [] : [dc]), ...stores.data.items.map((s) => s.code)];
      else storeLookupError = stores.error;
    }
  }

  const [positions, audit] = await Promise.all([
    load(
      apiGetAll<InventoryPosition>(
        "/v1/inventory-positions",
        { location_type: locationType, location_code: locationCodes, sku: skus, style_code: style },
        { max: 10_000 },
      ),
    ),
    // The audit log pages oldest → newest: walk all of it so the newest changes are never cut off.
    load(
      (async () => {
        const latest = new Map<string, AuditRow>();
        const walk = await apiWalk<AuditRow>("/v1/audit-log", { entity_type: "inventory_position" }, (page) =>
          trackLatestChange(latest, page),
        );
        return { latest, complete: walk.complete };
      })(),
    ),
  ]);

  // Positions whose latest change came from an external system: amber highlight + batch link.
  const lastExternal = audit.ok ? externallyChangedEntities(audit.data.latest) : new Map<string, AuditRow>();
  const rows: InventoryRow[] = positions.ok
    ? positions.data.items.map((p) => {
        const ext = lastExternal.get(`${p.location_type}/${p.location_code}/${p.sku}`);
        return { ...p, external_change: ext?.change_set_code ?? (ext ? (ext.via ?? "External") : null), external_at: ext?.at ?? null };
      })
    : [];
  const filtered = !!(typeParam || dc || location || skus.length || style);

  return (
    <>
      <PageHeader
        title="Inventory"
        subtitle="Inventory positions by location and SKU. ATP = on hand − allocated − reserved. In transit is inbound to the location."
      />
      <form className="mx-toolbar" method="get" aria-label="Filter inventory">
        <label className="mx-field">
          <span className="mx-field__label">Location type</span>
          <select className="mx-select" name="location_type" defaultValue={typeParam ?? "dc"}>
            <option value="dc">Distribution centres</option>
            <option value="store">Stores</option>
            <option value="all">All locations</option>
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">DC</span>
          <select className="mx-select" name="dc" defaultValue={dc ?? ""}>
            <option value="">All DCs</option>
            {(dcs.ok ? dcs.data.items : []).map((d) => (
              <option key={d.code} value={d.code}>
                {d.code} · {d.name}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Location code</span>
          <input className="mx-input" name="location" defaultValue={location ?? ""} placeholder="e.g. UK1 or a store code" />
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Style</span>
          <select className="mx-select" name="style" defaultValue={style ?? ""}>
            <option value="">All styles</option>
            {(styles.ok ? styles.data.items : []).map((s) => (
              <option key={s.code} value={s.code}>
                {s.code} · {s.name}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">SKU(s)</span>
          <input
            className="mx-input mx-mono"
            name="sku"
            defaultValue={skus.join(", ")}
            placeholder="PUF-W-BLK-M, …"
            style={{ minWidth: 220 }}
          />
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {filtered && (
          <Link className="mx-btn mx-btn--ghost" href="/inventory">
            Reset
          </Link>
        )}
      </form>
      {storeLookupError && <ProblemPanel error={storeLookupError} what="the stores of this DC" />}
      {!audit.ok && <ProblemPanel error={audit.error} what="the change history (external-change highlights)" />}
      {positions.ok ? (
        <InventoryGrid
          rows={rows}
          truncated={positions.data.truncated}
          showLocationType={locationType === undefined}
        />
      ) : (
        <ProblemPanel error={positions.error} what="inventory positions" />
      )}
    </>
  );
}
