"use client";

import {
  IconArrowsExchange,
  IconBuildingFactory2,
  IconBuildingWarehouse,
  IconClipboardList,
  IconDiscount2,
  IconFileInvoice,
  IconHistory,
  IconInbox,
  IconKey,
  IconPackages,
  IconPlayerPlay,
  IconShip,
  IconShoppingCart,
  IconTruckDelivery,
  IconWebhook,
} from "@tabler/icons-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ComponentType } from "react";

type NavItem = {
  href: string;
  label: string;
  icon: ComponentType<{ size?: number; stroke?: number; "aria-hidden"?: boolean | "true" | "false" }>;
};
type NavGroup = { label: string; items: NavItem[] };

const GROUPS: NavGroup[] = [
  {
    label: "Supply",
    items: [
      { href: "/purchase-orders", label: "Purchase Orders", icon: IconFileInvoice },
      { href: "/production-schedule", label: "Production Schedule", icon: IconBuildingFactory2 },
      { href: "/supplier-portal", label: "Supplier Portal", icon: IconInbox },
    ],
  },
  {
    label: "Logistics",
    items: [
      { href: "/sailings", label: "Sailings & Lanes", icon: IconShip },
      { href: "/shipments", label: "Shipments", icon: IconTruckDelivery },
    ],
  },
  {
    label: "Stock",
    items: [
      { href: "/inventory", label: "Inventory", icon: IconPackages },
      { href: "/transfer-orders", label: "Transfer Orders", icon: IconArrowsExchange },
      { href: "/locations", label: "DCs & Stores", icon: IconBuildingWarehouse },
    ],
  },
  {
    label: "Demand",
    items: [
      { href: "/online-orders", label: "Online Orders", icon: IconShoppingCart },
      { href: "/promotions", label: "Promotions", icon: IconDiscount2 },
    ],
  },
  {
    label: "Control",
    items: [
      { href: "/change-batches", label: "Change Batches", icon: IconClipboardList },
      { href: "/audit", label: "Audit Log", icon: IconHistory },
    ],
  },
];

/** Merxa administrators only (merxa-api enforces the role on every endpoint behind them). */
const ADMIN_GROUP: NavGroup = {
  label: "Administration",
  items: [
    { href: "/api-clients", label: "API & MCP Clients", icon: IconKey },
    { href: "/webhooks", label: "Webhooks", icon: IconWebhook },
  ],
};

const DEMO_ITEM: NavItem = { href: "/demo-director", label: "Demo Director", icon: IconPlayerPlay };

export function isActive(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function Sidebar({ showDemoDirector, showAdmin = false }: { showDemoDirector: boolean; showAdmin?: boolean }) {
  const pathname = usePathname() ?? "/";
  const groups = [
    ...GROUPS,
    ...(showAdmin ? [ADMIN_GROUP] : []),
    ...(showDemoDirector ? [{ label: "Demo", items: [DEMO_ITEM] }] : []),
  ];
  return (
    <nav className="mx-nav" aria-label="Modules">
      {groups.map((g) => (
        <div key={g.label}>
          <div className="mx-nav__group">{g.label}</div>
          {g.items.map(({ href, label, icon: Icon }) => (
            <Link
              key={href}
              href={href}
              className="mx-nav__link"
              title={label}
              aria-current={isActive(pathname, href) ? "page" : undefined}
            >
              <Icon size={16} stroke={1.75} aria-hidden="true" />
              <span className="mx-nav__label">{label}</span>
            </Link>
          ))}
        </div>
      ))}
    </nav>
  );
}
