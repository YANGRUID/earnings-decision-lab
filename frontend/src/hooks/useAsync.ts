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
 * loading/error/data state.
 *
 * SPA navigation fix (V4-only reset, 2026-09-02): every run owns an
 * AbortController whose signal is handed to `fn`. When the component
 * unmounts or `deps` change, the in-flight request is ABORTED -- not just
 * ignored. The old hook only ignored stale results, so a page that fanned
 * out dozens of requests kept them alive after the user had navigated
 * away; the browser's per-origin connection limit (6 for HTTP/1.1) then
 * queued the next page's requests behind the abandoned ones, which is
 * exactly the "Loading…" stall that a full refresh (which drops every
 * connection) never showed. Callers that ignore the signal keep working;
 * callers that forward it to the API client get real cancellation.
 *
 * Keeps the previous `data` visible while a reload is in flight (only the
 * very first run starts from `data: null`), so callers that gate a
 * full-page loading state on `loading && !data` don't unmount their tree. */
export function useAsync<T>(fn: (signal: AbortSignal) => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [state, setState] = useState<InnerState<T>>({ data: null, loading: true, error: null });
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setState((prev) => ({ data: prev.data, loading: true, error: null }));
    fn(controller.signal)
      .then((data) => {
        if (!cancelled) setState({ data, loading: false, error: null });
      })
      .catch((err: unknown) => {
        if (cancelled || isAbort(err)) return;
        const message = err instanceof ApiError ? err.message : "Something went wrong.";
        setState((prev) => ({ data: prev.data, loading: false, error: message }));
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, version]);

  const reload = useCallback(() => setVersion((v) => v + 1), []);

  return { ...state, reload };
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException ? err.name === "AbortError" : (err as { name?: string } | null)?.name === "AbortError";
}
