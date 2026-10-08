/** Type declarations for scripts/gen-tokens.mjs (used by tests). */
export declare function toHex(value: string): string;
export declare function renderCss(): string;
export declare function renderHexTs(): string;
/** Absolute output path → expected contents. */
export declare function outputs(): Map<string, string>;
/** Line-ending-insensitive text equality (`null` never matches). */
export declare function sameText(a: string | null, b: string | null): boolean;
