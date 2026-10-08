"use client";

import { useEffect, useEffectEvent, useMemo, useState } from "react";
import { describeError, isApiError } from "../api/client";
import { useSession } from "../client/session";
import { AdminApi } from "./client";

/** The back-office client over the session's `ApiClient`. */
export function useAdminApi(): AdminApi {
  const { api } = useSession();
  return useMemo(() => new AdminApi(api), [api]);
}

export type Resource<T> = {
  data: T | undefined;
  /** Human message of the last failure (cleared by a successful load). */
  error: string | null;
  /** HTTP status of the last failure (`403`: not allowed, `404`: not found / no route yet). */
  errorStatus: number | null;
  /** A load for the current key is in flight (previous data stays visible meanwhile). */
  loading: boolean;
  reload: () => void;
  /** Replace the data locally (after a mutation answered with the new value). */
  setData: (update: (prev: T | undefined) => T | undefined) => void;
};

type Settled<T> = { token: string; data: T | undefined; error: string | null; errorStatus: number | null };

/**
 * Loads `load()` whenever `key` changes or `reload()` is called. Stale answers (an older key or
 * reload) are ignored; the last good data is kept while a reload is in flight.
 */
export function useResource<T>(load: () => Promise<T>, key: string): Resource<T> {
  const [epoch, setEpoch] = useState(0);
  const token = `${key}#${epoch}`;
  const [state, setState] = useState<Settled<T> | null>(null);
  const run = useEffectEvent(() => load());

  useEffect(() => {
    let cancelled = false;
    run().then(
      (data) => {
        if (!cancelled) setState({ token, data, error: null, errorStatus: null });
      },
      (err: unknown) => {
        if (!cancelled)
          setState((prev) => ({
            token,
            data: prev?.data,
            error: describeError(err),
            errorStatus: isApiError(err) ? err.status : null,
          }));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [token]);

  return {
    data: state?.data,
    error: state?.token === token ? state.error : null,
    errorStatus: state?.token === token ? state.errorStatus : null,
    loading: state?.token !== token,
    reload: () => setEpoch((n) => n + 1),
    setData: (update) => setState((prev) => (prev ? { ...prev, data: update(prev.data) } : prev)),
  };
}
