import type { Metadata } from "next";
import { AgentsPage } from "@/components/admin/AgentsPage";

export const metadata: Metadata = { title: "Agents · Admin" };

export default function AdminAgentsRoute() {
  return <AgentsPage />;
}
