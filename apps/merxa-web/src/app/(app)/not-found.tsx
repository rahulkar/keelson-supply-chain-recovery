import Link from "next/link";

import { EmptyState } from "@/components/ui/Card";

export default function NotFound() {
  return (
    <div className="mx-card">
      <EmptyState title="Record not found">
        It may have been removed or the link is wrong. <Link href="/change-batches">Back to Change Batches</Link>
      </EmptyState>
    </div>
  );
}
