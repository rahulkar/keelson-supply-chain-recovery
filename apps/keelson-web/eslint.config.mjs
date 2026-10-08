import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// Keelson and Merxa are separate products: the Keelson app never reaches into Merxa's UI code.
const MERXA_IMPORT_BAN = {
  patterns: [
    { group: ["**/apps/merxa-web/**", "merxa-web", "merxa-web/*"], message: "keelson-web must not import Merxa's UI (separate product)." },
  ],
};

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    rules: {
      "no-restricted-imports": ["error", MERXA_IMPORT_BAN],
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  globalIgnores([".next/**", "out/**", "build/**", "coverage/**", "next-env.d.ts", "public/maplibre/**"]),
]);
