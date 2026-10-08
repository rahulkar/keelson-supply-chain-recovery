import type { Metadata } from "next";
import { KnowledgePage } from "@/components/admin/KnowledgePage";

export const metadata: Metadata = { title: "Knowledge · Admin" };

export default function AdminKnowledgeRoute() {
  return <KnowledgePage />;
}
