import { describe, expect, it } from "vitest";
import { runningActivityLabel } from "./activityLabels";

describe("runningActivityLabel", () => {
  it.each([
    ["list_purchase_orders", "Checking list purchase orders in Merxa", "Reading purchase orders from Merxa"],
    ["get_inventory", "Checking get inventory in Merxa", "Reading inventory positions from Merxa"],
    ["get_demand_forecast", "Checking get demand forecast in Merxa", "Reading the demand forecast from Merxa"],
    ["get_bom_where_used", "Checking get bom where used in Merxa", "Checking where the components are used in Merxa"],
    ["search_entities", "Checking search entities in Acme ERP", "Searching Acme ERP"],
    ["calc", "Checking calc in Merxa", "Calculating over the evidence"],
    ["list_sailings", "Checking list sailings in Merxa", "Reading sailings from Merxa"],
    ["get_factory_schedule", "Checking get factory schedule in Merxa", "Reading the factory schedule from Merxa"],
    ["list_items", "Checking list items in Merxa Lite", "Reading records from Merxa Lite"],
  ])("%s: %s → %s", (tool, label, expected) => {
    expect(runningActivityLabel(tool, label)).toBe(expected);
  });

  it("keeps any label that is not the mechanical tool-name form", () => {
    expect(runningActivityLabel("get_inventory", "Reading stock for the UK DCs")).toBe("Reading stock for the UK DCs");
    expect(runningActivityLabel("get_inventory", "Checking stock in Merxa")).toBe("Checking stock in Merxa");
  });

  it("never shows an unknown tool's name", () => {
    const label = runningActivityLabel("list_widget_bins", "Checking list widget bins in Merxa");
    expect(label).toBe("Looking things up in Merxa");
    expect(runningActivityLabel(null, null)).toBe("Checking your systems");
    expect(runningActivityLabel("get_inventory", "")).toBe("Reading inventory positions from your systems");
  });

  it("tool names that are Object.prototype keys are unknown tools, not functions (third-party MCP)", () => {
    for (const tool of ["constructor", "toString", "hasOwnProperty", "__proto__", "valueOf"]) {
      const label = runningActivityLabel(tool, `Checking ${tool.replace(/_/g, " ")} in Acme ERP`);
      expect(typeof label).toBe("string");
      expect(label).toBe("Looking things up in Acme ERP");
    }
  });
});
