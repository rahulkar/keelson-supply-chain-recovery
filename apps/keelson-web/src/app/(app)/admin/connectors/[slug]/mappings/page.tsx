import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { MappingsPage } from "@/components/admin/MappingsPage";
import { SLUG_PATTERN } from "@/lib/admin/wizard";

export const metadata: Metadata = { title: "Mappings · Admin" };

export default async function AdminConnectorMappingsRoute({
  params,
  searchParams,
}: {
  params: Promise<{ slug: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { slug } = await params;
  if (!SLUG_PATTERN.test(slug)) notFound();
  // M8: `?propose=1` (the connector page's and the wizard's call to action) opens the proposals panel.
  const propose = (await searchParams)["propose"] === "1";
  return <MappingsPage slug={slug} propose={propose} />;
}
