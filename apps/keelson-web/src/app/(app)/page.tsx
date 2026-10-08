import { EmptyState } from "@keelson/ui";
import { ShieldCheck } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import { HomeComposer } from "@/components/home/HomeComposer";
import { NewAlertsPill } from "@/components/home/NewAlertsPill";
import { canReadCases, canWriteCases, isAdmin } from "@/lib/roles";
import { requireMe } from "@/lib/server/session";

export const metadata: Metadata = { title: "Home" };

export default async function HomePage() {
  const me = await requireMe();
  const firstName = me.user.name.split(" ")[0] || me.user.username;

  if (!canReadCases(me.user)) {
    return (
      <div className="mx-auto flex w-full max-w-chat flex-1 flex-col justify-center px-4 py-12">
        <EmptyState
          icon={<ShieldCheck />}
          title={`Hello, ${firstName}`}
          headingLevel={1}
          description="Your role doesn't include working on cases."
          action={
            isAdmin(me.user) ? (
              <Link href="/admin" className="text-ui font-medium text-accent underline-offset-4 hover:underline">
                Go to Admin
              </Link>
            ) : undefined
          }
        />
      </div>
    );
  }

  const canWrite = canWriteCases(me.user);
  return (
    <div className="mx-auto flex w-full max-w-chat flex-1 flex-col justify-center gap-8 px-4 pt-8 pb-[12vh]">
      <div className="flex flex-col items-center gap-4 text-center">
        <NewAlertsPill />
        <h1 className="text-title text-fg sm:text-display">Hello, {firstName}</h1>
        <p className="text-body text-fg-muted">
          {canWrite
            ? "Tell me what changed in your supply chain. I'll check your systems and suggest a plan."
            : "Your role can follow existing cases. Open one from History."}
        </p>
      </div>
      <HomeComposer disabled={!canWrite} />
    </div>
  );
}
