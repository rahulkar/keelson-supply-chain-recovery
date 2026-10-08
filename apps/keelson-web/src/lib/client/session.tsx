"use client";

/**
 * Client session context: the server-rendered `/api/me` (user, CSRF token, demo flag, brand)
 * and the browser API client primed with that CSRF token.
 */
import { createContext, useContext, useEffect, type ReactNode } from "react";
import { api, type ApiClient } from "../api/client";
import type { Me } from "../api/types";

type SessionValue = { me: Me; api: ApiClient };

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ me, client = api, children }: { me: Me; client?: ApiClient; children: ReactNode }) {
  useEffect(() => {
    client.setCsrfToken(me.csrf_token);
  }, [client, me.csrf_token]);
  return <SessionContext.Provider value={{ me, api: client }}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession() must be used inside <SessionProvider>");
  return value;
}

export function useApi(): ApiClient {
  return useSession().api;
}
