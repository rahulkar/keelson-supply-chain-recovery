import "server-only";

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import type { Me } from "../api/types";
import { loginUrl, safeReturnPath } from "../paths";
import { getMe } from "./engine";

/** Thrown when engine-api cannot answer; rendered by the route error boundary. */
export class EngineUnavailableError extends Error {
  constructor(detail: string) {
    super(`Keelson's engine is not reachable right now (${detail}).`);
    this.name = "EngineUnavailableError";
  }
}

/** Path of the current request (forwarded by `proxy.ts`). */
export async function currentPath(): Promise<string> {
  return safeReturnPath((await headers()).get("x-keelson-path"));
}

/** The signed-in user, or a redirect to the BFF sign-in (expired / revoked session). */
export async function requireMe(): Promise<Me> {
  const me = await getMe();
  if (me.kind === "ok") return me.data;
  if (me.kind === "unauthenticated") redirect(loginUrl(await currentPath()));
  throw new EngineUnavailableError(me.kind === "unavailable" ? me.detail : me.kind);
}
