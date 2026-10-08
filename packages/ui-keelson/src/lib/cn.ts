import { clsx, type ClassValue } from "clsx";

/** Join class names (clsx). Consumer classes are appended after component defaults. */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}
