"use client";

import { AlertDialog } from "@base-ui/react/alert-dialog";
import { Dialog as BaseDialog } from "@base-ui/react/dialog";
import { X } from "lucide-react";
import { useRef, useState, type ComponentProps, type ReactElement, type ReactNode } from "react";
import { cn } from "../lib/cn";
import { Button } from "./Button";

type DialogBaseProps = {
  open?: boolean | undefined;
  onOpenChange?: ((open: boolean) => void) | undefined;
  /** Optional trigger element (rendered via Base UI `render`). */
  trigger?: ReactElement | undefined;
  title: ReactNode;
  /** Visually hide the title (it still names the dialog). */
  hideTitle?: boolean | undefined;
  description?: ReactNode | undefined;
  children?: ReactNode | undefined;
  footer?: ReactNode | undefined;
  /** Label for the close button (default "Close"). */
  closeLabel?: string | undefined;
  className?: string | undefined;
};

function CloseButton({ label }: { label: string }) {
  return (
    <BaseDialog.Close
      aria-label={label}
      className="inline-flex size-9 items-center justify-center rounded-pill text-fg-muted hover:bg-surface-2 hover:text-fg focus-visible:outline-2 focus-visible:outline-focus"
    >
      <X aria-hidden="true" className="size-4.5" />
    </BaseDialog.Close>
  );
}

function Header({ title, hideTitle, description, closeLabel }: Pick<DialogBaseProps, "title" | "hideTitle" | "description"> & { closeLabel: string }) {
  return (
    <div className="flex items-start gap-3">
      <div className="min-w-0 flex-1">
        <BaseDialog.Title className={cn("text-heading font-semibold text-fg", hideTitle && "sr-only")}>{title}</BaseDialog.Title>
        {description ? <BaseDialog.Description className="mt-1 text-ui text-fg-muted">{description}</BaseDialog.Description> : null}
      </div>
      <CloseButton label={closeLabel} />
    </div>
  );
}

const backdrop =
  "fixed inset-0 z-40 bg-scrim transition-opacity duration-200 data-starting-style:opacity-0 data-ending-style:opacity-0";

