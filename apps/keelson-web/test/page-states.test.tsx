import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AppNotFound from "@/app/(app)/not-found";
import RootNotFound from "@/app/not-found";
import { ErrorState } from "@/components/common/ErrorState";
import { expectNoA11yViolations } from "./a11y";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }), usePathname: () => "/" }));

/**
 * Pages that consist of a single state (not found, error) still have a main heading: found by
 * the browser e2e (`e2e/playwright`, "unknown case shows a calm not-found state").
 */
describe("page-level states have a level-1 heading", () => {
  it("not found inside the app", async () => {
    const { container } = render(
      <main>
        <AppNotFound />
      </main>,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to home" })).toHaveAttribute("href", "/");
    await expectNoA11yViolations(container);
  });

  it("not found outside the app chrome", () => {
    render(<RootNotFound />);
    expect(screen.getByRole("heading", { level: 1, name: "Not found" })).toBeInTheDocument();
  });

  it("error state", () => {
    render(<ErrorState digest="abc123" onRetry={() => {}} />);
    expect(screen.getByRole("heading", { level: 1, name: "This page didn't load" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
