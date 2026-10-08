import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { ConnectorDetail } from "@/components/admin/ConnectorDetail";
import { SLUG_PATTERN } from "@/lib/admin/wizard";

export const metadata: Metadata = { title: "Connector · Admin" };

export default async function AdminConnectorRoute({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  if (!SLUG_PATTERN.test(slug)) notFound();
  return <ConnectorDetail slug={slug} />;
}
