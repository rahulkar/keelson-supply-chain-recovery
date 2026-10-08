import { EmptyState } from "@keelson/ui";
import { SearchX } from "lucide-react";
import Link from "next/link";

export default function RootNotFound() {
  return (
    <main className="mx-auto flex min-h-dvh w-full max-w-chat flex-col justify-center px-4 py-12">
      <EmptyState
        icon={<SearchX />}
        title="Not found"
        headingLevel={1}
        description="This page doesn't exist."
        action={
          <Link href="/" className="text-ui font-medium text-accent underline-offset-4 hover:underline">
            Back to home
          </Link>
        }
      />
    </main>
  );
}
