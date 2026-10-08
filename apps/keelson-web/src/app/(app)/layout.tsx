import type { ReactNode } from "react";
import { AppShell } from "@/components/shell/AppShell";
import { requireMe } from "@/lib/server/session";

/** Every product page: signed-in user (else BFF sign-in) and the app chrome. */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const me = await requireMe();
  return <AppShell me={me}>{children}</AppShell>;
}
