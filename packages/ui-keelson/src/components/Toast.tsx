"use client";

import { Toast as BaseToast } from "@base-ui/react/toast";
import { CircleAlert, CircleCheck, Info, X } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "../lib/cn";

export type ToastType = "info" | "success" | "error";

export type ToastOptions = {
  description?: ReactNode | undefined;
  type?: ToastType | undefined;
  /** ms before auto-dismiss (0 = sticky). Default 5000; errors 8000. */
  timeout?: number | undefined;
  /** Optional action button (e.g. "Open case"). */
  action?: ({ label: string; onClick: () => void }) | undefined;
};

type ToastManager = ReturnType<typeof BaseToast.createToastManager>;

/**
 * Base UI's toast manager only forwards to providers that are already subscribed, and the
 * `<Toaster />` subscribes in an effect. Effects run child-first, so a toast raised while a page
 * mounts (e.g. "Merxa connected" when returning from the connect flow) reached nobody and was
 * silently lost. This manager queues adds/updates/closes until a Toaster subscribes, then
 * replays them in order.
 */
export function createBufferedToastManager(): ToastManager {
  const inner = BaseToast.createToastManager();
  const queue: { id: string; run: () => void }[] = [];
  let subscribers = 0;
  let seq = 0;
  return {
    ...inner,
    " subscribe": (listener) => {
      const off = inner[" subscribe"](listener);
      subscribers += 1;
      for (const item of queue.splice(0)) item.run();
      return () => {
        subscribers -= 1;
        off();
      };
    },
    add: (options) => {
      if (subscribers > 0) return inner.add(options);
      seq += 1;
      const id = options.id ?? `toast-early-${seq}`;
      queue.push({ id, run: () => inner.add({ ...options, id }) });
      return id;
    },
    update: (id, updates) => {
      if (subscribers > 0) return inner.update(id, updates);
      queue.push({ id, run: () => inner.update(id, updates) });
    },
    close: (id) => {
      if (subscribers > 0) return inner.close(id);
      for (let i = queue.length - 1; i >= 0; i--) if (id === undefined || queue[i]!.id === id) queue.splice(i, 1);
    },
  };
}

/** App-wide toast queue; render `<Toaster />` once. Toasts raised before it mounts are kept. */
export const toastManager = createBufferedToastManager();

function add(title: ReactNode, opts: ToastOptions = {}): string {
  const type = opts.type ?? "info";
  return toastManager.add({
    title,
    description: opts.description,
    type,
    timeout: opts.timeout ?? (type === "error" ? 8000 : 5000),
    priority: type === "error" ? "high" : "low",
    actionProps: opts.action ? { children: opts.action.label, onClick: opts.action.onClick } : undefined,
  });
}

/** Sonner-like minimal API: `toast("Saved")`, `toast.success(…)`, `toast.error(…)`, `toast.dismiss(id)`. */
export const toast = Object.assign(add, {
  success: (title: ReactNode, opts: Omit<ToastOptions, "type"> = {}) => add(title, { ...opts, type: "success" }),
  error: (title: ReactNode, opts: Omit<ToastOptions, "type"> = {}) => add(title, { ...opts, type: "error" }),
  dismiss: (id: string) => toastManager.close(id),
});

const icons: Record<ToastType, ReactNode> = {
  info: <Info className="size-4.5 text-info" />,
  success: <CircleCheck className="size-4.5 text-success" />,
  error: <CircleAlert className="size-4.5 text-danger" />,
};

function ToastList() {
  const { toasts } = BaseToast.useToastManager();
  return toasts.map((t) => (
    <BaseToast.Root
      key={t.id}
      toast={t}
      className={cn(
        "pointer-events-auto flex w-full items-start gap-3 rounded-lg border border-border bg-surface p-3 pr-2 text-fg shadow-lg",
        "transition-[opacity,translate] duration-200 ease-standard data-starting-style:-translate-y-2 data-starting-style:opacity-0 data-ending-style:opacity-0",
      )}
    >
      <span aria-hidden="true" className="mt-0.5 inline-flex">
        {icons[(t.type as ToastType | undefined) ?? "info"] ?? icons.info}
      </span>
      <div className="min-w-0 flex-1">
        <BaseToast.Title className="text-ui font-medium" />
        <BaseToast.Description className="text-ui text-fg-muted" />
      </div>
      {t.actionProps ? (
        <BaseToast.Action className="h-8 rounded-pill px-3 text-ui font-medium text-accent hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-focus" />
      ) : null}
      <BaseToast.Close
        aria-label="Dismiss"
        className="inline-flex size-8 shrink-0 items-center justify-center rounded-pill text-fg-muted hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-focus"
      >
        <X aria-hidden="true" className="size-4" />
      </BaseToast.Close>
    </BaseToast.Root>
  ));
}

/**
 * Toast viewport, top right under the app header (full width on phones). Mount once near the root.
 * Not at the bottom: that is where the composer and its Send / Stop button live.
 */
export function Toaster({ limit = 3 }: { limit?: number | undefined }) {
  return (
    <BaseToast.Provider toastManager={toastManager} limit={limit}>
      <BaseToast.Portal>
        <BaseToast.Viewport
          data-placement="top"
          className="pointer-events-none fixed top-16 right-4 left-4 z-[60] flex flex-col items-stretch gap-2 sm:left-auto sm:w-[22.5rem]"
        >
          <ToastList />
        </BaseToast.Viewport>
      </BaseToast.Portal>
    </BaseToast.Provider>
  );
}
