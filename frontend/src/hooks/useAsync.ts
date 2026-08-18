import { useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";

interface InnerState<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

export interface AsyncState<T> extends InnerState<T> {
  /** Re-runs `fn` immediately, e.g. after a mutation elsewhere changed what
   * it would return. Does not require `deps` to change. */
  reload: () => void;
}

/** Runs `fn` whenever `deps` changes (or `reload()` is called), tracking
 * loading/error/data state. Ignores results from a stale run if `deps`
 * change again, or `reload()` is called again, before it resolves.
 * Keeps the previous `data` visible while a reload is in flight (only the
 * very first run starts from `data: null`), so callers that gate a
 * full-page loading state on `loading && !data` don't unmount their tree —
 * and any transient local state further down (e.g. a "Connected" test
 * result) survives a `reload()` triggered by that same interaction. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [state, setState] = useState<InnerState<T>>({ data: null, loading: true, error: null });
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setState((prev) => ({ data: prev.data, loading: true, error: null }));
    fn()
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof ApiError ? err.message : "Something went wrong.";
        setState((prev) => ({ data: prev.data, loading: false, error: message }));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);

  return { ...state, reload };
}
