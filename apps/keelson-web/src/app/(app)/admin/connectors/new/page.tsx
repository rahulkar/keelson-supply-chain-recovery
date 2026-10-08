import type { Metadata } from "next";
import { ConnectorWizard } from "@/components/admin/ConnectorWizard";

export const metadata: Metadata = { title: "Add connector · Admin" };

export default function AdminNewConnectorRoute() {
  return <ConnectorWizard />;
}
