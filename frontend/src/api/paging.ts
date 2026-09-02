// ---------------------------------------------------------------------------
// Server-side page walking (2026-09-02). Several read endpoints cap a call
// at 200 rows (limit <= 200, offset). Pages that used to ask for a single
// page of 200 silently hid everything older once the dataset grew past
// it. This walks the pages until a short page comes back, so the client
// holds the whole dataset and useListControls can search/page it locally.
// Bounded: at most `maxPages` calls (5,000 rows by default) -- past that
// size a list needs real server-side search, not a bigger download.
// ---------------------------------------------------------------------------

export async function fetchAllPages<T>(
  fetchPage: (offset: number, limit: number) => Promise<T[]>,
  { pageSize = 200, maxPages = 25 }: { pageSize?: number; maxPages?: number } = {},
): Promise<T[]> {
  const out: T[] = [];
  for (let i = 0; i < maxPages; i++) {
    const page = await fetchPage(i * pageSize, pageSize);
    out.push(...page);
    if (page.length < pageSize) break;
  }
  return out;
}
