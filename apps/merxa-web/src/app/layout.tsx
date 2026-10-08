import "@fontsource-variable/source-sans-3";
import "@fontsource-variable/jetbrains-mono";
import "./globals.css";

import type { Metadata, Viewport } from "next";
import { cookies } from "next/headers";
import type { ReactNode } from "react";

import { DensityProvider } from "@/components/shell/DensityProvider";
import { DENSITY_COOKIE, parseDensity } from "@/lib/density";

export const metadata: Metadata = {
  title: { default: "Merxa Retail Suite", template: "%s · Merxa Retail Suite" },
  description: "Merxa Retail Suite by Merxa Systems: merchandise, supply and stock operations.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = { themeColor: "#152b26" };

export default async function RootLayout({ children }: { children: ReactNode }) {
  const density = parseDensity((await cookies()).get(DENSITY_COOKIE)?.value);
  return (
    <html lang="en-GB" data-density={density}>
      <body>
        <DensityProvider initial={density}>{children}</DensityProvider>
      </body>
    </html>
  );
}
