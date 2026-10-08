import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  Badge,
  Checkbox,
  ConfirmDialog,
  Drawer,
  Field,
  JsonDiff,
  Meter,
  Select,
  StepLog,
  Switch,
  Table,
  TextInput,
  Textarea,
  counterAnnouncement,
  diffJson,
  jsonEqual,
  stepLogAnnouncement,
  type StepLogItem,
} from "../src";
import { expectNoA11yViolations } from "./a11y";

describe("Badge variants", () => {
  it("keeps the default soft look and supports outline / solid / sm / dot", () => {
    const { container } = render(
      <div>
        <Badge>Default</Badge>
        <Badge tone="success" variant="outline" size="sm" dot>
          Active
        </Badge>
        <Badge tone="danger" variant="solid">
          Failed
        </Badge>
      </div>,
    );
    const [def, outline, solid] = Array.from(container.querySelectorAll("span[data-variant]"));
    expect(def).toHaveAttribute("data-variant", "soft");
    expect(def!.className).toContain("h-6");
    expect(outline).toHaveAttribute("data-variant", "outline");
    expect(outline!.className).toContain("h-5");
    expect(outline!.querySelector("span[aria-hidden]")).not.toBeNull(); // dot is decorative
    expect(solid!.className).toContain("bg-danger");
  });
});

