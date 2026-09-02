/** Shared, short-lived cache for status-style GET endpoints that several
 * components on one screen request at the same moment (the dashboard
 * header, the sidebar-independent Operations page and the workspace all
 * read the operations summary / system status). One in-flight promise is
 * shared by every caller and the resolved value is reused for `ttlMs`, so
 * a navigation never issues duplicate status calls that would compete for
 * the browser's per-origin connection budget. Shared requests are never
 * aborted by one consumer: they are cheap, and another consumer may still
 * be waiting on the same promise. */

interface Entry<T> {
  promise: Promise<T>;
  resolvedAt: number | null;
}

const entries = new Map<string, Entry<unknown>>();

export function cachedStatus<T>(key: string, ttlMs: number, loader: () => Promise<T>): Promise<T> {
  const existing = entries.get(key) as Entry<T> | undefined;
  const now = Date.now();
  if (existing && (existing.resolvedAt === null || now - existing.resolvedAt < ttlMs)) {
    return existing.promise;
  }
  const entry: Entry<T> = { promise: undefined as unknown as Promise<T>, resolvedAt: null };
  entry.promise = loader().then(
    (value) => {
      entry.resolvedAt = Date.now();
      return value;
    },
    (err: unknown) => {
      // Never cache a failure: the next caller retries immediately.
      if (entries.get(key) === entry) entries.delete(key);
      throw err;
    },
  );
  entries.set(key, entry as Entry<unknown>);
  return entry.promise;
}

/** Drops a cached value so the next read goes to the backend (used by
 * pollers and after mutations). */
export function invalidateStatus(key?: string): void {
  if (key === undefined) entries.clear();
  else entries.delete(key);
}
