import type { Metadata } from "next";

import { ApiClients } from "@/components/admin/ApiClients";
import { EmptyState } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { errorMessage } from "@/lib/http";
import { apiGet, getMe, load } from "@/lib/server-api";
import type { ApiKey, OAuthClientSeen } from "@/lib/types";

export const metadata: Metadata = { title: "API & MCP Clients" };

/** API keys (mint show-once, revoke) and OAuth clients seen on /mcp, /mcp-lite and REST. merxa-admin only. */
export default async function ApiClientsPage() {
  const me = await getMe();
  const isAdmin = me.user.roles.includes("merxa-admin");
  const header = (
    <PageHeader
      title="API & MCP Clients"
      subtitle="Merxa-issued API keys for integrations and the OAuth clients that call Merxa's MCP servers and REST API."
    />
  );
  if (!isAdmin) {
    return (
      <>
        {header}
        <div className="mx-card">
          <EmptyState title="Not available">Managing API keys and clients needs the Merxa Administrator role.</EmptyState>
        </div>
      </>
    );
  }
  const [keys, clients] = await Promise.all([
    load(apiGet<{ items: ApiKey[] }>("/v1/api-keys")),
    load(apiGet<{ items: OAuthClientSeen[] }>("/v1/oauth-clients")),
  ]);
  return (
    <>
      {header}
      {keys.ok ? (
        <ApiClients
          initialKeys={keys.data.items}
          clients={clients.ok ? clients.data.items : []}
          clientsError={clients.ok ? null : errorMessage(clients.error)}
        />
      ) : (
        <ProblemPanel error={keys.error} what="API keys" />
      )}
    </>
  );
}
