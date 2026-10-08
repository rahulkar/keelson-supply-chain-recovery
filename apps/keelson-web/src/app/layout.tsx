import "@fontsource-variable/inter";
import "./globals.css";

import { UiProvider } from "@keelson/ui";
import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import { THEME_BOOT_SCRIPT } from "@/lib/theme";

const engineName = process.env.BRAND_ENGINE_NAME || "Keelson";

export const metadata: Metadata = {
  title: { default: engineName, template: `%s · ${engineName}` },
  description: `${engineName}: plan and recover from supply-chain disruptions.`,
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#f9fafc" },
    { media: "(prefers-color-scheme: dark)", color: "#0d1014" },
  ],
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en-GB" data-theme="light" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOT_SCRIPT }} />
      </head>
      <body className="min-h-dvh antialiased">
        <UiProvider>{children}</UiProvider>
      </body>
    </html>
  );
}
