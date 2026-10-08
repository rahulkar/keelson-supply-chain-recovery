import type { Metadata } from "next";
import { WorkflowPage } from "@/components/admin/WorkflowPage";

export const metadata: Metadata = { title: "Workflow · Admin" };

export default function AdminWorkflowRoute() {
  return <WorkflowPage />;
}
