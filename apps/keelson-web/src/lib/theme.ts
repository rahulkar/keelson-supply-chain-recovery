/**
 * Light / dark / system theme. Tokens switch on `<html data-theme="dark">` only, so the choice is
 * applied by a tiny pre-paint script (no flash) and remembered per browser.
 */
export type ThemeChoice = "light" | "dark" | "system";
export const THEME_STORAGE_KEY = "keelson-theme";

export function readThemeChoice(): ThemeChoice {
  try {
    const v = window.localStorage.getItem(THEME_STORAGE_KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function resolveTheme(choice: ThemeChoice, prefersDark: boolean): "light" | "dark" {
  return choice === "system" ? (prefersDark ? "dark" : "light") : choice;
}

export function applyThemeChoice(choice: ThemeChoice): void {
  try {
    if (choice === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, choice);
  } catch {
    // storage blocked: still apply for this page
  }
  const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
  document.documentElement.dataset["theme"] = resolveTheme(choice, dark);
}

/** Inline, dependency-free pre-paint script (runs before React hydrates). */
export const THEME_BOOT_SCRIPT = `(function(){try{var c=localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY,
)});var d=c==="dark"||(c!=="light"&&window.matchMedia("(prefers-color-scheme: dark)").matches);document.documentElement.dataset.theme=d?"dark":"light"}catch(e){document.documentElement.dataset.theme="light"}})();`;
