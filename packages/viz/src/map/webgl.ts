let cached: boolean | null = null;

/** WebGL2 available (MapLibre 6 and deck.gl 9 need it). Cached; `false` outside the browser. */
export function supportsWebGL2(): boolean {
  if (cached !== null) return cached;
  if (typeof document === "undefined") return false;
  try {
    const canvas = document.createElement("canvas");
    cached = typeof WebGL2RenderingContext !== "undefined" && !!canvas.getContext("webgl2");
  } catch {
    cached = false;
  }
  return cached;
}
