import { EmptyState } from "@keelson/ui";
import { SearchX } from "lucide-react";
import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto flex w-full max-w-chat flex-1 flex-col justify-center px-4 py-12">
      <EmptyState
        icon={<SearchX />}
        title="Not found"
        headingLevel={1}
        description="This page or case doesn't exist, or it belongs to another workspace."
        action={
          <Link href="/" className="text-ui font-medium text-accent underline-offset-4 hover:underline">
            Back to home
          </Link>
        }
      />
    </div>
  );
}
