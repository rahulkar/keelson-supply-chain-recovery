"use client";

import { Button, EmptyState, IconButton, Sheet, Skeleton } from "@keelson/ui";
import { History } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { CaseList } from "@/components/cases/CaseList";
import { describeError } from "@/lib/api/client";
import type { CaseSummary } from "@/lib/api/types";
import { useSession } from "@/lib/client/session";

type Load = { status: "idle" | "loading" } | { status: "error"; message: string } | { status: "ready"; items: CaseSummary[] };

const RECENT = 20;

/** History lives in a drawer (plan §4): recent cases, loaded each time it opens. */
export function HistoryDrawer() {
  const { api } = useSession();
  const [open, setOpen] = useState(false);
  const [load, setLoad] = useState<Load>({ status: "idle" });

  const refresh = async () => {
    setLoad({ status: "loading" });
    try {
      setLoad({ status: "ready", items: await api.listCases(RECENT) });
    } catch (err) {
      setLoad({ status: "error", message: describeError(err) });
    }
  };

  const onOpenChange = (next: boolean) => {
    setOpen(next);
    if (next) void refresh();
  };

  return (
    <>
      <IconButton label="History" icon={<History className="size-5" />} onClick={() => onOpenChange(true)} />
      <Sheet
        open={open}
        onOpenChange={onOpenChange}
        side="left"
        title="History"
        description="Your recent cases"
        footer={
          <Link
            href="/cases"
            onClick={() => setOpen(false)}
            className="text-ui font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-focus"
          >
            All cases
          </Link>
        }
      >
        {load.status === "loading" || load.status === "idle" ? (
          <div className="flex flex-col gap-3" role="status" aria-label="Loading cases" aria-busy="true">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-14 w-full" />
            ))}
          </div>
        ) : null}
        {load.status === "error" ? (
          <EmptyState
            title="Couldn't load your cases"
            description={load.message}
            action={
              <Button size="sm" onClick={() => void refresh()}>
                Try again
              </Button>
            }
          />
        ) : null}
        {load.status === "ready" && load.items.length === 0 ? (
          <EmptyState title="No cases yet" description="Describe a disruption on the home screen to start one." />
        ) : null}
        {load.status === "ready" && load.items.length > 0 ? (
          <CaseList items={load.items} onNavigate={() => setOpen(false)} />
        ) : null}
      </Sheet>
    </>
  );
}
