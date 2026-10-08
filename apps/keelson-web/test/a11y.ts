import axe from "axe-core";
import { expect } from "vitest";

/**
 * Runs axe-core (WCAG 2.x A/AA + best practices) against a rendered container. Colour contrast is
 * skipped (jsdom computes no styles; `@keelson/tokens` tests contrast on the tokens).
 *
 * `landmark-unique` is on: every turn and every thinking timeline has a unique name.
 */
export async function expectNoA11yViolations(container: Element): Promise<void> {
  const results = await axe.run(container, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"] },
    rules: {
      "color-contrast": { enabled: false },
      region: { enabled: false },
      "landmark-one-main": { enabled: false },
      "page-has-heading-one": { enabled: false },
    },
  });
  const summary = results.violations.map((v) => `${v.id}: ${v.help}\n  ${v.nodes.map((n) => n.html).join("\n  ")}`);
  expect(summary, summary.join("\n")).toEqual([]);
}