describe("Field + controls", () => {
  it("wires label, description, lock reason and error to the control", async () => {
    const { container } = render(
      <Field label="Auto-apply max lines" description="Changes up to this size apply without approval." lockedReason="Requires platform admin to raise" error="Too high">
        <TextInput type="number" defaultValue={10} />
      </Field>,
    );
    const input = screen.getByLabelText(/Auto-apply max lines/);
    expect(input).toHaveAttribute("aria-invalid", "true");
    const describedBy = input.getAttribute("aria-describedby")!.split(" ");
    expect(describedBy).toHaveLength(3);
    for (const id of describedBy) expect(document.getElementById(id)).not.toBeNull();
    expect(screen.getByText("Requires platform admin to raise")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("Textarea shows a counter, blocks past the limit and announces only near it", async () => {
    function Harness() {
      const [v, setV] = useState("");
      return (
        <Field label="Prompt addendum" withCounter>
          <Textarea value={v} onChange={setV} maxLength={100} />
        </Field>
      );
    }
    const { container } = render(<Harness />);
    const box = screen.getByLabelText("Prompt addendum");
    expect(box).toHaveAttribute("maxLength", "100");
    expect(screen.getByText(/0 \/ 100/)).toBeInTheDocument();
    await userEvent.type(box, "hello");
    expect(screen.getByText(/5 \/ 100/)).toBeInTheDocument();
    // the counter is part of the description
    const counterId = box.getAttribute("aria-describedby")!;
    expect(document.getElementById(counterId)!.textContent).toContain("5 / 100");
    expect(container.querySelector("[aria-live=polite]")!.textContent).toBe("");
    await expectNoA11yViolations(container);
  });

  it("counterAnnouncement speaks only in the last 10% and over the limit", () => {
    expect(counterAnnouncement(10, 4000)).toBe("");
    expect(counterAnnouncement(3700, 4000)).toBe("300 characters left");
    expect(counterAnnouncement(4000, 4000)).toBe("Character limit reached");
    expect(counterAnnouncement(4010, 4000)).toBe("10 characters over the limit");
  });

  it("Textarea over the limit (pasted programmatically) is invalid", () => {
    render(<Textarea aria-label="Notes" value={"x".repeat(12)} onChange={() => {}} maxLength={10} />);
    expect(screen.getByLabelText("Notes")).toHaveAttribute("aria-invalid", "true");
  });

  it("Select is a labelled native select", async () => {
    const onChange = vi.fn();
    const { container } = render(
      <Field label="Model">
        <Select
          value="fast-default"
          onChange={onChange}
          options={[
            { value: "fast-default", label: "fast-default" },
            { value: "reasoning-default", label: "reasoning-default" },
          ]}
        />
      </Field>,
    );
    await userEvent.selectOptions(screen.getByLabelText("Model"), "reasoning-default");
    expect(onChange).toHaveBeenCalledWith("reasoning-default");
    await expectNoA11yViolations(container);
  });

  it("Checkbox announces why it is disabled", async () => {
    const { container } = render(
      <Checkbox checked={false} onCheckedChange={() => {}} label="list_supplier_messages" disabled disabledReason="Untrusted text: never given to agents" />,
    );
    const box = screen.getByRole("checkbox", { name: "list_supplier_messages" });
    expect(box).toBeDisabled();
    expect(box).toHaveAccessibleDescription("Untrusted text: never given to agents");
    await expectNoA11yViolations(container);
  });

  it("Switch toggles with click and keyboard and exposes aria-checked", async () => {
    function Harness() {
      const [on, setOn] = useState(false);
      return <Switch checked={on} onCheckedChange={setOn} label="Grant to all tenants" description="Adds the alias to every team." />;
    }
    const { container } = render(<Harness />);
    const sw = screen.getByRole("switch", { name: "Grant to all tenants" });
    expect(sw).toHaveAttribute("aria-checked", "false");
    await userEvent.click(sw);
    expect(sw).toHaveAttribute("aria-checked", "true");
    sw.focus();
    await userEvent.keyboard(" ");
    expect(sw).toHaveAttribute("aria-checked", "false");
    await expectNoA11yViolations(container);
  });
});

describe("Table", () => {
  type Row = { alias: string; mode: string; review: boolean };
  const rows: Row[] = [
    { alias: "fast-default", mode: "chat", review: false },
    { alias: "reasoning-default", mode: "chat", review: true },
  ];
  it("renders a captioned, scrollable, keyboard-reachable table with row headers and tones", async () => {
    const { container } = render(
      <Table<Row>
        caption="Models"
        rows={rows}
        rowKey={(r) => r.alias}
        rowTone={(r) => (r.review ? "warning" : undefined)}
        columns={[
          { id: "alias", header: "Alias", cell: (r) => r.alias, rowHeader: true, nowrap: true },
          { id: "mode", header: "Mode", cell: (r) => r.mode },
          { id: "act", header: "Actions", srOnlyHeader: true, cell: () => <button type="button">Edit</button> },
        ]}
      />,
    );
    const table = screen.getByRole("table", { name: "Models" });
    expect(within(table).getAllByRole("rowheader").map((c) => c.textContent)).toEqual(["fast-default", "reasoning-default"]);
    expect(screen.getByRole("group", { name: "Models" })).toHaveAttribute("tabindex", "0");
    expect(container.querySelector("tr[data-tone=warning]")).not.toBeNull();
    await expectNoA11yViolations(container);
  });

  it("shows the empty message", () => {
    render(<Table<Row> caption="Models" rows={[]} rowKey={(r) => r.alias} columns={[{ id: "a", header: "Alias", cell: (r) => r.alias }]} empty="No models yet." />);
    expect(screen.getByText("No models yet.")).toBeInTheDocument();
  });
});

describe("StepLog", () => {
  const base: StepLogItem[] = [
    { id: "url_policy", label: "URL policy", state: "pending" },
    { id: "reach", label: "Reach", state: "pending" },
  ];
  it("spells out each state and announces changes", async () => {
    const { container, rerender } = render(<StepLog label="Connection test" steps={base} />);
    expect(screen.getByRole("list", { name: "Connection test" })).toBeInTheDocument();
    expect(screen.getAllByText("Waiting")).toHaveLength(2);
    rerender(
      <StepLog
        label="Connection test"
        steps={[
          { id: "url_policy", label: "URL policy", state: "ok", ms: 3 },
          { id: "reach", label: "Reach", state: "failed", ms: 1200, detail: "connection refused" },
        ]}
      />,
    );
    expect(screen.getByText("Passed")).toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("1.2 s")).toBeInTheDocument();
    expect(screen.getByText("connection refused")).toBeInTheDocument();
    expect(screen.getByRole("status").textContent).toBe("URL policy: passed. Reach: failed");
    await expectNoA11yViolations(container);
  });

  it("summarises many changes at once", () => {
    const prev = ["a", "b", "c", "d"].map((id) => ({ id, label: id.toUpperCase(), state: "pending" as const }));
    const next = prev.map((s, i) => ({ ...s, state: i === 3 ? ("failed" as const) : ("ok" as const) }));
    expect(stepLogAnnouncement(prev, next)).toBe("D: failed. 3 of 4 steps passed.");
  });
});

describe("diffJson / JsonDiff", () => {
  const v1 = {
    version: "v1",
    models: { intake: "fast-default", recommender: "reasoning-default" },
    specialists: [{ slug: "inventory_demand", tools: ["get_inventory", "get_entity"], max_tool_calls: 15 }],
    policy: { auto_max_lines: 10 },
  };
  const v2 = {
    version: "v2",
    models: { intake: "fast-default", recommender: "fast-default" },
    specialists: [{ slug: "inventory_demand", tools: ["get_inventory", "list_items"], max_tool_calls: 15 }],
    policy: { auto_max_lines: 5 },
    prompts: { intake: "Prefer EU DCs." },
  };

  it("reports changes by path, matches arrays of objects by id and lists set members", () => {
    const d = diffJson(v1, v2);
    expect(d.map((e) => [e.path, e.kind])).toEqual([
      ["models.recommender", "changed"],
      ["policy.auto_max_lines", "changed"],
      ["prompts", "added"],
      ["specialists[inventory_demand].tools", "changed"],
      ["version", "changed"],
    ]);
    const tools = d.find((e) => e.path.endsWith("tools"))!;
    expect(tools.added).toEqual(["list_items"]);
    expect(tools.removed).toEqual(["get_entity"]);
  });

  it("jsonEqual ignores key order and undefined", () => {
    expect(jsonEqual({ a: 1, b: [1, { c: 2 }] }, { b: [1, { c: 2 }], a: 1, x: undefined })).toBe(true);
    expect(jsonEqual([1, 2], [2, 1])).toBe(false);
  });

  it("renders a labelled diff table, honours ignorePaths and has no a11y violations", async () => {
    const { container } = render(<JsonDiff caption="Changes from v1 to v2" before={v1} after={v2} beforeLabel="v1" afterLabel="v2" ignorePaths={["version"]} />);
    const table = screen.getByRole("table", { name: "Changes from v1 to v2" });
    expect(within(table).queryByText("version")).toBeNull();
    expect(within(table).getByText("specialists[inventory_demand].tools")).toBeInTheDocument();
    expect(within(table).getByText("list_items")).toBeInTheDocument();
    await expectNoA11yViolations(container);
  });

  it("says when nothing changed", () => {
    render(<JsonDiff caption="x" before={{ a: 1 }} after={{ a: 1 }} empty="Same as the current version." />);
    expect(screen.getByText("Same as the current version.")).toBeInTheDocument();
  });
});

describe("Meter", () => {
  it("exposes value and spoken text, and turns warning near the limit", async () => {
    const { container } = render(<Meter label="Budget used" value={42} max={50} valueText="€42 of €50" />);
    const m = screen.getByRole("meter", { name: "Budget used" });
    expect(m).toHaveAttribute("aria-valuetext", "€42 of €50");
    expect(m).toHaveAttribute("data-tone", "warning");
    await expectNoA11yViolations(container);
  });
});

describe("ConfirmDialog / Drawer", () => {
  it("confirms asynchronously and stays open when onConfirm returns false", async () => {
    let result: boolean = false;
    const onConfirm = vi.fn(async () => result);
    function Harness() {
      const [open, setOpen] = useState(true);
      return (
        <ConfirmDialog
          open={open}
          onOpenChange={setOpen}
          title="Rotate the gateway key?"
          description="Running cases switch to the new key."
          confirmLabel="Rotate key"
          tone="danger"
          onConfirm={onConfirm}
        />
      );
    }
    render(<Harness />);
    const dialog = await screen.findByRole("alertdialog", { name: "Rotate the gateway key?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Rotate key" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    result = true;
    await userEvent.click(within(dialog).getByRole("button", { name: "Rotate key" }));
    expect(onConfirm).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
  });

  it("Drawer is a labelled dialog with a size", async () => {
    render(
      <Drawer open onOpenChange={() => {}} title="Versions" size="lg">
        <p>History</p>
      </Drawer>,
    );
    const d = await screen.findByRole("dialog", { name: "Versions" });
    expect(d.className).toContain("720px");
  });
});
