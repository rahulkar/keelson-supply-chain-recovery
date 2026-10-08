"use client";

import { Button as BaseButton } from "@base-ui/react/button";
import { forwardRef, type ComponentPropsWithoutRef, type ReactNode } from "react";
import { cn } from "../lib/cn";
import { Spinner } from "./Spinner";
import { Tooltip } from "./Tooltip";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "subtle" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

const base =
  "inline-flex items-center justify-center gap-2 whitespace-nowrap font-medium select-none " +
  "transition-[background-color,color,border-color,box-shadow] duration-150 ease-standard " +
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus " +
  "disabled:cursor-not-allowed data-disabled:cursor-not-allowed disabled:opacity-55 data-disabled:opacity-55";

const variants: Record<ButtonVariant, string> = {
  primary: "bg-accent text-accent-fg hover:not-data-disabled:bg-accent-hover shadow-sm",
  secondary:
    "bg-surface text-fg border border-border-strong hover:not-data-disabled:bg-surface-2 active:not-data-disabled:bg-surface-3",
  ghost: "bg-transparent text-fg hover:not-data-disabled:bg-surface-2 active:not-data-disabled:bg-surface-3",
  subtle: "bg-accent-soft text-accent-soft-fg hover:not-data-disabled:bg-surface-3",
  danger: "bg-danger text-fg-inverse hover:not-data-disabled:opacity-90",
};

const sizes: Record<ButtonSize, string> = {
  sm: "h-8 px-3 text-caption rounded-pill",
  md: "h-10 px-4 text-ui rounded-pill",
  lg: "h-12 px-6 text-body rounded-pill",
};

export type ButtonProps = Omit<ComponentPropsWithoutRef<"button">, "className"> & {
  variant?: ButtonVariant | undefined;
  size?: ButtonSize | undefined;
  /** Shows a spinner, sets `aria-busy` and disables the button. */
  loading?: boolean | undefined;
  /** Icon before the label. */
  icon?: ReactNode | undefined;
  className?: string | undefined;
};

/** Pill button (Base UI `Button`). Disabled buttons stay focusable so their reason can be read. */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading = false, icon, disabled, className, children, type = "button", ...rest },
  ref,
) {
  return (
    <BaseButton
      ref={ref}
      type={type}
      disabled={disabled || loading}
      focusableWhenDisabled
      aria-busy={loading || undefined}
      className={cn(base, variants[variant], sizes[size], className)}
      {...rest}
    >
      {loading ? <Spinner size={size === "lg" ? 18 : 16} /> : icon}
      {children}
    </BaseButton>
  );
});

export type IconButtonProps = Omit<ButtonProps, "icon" | "children" | "size"> & {
  /** Accessible name (required) — also shown as a tooltip unless `tooltip={false}`. */
  label: string;
  icon: ReactNode;
  size?: ButtonSize | undefined;
  tooltip?: boolean | undefined;
};

const iconSizes: Record<ButtonSize, string> = {
  sm: "size-8 rounded-pill",
  md: "size-10 rounded-pill",
  lg: "size-12 rounded-pill",
};

/** Round icon-only button with a required accessible label. */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { label, icon, variant = "ghost", size = "md", tooltip = true, loading = false, disabled, className, type = "button", ...rest },
  ref,
) {
  const button = (
    <BaseButton
      ref={ref}
      type={type}
      aria-label={label}
      disabled={disabled || loading}
      focusableWhenDisabled
      aria-busy={loading || undefined}
      className={cn(base, variants[variant], iconSizes[size], "p-0", className)}
      {...rest}
    >
      {loading ? <Spinner size={16} /> : <span aria-hidden="true" className="inline-flex">{icon}</span>}
    </BaseButton>
  );
  return tooltip ? <Tooltip content={label}>{button}</Tooltip> : button;
});
