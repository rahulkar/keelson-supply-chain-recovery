import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { Composer, SuggestionChips, type ComposerProps } from "../src";
import { expectNoA11yViolations } from "./a11y";

function Harness(props: Partial<ComposerProps> & { onSubmit: (v: string) => void }) {
  const [value, setValue] = useState(props.value ?? "");
  return <Composer {...props} value={value} onChange={setValue} />;
}

describe("Composer", () => {
  it("sends trimmed text on Enter, keeps Shift+Enter for new lines", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    render(<Harness onSubmit={onSubmit} placeholder="Ask Keelson" />);
    const box = screen.getByRole("textbox", { name: "Ask Keelson" });
    await user.type(box, "  Trim supplier delayed 3 weeks{Shift>}{Enter}{/Shift}line two");
    expect(onSubmit).not.toHaveBeenCalled();
    expect(box).toHaveValue("  Trim supplier delayed 3 weeks\nline two");
    await user.keyboard("{Enter}");
    expect(onSubmit).toHaveBeenCalledWith("Trim supplier delayed 3 weeks\nline two");
  });

  it("disables Send while empty and while busy", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(<Composer value="" onChange={() => {}} onSubmit={onSubmit} />);
    expect(screen.getByRole("button", { name: "Send" })).toHaveAttribute("aria-disabled", "true");
    rerender(<Composer value="hello" onChange={() => {}} onSubmit={onSubmit} busy />);
    await user.type(screen.getByRole("textbox"), "{Enter}");
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("turns Send into Stop while busy", async () => {
    const onStop = vi.fn();
    const user = userEvent.setup();
    render(<Composer value="" onChange={() => {}} onSubmit={() => {}} busy onStop={onStop} />);
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledOnce();
  });

  it("fills the composer from a suggestion (default) or submits it", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    const suggestions = [
      "A trim supplier is 3 weeks late",
      { id: "port", label: "Port strike in Rotterdam", prompt: "What if Rotterdam closes for a week?" },
    ];
    const { unmount } = render(<Harness onSubmit={onSubmit} suggestions={suggestions} />);
    const group = screen.getByRole("list", { name: "Suggestions" });
    expect(group).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Port strike in Rotterdam" }));
    expect(screen.getByRole("textbox")).toHaveValue("What if Rotterdam closes for a week?");
    expect(onSubmit).not.toHaveBeenCalled();
    // Suggestions hide once the box has text.
    expect(screen.queryByRole("list", { name: "Suggestions" })).not.toBeInTheDocument();
    unmount();

    render(<Harness onSubmit={onSubmit} suggestions={suggestions} suggestionBehavior="submit" />);
    await user.click(screen.getByRole("button", { name: "A trim supplier is 3 weeks late" }));
    expect(onSubmit).toHaveBeenCalledWith("A trim supplier is 3 weeks late");
  });

  it("SuggestionChips calls onSelect with the prompt", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<SuggestionChips suggestions={[{ label: "Show alerts", prompt: "Show me today's alerts" }]} onSelect={onSelect} />);
    await user.click(screen.getByRole("button", { name: "Show alerts" }));
    expect(onSelect).toHaveBeenCalledWith("Show me today's alerts");
  });

  it("has no axe violations", async () => {
    const { container } = render(
      <Composer value="" onChange={() => {}} onSubmit={() => {}} suggestions={["One", "Two"]} />,
    );
    await expectNoA11yViolations(container);
  });
});
