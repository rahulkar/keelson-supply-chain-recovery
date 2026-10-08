import type { Metadata } from "next";
import { OverviewPage } from "@/components/admin/OverviewPage";

export const metadata: Metadata = { title: "Admin" };

export default function AdminOverviewRoute() {
  return <OverviewPage />;
}
