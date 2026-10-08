import { KEELSON } from "./support/env";
import { expect, test } from "./support/fixtures";
import { signInAs, timeline } from "./support/keelson";

test.use({ reducedMotion: "reduce" });

test("prefers-reduced-motion disables the thinking shimmer and pulses", async ({ page }) => {
  test.setTimeout(5 * 60_000);
  await signInAs(page, "priya");
  await page.goto(`${KEELSON}/`);
  const box = page.getByRole("textbox", { name: "Describe a disruption or ask a question" });
  await box.fill("Which purchase orders arrive late?");
  await box.press("Enter");
  await page.waitForURL(/\/cases\/[^/]+$/);

  const tl = timeline(page, 1);
  await expect(tl).toHaveAttribute("data-status", "running", { timeout: 60_000 });
  // Reduced: static muted headline, no sweep, no pulsing dots/skeletons, no running CSS animations
  // other than the (non-decorative) spinner.
  await expect(tl.locator('[data-motion="reduced"]')).toBeVisible();
  await expect(page.locator(".k-shimmer-text")).toHaveCount(0);
  await expect(page.locator(".animate-pulse-soft")).toHaveCount(0);
  const animations = await page.evaluate(() =>
    document.getAnimations().map((a) => (a as CSSAnimation).animationName ?? a.constructor.name),
  );
  expect(animations.filter((n) => /shimmer|pulse/i.test(n)), "shimmer/pulse animations").toEqual([]);

  // Control: the same run with motion allowed shows the sweep (the preference is live).
  await page.emulateMedia({ reducedMotion: "no-preference" });
  const status = await tl.getAttribute("data-status");
  if (status === "running") {
    await expect(tl.locator('[data-motion="on"]')).toBeVisible();
    await expect(page.locator(".k-shimmer-text").first()).toBeVisible();
  } else {
    test.info().annotations.push({ type: "note", description: `run already ${status}; motion control skipped` });
  }
});
