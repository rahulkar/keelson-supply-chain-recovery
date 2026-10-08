"use client";

import { IconX } from "@tabler/icons-react";
import { useEffect, useId, useRef, type ReactNode } from "react";

const FOCUSABLE = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])';

/** Side drawer used for master-detail ("a list plus a drawer"; no Enterprise master/detail). */
export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  const titleId = useId();
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      // Modal dialog: keep Tab / Shift+Tab inside the drawer.
      if (e.key === "Tab" && panelRef.current) {
        const focusable = [...panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => !el.hasAttribute("disabled"));
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (!first || !last) return;
        const active = document.activeElement;
        if (e.shiftKey && (active === first || !panelRef.current.contains(active))) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && (active === last || !panelRef.current.contains(active))) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, [onClose]);
  return (
    <>
      <div className="mx-drawer-backdrop" onClick={onClose} aria-hidden="true" />
      <aside ref={panelRef} className="mx-drawer" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <div className="mx-drawer__header">
          <h2 className="mx-drawer__title" id={titleId}>
            {title}
          </h2>
          <button ref={closeRef} type="button" className="mx-btn mx-btn--ghost mx-btn--sm" onClick={onClose} aria-label="Close">
            <IconX size={16} aria-hidden="true" />
          </button>
        </div>
        <div className="mx-drawer__body">{children}</div>
      </aside>
    </>
  );
}
