import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

/**
 * Browser e2e against the running compose stack (see README.md). Inside the e2e container,
 * Docker DNS resolves *.localhost to Caddy, but Chromium hard-wires *.localhost to loopback, so
 * `EDGE_IP` (Caddy's address on keelson_edge, set by run.sh) is mapped in with
 * --host-resolver-rules. The URLs stay *.localhost, so they remain secure contexts (Keycloak's
 * Secure cookies work over plain HTTP exactly as in a desktop browser).
 */
const edgeIp = process.env.EDGE_IP;
const artifacts = path.join(__dirname, "artifacts");

export default defineConfig({
  testDir: "./tests",
  // Fails the run if a credential reached any trace / report / attachment.
  globalTeardown: "./tests/support/global-teardown.ts",
  outputDir: path.join(artifacts, "test-results"),
  // One worker: the tests share the demo data (Merxa scenario, Marco's Merxa link).
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [
    ["list"],
    ["html", { outputFolder: path.join(artifacts, "report"), open: "never" }],
    ["json", { outputFile: path.join(artifacts, "results.json") }],
  ],
  use: {
    ...devices["Desktop Chrome"],
    viewport: { width: 1280, height: 800 },
    locale: "en-GB",
    timezoneId: "Europe/London",
    // Traces are recorded by the `tracer` fixture (tests/support/fixtures.ts) so password entry
    // can be left out of them.
    trace: "off",
    screenshot: "only-on-failure",
    video: "off",
    actionTimeout: 20_000,
    navigationTimeout: 45_000,
    launchOptions: {
      args: edgeIp ? [`--host-resolver-rules=MAP *.localhost ${edgeIp}`] : [],
    },
  },
  projects: [{ name: "chromium" }],
});