/** Centred modal dialog (Base UI): focus trap, Esc to close, labelled by its title. */
export function Dialog({ open, onOpenChange, trigger, title, hideTitle, description, children, footer, closeLabel = "Close", className }: DialogBaseProps) {
  return (
    <BaseDialog.Root open={open} onOpenChange={onOpenChange ? (next) => onOpenChange(next) : undefined}>
      {trigger ? <BaseDialog.Trigger render={trigger} /> : null}
      <BaseDialog.Portal>
        <BaseDialog.Backdrop className={backdrop} />
        <BaseDialog.Popup
          className={cn(
            "fixed top-1/2 left-1/2 z-50 flex max-h-[85dvh] w-[min(560px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 flex-col gap-4",
            "rounded-xl border border-border bg-surface p-5 text-fg shadow-lg outline-none",
            "transition-[opacity,scale] duration-200 ease-standard data-starting-style:scale-95 data-starting-style:opacity-0 data-ending-style:scale-95 data-ending-style:opacity-0",
            className,
          )}
        >
          <Header title={title} hideTitle={hideTitle} description={description} closeLabel={closeLabel} />
          {children ? <div className="min-h-0 overflow-y-auto">{children}</div> : null}
          {footer ? <div className="flex flex-wrap justify-end gap-2">{footer}</div> : null}
        </BaseDialog.Popup>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}

export type SheetProps = DialogBaseProps & {
  /** Edge the sheet slides from (default right; bottom on phones). */
  side?: "right" | "left" | "bottom" | undefined;
  /** Non-modal sheets let the page stay interactive. */
  modal?: boolean | undefined;
  bodyClassName?: string | undefined;
  /** Where focus goes on close (Base UI `finalFocus`; default: the trigger / previously focused element). */
  finalFocus?: ComponentProps<typeof BaseDialog.Popup>["finalFocus"];
  /** Width of a right-hand sheet (default `w-[min(560px,100vw)]`). */
  widthClassName?: string | undefined;
};

const sheetSide = {
  right:
    "inset-y-0 right-0 h-dvh rounded-l-xl border-l data-starting-style:translate-x-full data-ending-style:translate-x-full",
  left: "inset-y-0 left-0 h-dvh w-[min(420px,100vw)] rounded-r-xl border-r data-starting-style:-translate-x-full data-ending-style:-translate-x-full",
  bottom:
    "inset-x-0 bottom-0 max-h-[88dvh] w-full rounded-t-xl border-t data-starting-style:translate-y-full data-ending-style:translate-y-full",
} as const;

/** Edge sheet (Base UI Dialog): history drawer, small-screen canvas, details. */
export function Sheet({
  open,
  onOpenChange,
  trigger,
  title,
  hideTitle,
  description,
  children,
  footer,
  closeLabel = "Close",
  side = "right",
  modal = true,
  className,
  bodyClassName,
  finalFocus,
  widthClassName,
}: SheetProps) {
  return (
    <BaseDialog.Root open={open} modal={modal} onOpenChange={onOpenChange ? (next) => onOpenChange(next) : undefined}>
      {trigger ? <BaseDialog.Trigger render={trigger} /> : null}
      <BaseDialog.Portal>
        {modal ? <BaseDialog.Backdrop className={backdrop} /> : null}
        <BaseDialog.Popup
          finalFocus={finalFocus}
          className={cn(
            "fixed z-50 flex flex-col border-border bg-surface text-fg shadow-lg outline-none",
            "transition-transform duration-300 ease-emphasized",
            sheetSide[side],
            side === "right" && (widthClassName ?? "w-[min(560px,100vw)]"),
            className,
          )}
        >
          <div className="px-5 pt-4 pb-2">
            {side === "bottom" ? <div aria-hidden="true" className="mx-auto mb-3 h-1 w-10 rounded-pill bg-border-strong" /> : null}
            <Header title={title} hideTitle={hideTitle} description={description} closeLabel={closeLabel} />
          </div>
          <div className={cn("min-h-0 flex-1 overflow-y-auto px-5 pb-5", bodyClassName)}>{children}</div>
          {footer ? <div className="flex flex-wrap justify-end gap-2 border-t border-border px-5 py-3">{footer}</div> : null}
        </BaseDialog.Popup>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}

export type DrawerProps = Omit<SheetProps, "side"> & {
  /** Width: md 560 px (default), lg 720 px, xl 960 px; full width on phones. */
  size?: "md" | "lg" | "xl" | undefined;
};

const drawerWidths = { md: "w-[min(560px,100vw)]", lg: "w-[min(720px,100vw)]", xl: "w-[min(960px,100vw)]" } as const;

/** Right-hand detail drawer (version history, record details): a wider `Sheet`. */
export function Drawer({ size = "md", className, ...rest }: DrawerProps) {
  return <Sheet {...rest} side="right" widthClassName={drawerWidths[size]} className={className} />;
}

export type ConfirmDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: ReactNode;
  description?: ReactNode | undefined;
  children?: ReactNode | undefined;
  confirmLabel: string;
  cancelLabel?: string | undefined;
  /** `danger` for destructive confirmations (delete, rotate a key). */
  tone?: "default" | "danger" | undefined;
  /** Async confirm: the dialog stays open with a spinner until it settles; closes on success. */
  onConfirm: () => Promise<boolean | void> | boolean | void;
  /** Disable the confirm button (e.g. a required acknowledgement is missing). */
  confirmDisabled?: boolean | undefined;
};

/**
 * Confirmation (Base UI AlertDialog, `role="alertdialog"`): no outside-click dismissal, focus
 * starts on Cancel. `onConfirm` returning `false` keeps the dialog open (e.g. the call failed).
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  confirmLabel,
  cancelLabel = "Cancel",
  tone = "default",
  onConfirm,
  confirmDisabled,
}: ConfirmDialogProps) {
  const [busy, setBusy] = useState(false);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const confirm = async () => {
    setBusy(true);
    try {
      const out = await onConfirm();
      if (out !== false) onOpenChange(false);
    } finally {
      setBusy(false);
    }
  };
  return (
    <AlertDialog.Root open={open} onOpenChange={(next) => (busy ? undefined : onOpenChange(next))}>
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className={backdrop} />
        <AlertDialog.Popup
          initialFocus={cancelRef}
          className={cn(
            "fixed top-1/2 left-1/2 z-50 flex max-h-[85dvh] w-[min(440px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 flex-col gap-3",
            "rounded-xl border border-border bg-surface p-5 text-fg shadow-lg outline-none",
            "transition-[opacity,scale] duration-200 ease-standard data-starting-style:scale-95 data-starting-style:opacity-0 data-ending-style:scale-95 data-ending-style:opacity-0",
          )}
        >
          <AlertDialog.Title className="text-body font-semibold text-fg">{title}</AlertDialog.Title>
          {description ? <AlertDialog.Description className="text-ui text-fg-muted">{description}</AlertDialog.Description> : null}
          {children ? <div className="min-h-0 overflow-y-auto text-ui">{children}</div> : null}
          <div className="mt-1 flex flex-wrap justify-end gap-2">
            <Button ref={cancelRef} size="sm" variant="ghost" disabled={busy} onClick={() => onOpenChange(false)}>
              {cancelLabel}
            </Button>
            <Button
              size="sm"
              variant={tone === "danger" ? "danger" : "primary"}
              loading={busy}
              disabled={confirmDisabled}
              onClick={() => void confirm()}
            >
              {confirmLabel}
            </Button>
          </div>
        </AlertDialog.Popup>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
