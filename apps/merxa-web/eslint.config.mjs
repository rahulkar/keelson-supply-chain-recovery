import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

// Merxa is a separate product (Merxa Systems): it must never import Keelson code.
const KEELSON_IMPORT_BAN = {
  patterns: [
    {
      group: ["@keelson", "@keelson/*"],
      message: "merxa-web is a separate product and must not import Keelson packages (@keelson/*).",
    },
    {
      group: ["**/packages/ui-keelson/**", "**/packages/tokens-keelson/**", "**/packages/viz/**", "**/packages/contracts-ts/**", "**/apps/keelson-web/**"],
      message: "merxa-web must not reach into Keelson sources.",
    },
  ],
};

const KEELSON_MESSAGE = "merxa-web is a separate product and must not load Keelson packages (@keelson/*).";
// `no-restricted-imports` only sees static imports/exports: also ban dynamic import() and require().
const KEELSON_DYNAMIC_BAN = [
  { selector: "ImportExpression[source.value=/^@keelson($|[^a-z0-9])/]", message: KEELSON_MESSAGE },
  { selector: "CallExpression[callee.name='require'][arguments.0.value=/^@keelson($|[^a-z0-9])/]", message: KEELSON_MESSAGE },
];

export default defineConfig([
  ...nextVitals,
  ...nextTs,
  {
    rules: {
      "no-restricted-imports": ["error", KEELSON_IMPORT_BAN],
      "no-restricted-syntax": ["error", ...KEELSON_DYNAMIC_BAN],
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
    },
  },
  globalIgnores([".next/**", "out/**", "build/**", "coverage/**", "next-env.d.ts"]),
]);
