import type { Metadata } from "next";
import { GatewayPage } from "@/components/admin/GatewayPage";

export const metadata: Metadata = { title: "AI Gateway · Admin" };

export default function AdminGatewayRoute() {
  return <GatewayPage />;
}
