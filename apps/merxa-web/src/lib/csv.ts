/**
 * CSV export hardening. Grid cells can hold text written by external parties (change-batch
 * titles from integrations, supplier codes, audit details). Spreadsheet applications
 * evaluate cells starting with `=`, `+`, `-`, `@`, TAB or CR as formulas ("CSV injection"),
 * so such text values are prefixed with a single quote. Numbers (e.g. a negative ATP) are
 * exported unchanged.
 */
const FORMULA_START = /^[=+\-@\t\r]/;

export function csvSafeCell(raw: unknown, formatted: unknown): string {
  const text = formatted === null || formatted === undefined ? "" : String(formatted);
  if (typeof raw === "number" || typeof raw === "bigint") return text;
  return FORMULA_START.test(text) ? `'${text}` : text;
}
