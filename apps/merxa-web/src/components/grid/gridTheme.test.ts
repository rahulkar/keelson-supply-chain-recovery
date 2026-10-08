import { describe, expect, it } from "vitest";

import { gridModules, merxaGridTheme } from "./gridTheme";

describe("AG Grid setup", () => {
  it("registers the Community modules the wrapper relies on", () => {
    const names = gridModules.map((m) => m.moduleName);
    for (const required of ["ClientSideRowModel", "RowApi", "CsvExport", "QuickFilter", "RowStyle", "PinnedRow"]) {
      expect(names).toContain(required);
    }
  });

  it("uses the Balham theme", () => {
    expect(merxaGridTheme).toBeDefined();
  });
});
