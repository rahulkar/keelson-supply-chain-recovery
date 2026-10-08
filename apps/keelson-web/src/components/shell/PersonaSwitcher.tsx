"use client";

import { Menu } from "@base-ui/react/menu";
import { Avatar, Spinner, toast } from "@keelson/ui";
import { Check, ChevronDown, Clapperboard, RotateCcw, Users } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { describeError } from "@/lib/api/client";
import type { Persona } from "@/lib/api/types";
import { useSession } from "@/lib/client/session";
import { DemoPacksDialog } from "./DemoPacks";
import { canResetDemo, DemoResetDialog } from "./DemoReset";
import { menuItem, menuLabel, menuPopup, menuSeparator } from "./menuStyles";

type Load = { status: "idle" | "loading" | "error" } | { status: "ready"; personas: Persona[] };

function defaultReload(): void {
  window.location.reload();
}

/**
 * DEMO persona switcher: `GET /api/demo/personas`, then `POST /api/demo/login-as {username}`
 * (server-side password grant on `keelson-demo`; the response only sets a new session cookie)
 * and a full reload so every server-rendered gate re-evaluates for the new persona.
 */
export function PersonaSwitcher({ reload = defaultReload, afterReset }: { reload?: () => void; afterReset?: (() => void) | undefined }) {
  const { me, api } = useSession();
  const router = useRouter();
  const [resetOpen, setResetOpen] = useState(false);
  const [packsOpen, setPacksOpen] = useState(false);
  const presenter = canResetDemo(me);
  const goHome = () => {
    if (afterReset) return afterReset();
    // Every case is gone: back to the home screen; the toast with the step log stays.
    router.push("/");
    router.refresh?.();
  };
  const [load, setLoad] = useState<Load>({ status: "idle" });
  const [switching, setSwitching] = useState<string | null>(null);

  const fetchPersonas = async () => {
    if (load.status === "ready" || load.status === "loading") return;
    setLoad({ status: "loading" });
    try {
      setLoad({ status: "ready", personas: await api.personas() });
    } catch (err) {
      setLoad({ status: "error" });
      toast.error("Couldn't load demo personas", { description: describeError(err) });
    }
  };

  const switchTo = async (username: string) => {
    if (username === me.user.username || switching) return;
    setSwitching(username);
    try {
      await api.loginAs(username);
      reload();
    } catch (err) {
      setSwitching(null);
      toast.error("Couldn't switch persona", { description: describeError(err) });
    }
  };

  const firstName = me.user.name.split(" ")[0] || me.user.username;

  return (
    <>
      <Menu.Root
        onOpenChange={(open) => {
          if (open) void fetchPersonas();
        }}
      >
        <Menu.Trigger
          className="inline-flex h-8 items-center gap-1.5 rounded-pill border border-dashed border-border-strong px-3 text-caption font-medium text-fg-muted hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          aria-label={`Demo persona: ${me.user.name}. Switch persona`}
        >
          <Users aria-hidden="true" className="size-3.5" />
          <span className="hidden sm:inline">Demo ·</span> {firstName}
          <ChevronDown aria-hidden="true" className="size-3.5" />
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Positioner side="bottom" align="end" sideOffset={6}>
            <Menu.Popup className={menuPopup}>
              <Menu.Group>
                <Menu.GroupLabel className={menuLabel}>View the demo as</Menu.GroupLabel>
                {load.status === "loading" || load.status === "idle" ? (
                  <div className="flex items-center gap-2 px-3 py-2 text-fg-muted" role="status">
                    <Spinner size={14} /> Loading personas…
                  </div>
                ) : null}
                {load.status === "error" ? <p className="px-3 py-2 text-fg-muted">Personas are unavailable.</p> : null}
                {load.status === "ready"
                  ? load.personas.map((p) => {
                      const current = p.username === me.user.username;
                      return (
                        <Menu.Item
                          key={p.username}
                          className={menuItem}
                          disabled={switching !== null}
                          closeOnClick={false}
                          onClick={() => void switchTo(p.username)}
                          aria-current={current || undefined}
                        >
                          <Avatar name={p.name} size="sm" decorative />
                          <span className="min-w-0 flex-1">
                            <span className="block truncate">{p.name}</span>
                            <span className="block truncate text-caption text-fg-muted">{p.role_label}</span>
                          </span>
                          {switching === p.username ? <Spinner size={14} /> : null}
                          {current ? <Check aria-label="Current persona" className="size-4 text-accent" /> : null}
                        </Menu.Item>
                      );
                    })
                  : null}
              </Menu.Group>
              {presenter ? (
                <>
                  <Menu.Separator className={menuSeparator} />
                  <Menu.Group>
                    <Menu.GroupLabel className={menuLabel}>Presenter</Menu.GroupLabel>
                    <Menu.Item className={menuItem} disabled={switching !== null} onClick={() => setPacksOpen(true)}>
                      <Clapperboard aria-hidden="true" className="size-4 text-fg-muted" />
                      Demo scenarios…
                    </Menu.Item>
                    <Menu.Item className={menuItem} disabled={switching !== null} onClick={() => setResetOpen(true)}>
                      <RotateCcw aria-hidden="true" className="size-4 text-fg-muted" />
                      Reset demo…
                    </Menu.Item>
                  </Menu.Group>
                </>
              ) : null}
            </Menu.Popup>
          </Menu.Positioner>
        </Menu.Portal>
      </Menu.Root>
      {presenter ? <DemoResetDialog open={resetOpen} onOpenChange={setResetOpen} onDone={goHome} /> : null}
      {presenter ? <DemoPacksDialog open={packsOpen} onOpenChange={setPacksOpen} /> : null}
    </>
  );
}
