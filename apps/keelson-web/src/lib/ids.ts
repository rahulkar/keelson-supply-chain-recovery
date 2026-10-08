/**
 * An engine resource id (case, run) that is safe to put in a URL path segment: 1–64 characters,
 * starting with a letter or digit (so never `.` / `..`, which `fetch` would normalise into a
 * different engine route). engine-api answers 404 for anything longer anyway.
 */
export function isResourceId(value: string): boolean {
  return /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/.test(value);
}

/**
 * RFC 4122 v4 UUID for `Idempotency-Key`. `crypto.randomUUID` only exists in secure contexts;
 * `getRandomValues` is available everywhere (plain-HTTP dev hosts included).
 */
export function uuid(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === "function") {
    try {
      return c.randomUUID();
    } catch {
      // insecure context in some browsers: fall through
    }
  }
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
