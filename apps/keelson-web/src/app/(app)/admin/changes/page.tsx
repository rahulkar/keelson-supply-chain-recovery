import type { Metadata } from "next";
import { ChangeRequestsPage } from "@/components/admin/ChangeRequestsPage";

export const metadata: Metadata = { title: "Pending changes · Admin" };

export default function AdminChangesRoute() {
  return <ChangeRequestsPage />;
}
