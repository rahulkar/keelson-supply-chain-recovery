import type { Metadata } from "next";
import { PoliciesPage } from "@/components/admin/PoliciesPage";

export const metadata: Metadata = { title: "Policies · Admin" };

export default function AdminPoliciesRoute() {
  return <PoliciesPage />;
}
