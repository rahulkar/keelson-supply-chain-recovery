import type { Metadata } from "next";
import { AuditPage } from "@/components/admin/AuditPage";

export const metadata: Metadata = { title: "Audit · Admin" };

export default function AdminAuditRoute() {
  return <AuditPage />;
}
