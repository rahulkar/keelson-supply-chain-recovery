"use client";

import { Menu } from "@base-ui/react/menu";
import { Avatar, toast } from "@keelson/ui";
import { Check, LogOut, Monitor, Moon, Settings, Sun } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { describeError } from "@/lib/api/client";
import { useSession } from "@/lib/client/session";
import { isAdmin, roleSummary } from "@/lib/roles";
import { applyThemeChoice, readThemeChoice, type ThemeChoice } from "@/lib/theme";
import { menuItem, menuLabel, menuPopup, menuSeparator } from "./menuStyles";

const THEMES: { id: ThemeChoice; label: string; icon: typeof Sun }[] = [
  { id: "system", label: "Match system", icon: Monitor },
  { id: "light", label: "Light", icon: Sun },
  { id: "dark", label: "Dark", icon: Moon },
];

/** Avatar menu: who you are (roles, workspace), theme, admin (role-gated) and sign out. */
export function UserMenu() {
  const { me, api } = useSession();
  const [theme, setTheme] = useState<ThemeChoice>("system");
  const [signingOut, setSigningOut] = useState(false);

  const signOut = async () => {
    setSigningOut(true);
    try {
      const next = await api.logout();
      window.location.assign(next ?? "/");
    } catch (err) {
      setSigningOut(false);
      toast.error("Couldn't sign out", { description: describeError(err) });
    }
  };

  return (
    <Menu.Root
      onOpenChange={(open) => {
        if (open) setTheme(readThemeChoice());
      }}
    >
      <Menu.Trigger
        aria-label={`Account: ${me.user.name}`}
        className="inline-flex size-10 items-center justify-center rounded-pill hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
      >
        <Avatar name={me.user.name} size="md" decorative />
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner side="bottom" align="end" sideOffset={6}>
          <Menu.Popup className={menuPopup}>
            <div className="flex items-center gap-3 px-3 py-2">
              <Avatar name={me.user.name} size="lg" decorative />
              <div className="min-w-0">
                <p className="truncate font-medium">{me.user.name}</p>
                <p className="truncate text-caption text-fg-muted">{roleSummary(me.user.roles)}</p>
                {me.user.active_tenant ? (
                  <p className="truncate text-caption text-fg-muted">Workspace: {me.user.active_tenant}</p>
                ) : null}
              </div>
            </div>
            <Menu.Separator className={menuSeparator} />
            <Menu.Group>
              <Menu.GroupLabel className={menuLabel}>Appearance</Menu.GroupLabel>
              <Menu.RadioGroup
                value={theme}
                onValueChange={(value: ThemeChoice) => {
                  setTheme(value);
                  applyThemeChoice(value);
                }}
              >
                {THEMES.map(({ id, label, icon: Icon }) => (
                  <Menu.RadioItem key={id} value={id} closeOnClick={false} className={menuItem}>
                    <Icon aria-hidden="true" className="size-4 text-fg-muted" />
                    <span className="flex-1">{label}</span>
                    <Menu.RadioItemIndicator className="inline-flex">
                      <Check aria-hidden="true" className="size-4 text-accent" />
                    </Menu.RadioItemIndicator>
                  </Menu.RadioItem>
                ))}
              </Menu.RadioGroup>
            </Menu.Group>
            <Menu.Separator className={menuSeparator} />
            {isAdmin(me.user) ? (
              <Menu.LinkItem render={<Link href="/admin" />} className={menuItem}>
                <Settings aria-hidden="true" className="size-4 text-fg-muted" />
                Admin
              </Menu.LinkItem>
            ) : null}
            <Menu.Item className={menuItem} disabled={signingOut} onClick={() => void signOut()}>
              <LogOut aria-hidden="true" className="size-4 text-fg-muted" />
              Sign out
            </Menu.Item>
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}
