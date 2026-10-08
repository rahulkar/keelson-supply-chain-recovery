import { blockRegistry, CANVAS_BLOCK_KEYS, networkMapConfig } from "@keelson/viz";
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it, vi } from "vitest";
import { createBlockDataLoader } from "./dataLoader";
import { canvasBlockComponents, MAPLIBRE_WORKER_URL } from "./canvasBlocks";

describe("canvas visuals (next/dynamic, ssr: false)", () => {
  it("replace the package's lazy registrations for all four M3 kinds", () => {
    expect(Object.keys(canvasBlockComponents).sort()).toEqual([...CANVAS_BLOCK_KEYS].sort());
    for (const key of CANVAS_BLOCK_KEYS) expect(blockRegistry[key]).toBe(canvasBlockComponents[key]);
  });

  it("point MapLibre at the same-origin worker copied into public/maplibre", () => {
    expect(MAPLIBRE_WORKER_URL).toBe("/maplibre/maplibre-gl-worker.mjs");
    expect(networkMapConfig().workerUrl).toBe(MAPLIBRE_WORKER_URL);
    const script = readFileSync(path.join(process.cwd(), "scripts", "copy-maplibre-worker.mjs"), "utf8");
    expect(script).toContain('"maplibre-gl-worker.mjs", "maplibre-gl-shared.mjs"');
    const pkg = JSON.parse(readFileSync(path.join(process.cwd(), "package.json"), "utf8")) as { scripts: Record<string, string> };
    expect(pkg.scripts["build"]).toMatch(/^node scripts\/copy-maplibre-worker\.mjs && next build$/);
  });
});

describe("createBlockDataLoader", () => {
  it("fetches the block's own endpoint with the abort signal, whatever data_ref says", async () => {
    const blockData = vi.fn().mockResolvedValue({ rows: [] });
    const load = createBlockDataLoader({ blockData });
    const ctrl = new AbortController();
    const envelope = { id: "blk 1", kind: "stockout_timeline", v: 1, title: "", placement: "canvas" as const, provenance: null, data_ref: "https://elsewhere/x", props: {} };
    await expect(load(envelope, { signal: ctrl.signal })).resolves.toEqual({ rows: [] });
    expect(blockData).toHaveBeenCalledWith("blk 1", { signal: ctrl.signal });
  });
});
