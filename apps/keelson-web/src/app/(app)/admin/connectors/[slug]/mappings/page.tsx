import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { MappingsPage } from "@/components/admin/MappingsPage";
import { SLUG_PATTERN } from "@/lib/admin/wizard";

export const metadata: Metadata = { title: "Mappings · Admin" };

export default async function AdminConnectorMappingsRoute({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = await params;
  if (!SLUG_PATTERN.test(slug)) notFound();
  return <MappingsPage slug={slug} />;
}
