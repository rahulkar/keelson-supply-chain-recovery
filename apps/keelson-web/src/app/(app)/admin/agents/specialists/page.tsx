import type { Metadata } from "next";
import { SpecialistsPage } from "@/components/admin/SpecialistsPage";

export const metadata: Metadata = { title: "Custom specialists · Admin" };

export default function AdminSpecialistsRoute() {
  return <SpecialistsPage />;
}
