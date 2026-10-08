import { defineConfig } from "vitest/config";

export default defineConfig({
  oxc: { jsx: { runtime: "automatic" } },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.{ts,tsx}"],
    setupFiles: ["./tests/setup.ts"],
    css: false,
    // Canvas visuals load lazily (React Flow + elkjs layout); keep slow CI / Docker builds green.
    testTimeout: 20_000,
  },
});
