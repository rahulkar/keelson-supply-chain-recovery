import type { Metadata } from "next";
import { ConnectorsPage } from "@/components/admin/ConnectorsPage";

export const metadata: Metadata = { title: "Connectors · Admin" };

export default function AdminConnectorsRoute() {
  return <ConnectorsPage />;
}
