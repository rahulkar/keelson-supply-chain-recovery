import axe from "axe-core";
import { expect } from "vitest";

/**
 * Runs axe-core (WCAG 2.x A/AA rules) against a rendered container. Colour contrast is
 * skipped because jsdom does not compute styles; contrast is verified on the tokens instead.
 */
export async function expectNoA11yViolations(container: Element): Promise<void> {
  const results = await axe.run(container, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"] },
    rules: {
      "color-contrast": { enabled: false },
      region: { enabled: false },
      "landmark-one-main": { enabled: false },
      "page-has-heading-one": { enabled: false },
      "heading-order": { enabled: false },
    },
  });
  const summary = results.violations.map(
    (v) => `${v.id}: ${v.help}\n  ${v.nodes.map((n) => n.html).join("\n  ")}`,
  );
  expect(summary, summary.join("\n")).toEqual([]);
}
