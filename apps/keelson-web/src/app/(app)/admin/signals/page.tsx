import type { Metadata } from "next";
import { SignalsPage } from "@/components/admin/SignalsPage";

export const metadata: Metadata = { title: "Signals · Admin" };

export default function AdminSignalsRoute() {
  return <SignalsPage />;
}
