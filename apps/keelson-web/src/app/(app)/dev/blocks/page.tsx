import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { DevBlocks } from "@/components/dev/DevBlocks";
import { requireMe } from "@/lib/server/session";

export const metadata: Metadata = { title: "Render blocks" };

/** Block gallery, DEMO_MODE only (`demo_mode` from `/api/me`). */
export default async function DevBlocksPage() {
  const me = await requireMe();
  if (!me.demo_mode) notFound();
  return <DevBlocks />;
}
