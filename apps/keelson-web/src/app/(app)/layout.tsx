import type { ReactNode } from "react";
import { NetworkMapSettingsProvider } from "@keelson/viz";
import { AppShell } from "@/components/shell/AppShell";
import { basemapModeFromEnv } from "@/lib/server/basemap";
import { requireMe } from "@/lib/server/session";

/**
 * Every product page: signed-in user (else BFF sign-in) and the app chrome. The network map's base
 * map mode comes from the runtime env (`KEELSON_BASEMAP`, read per request: a restart switches it).
 */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const me = await requireMe();
  return (
    <AppShell me={me}>
      <NetworkMapSettingsProvider basemap={{ mode: basemapModeFromEnv() }}>{children}</NetworkMapSettingsProvider>
    </AppShell>
  );
}
