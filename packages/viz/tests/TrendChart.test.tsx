import { act, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TrendChart } from "../src";
import { expectNoA11yViolations } from "./a11y";

// M8 Evals UI (docs/contracts/128 §6): a metric over runs with its target, as small multiples.
describe("TrendChart", () => {
  it("renders every point and the target in an accessible table; the drawing is decorative", async () => {
    const pct = (bp: number) => `${(bp / 100).toFixed(0)} %`;
    const { container } = render(
      <TrendChart
        caption="Pass rate"
        format={pct}
        threshold={{ value: 8000, label: "≥ 80 %" }}
        data={[
          { label: "7 Oct 08:08", value: 0 },
          { label: "7 Oct 08:50", value: 5000 },
          { label: "8 Oct 09:00", value: null },
        ]}
      />,
    );
    await act(async () => {});
    const table = screen.getByRole("table", { name: "Pass rate" });
    const rows = within(table).getAllByRole("row");
    expect(rows.map((r) => r.textContent)).toEqual([
      "RunValue",
      "7 Oct 08:080 %",
      "7 Oct 08:5050 %",
      "8 Oct 09:00Not measured",
      "Target≥ 80 %",
    ]);
    expect(container.querySelector("[data-chart=trend]")).toHaveAttribute("aria-hidden", "true");
    expect(screen.getByText("Pass rate", { selector: "figcaption" })).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("works without a target or a formatter", () => {
    render(<TrendChart caption="Tokens per case" labelHeader="Started" data={[{ label: "a", value: 1234.5 }]} />);
    const table = screen.getByRole("table", { name: "Tokens per case" });
    expect(within(table).getByRole("columnheader", { name: "Started" })).toBeInTheDocument();
    expect(within(table).getByText("1,234.5")).toBeInTheDocument();
    expect(within(table).queryByText("Target")).toBeNull();
  });
});
