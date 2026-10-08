/** Type declarations for scripts/gen-zod.mjs (used by tests). */
export declare function camel(name: string): string;
export declare function fileStem(name: string): string;
/** Every generated module in memory: relative file name → contents. */
export declare function generate(): Map<string, string>;
/** Line-ending-insensitive text equality (`null` never matches). */
export declare function sameText(a: string | null, b: string | null): boolean;
/** Rewrites closed 2020-12 `prefixItems` tuples into the draft-07 `items: [...]` form. */
export declare function legacyTuples<T>(node: T): T;
