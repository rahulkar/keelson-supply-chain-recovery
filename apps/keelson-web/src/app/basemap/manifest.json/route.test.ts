import { describe, expect, it } from "vitest";
import { GET } from "./route";

describe("/basemap/manifest.json without a base map route", () => {
  it("answers 204 (nothing installed), never a page", async () => {
    const res = GET();
    expect(res.status).toBe(204);
    expect(res.headers.get("cache-control")).toBe("no-cache");
    expect(await res.text()).toBe("");
  });
});
