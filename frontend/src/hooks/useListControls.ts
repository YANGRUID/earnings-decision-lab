import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

// ---------------------------------------------------------------------------
// Long-list controls (2026-09-02). Several pages render every row of a
// growing dataset in one table -- the Operations pipeline already shows
// 100+ rows and every forward-test day adds more. This hook gives any list
// the same four controls -- search, one facet filter, sort, paging -- with
// the state carried in the URL (?<key>_q=&<key>_f=&<key>_s=&<key>_p=&<key>_n=)
// so a filtered or "show all" view is bookmarkable, survives a reload and
// can be shared. Everything stays reachable: "All" is always a page size.
//
// Pure client-side: the row array is whatever the page already fetched.
// Server-side paging is a separate concern (see fetchAllPages in api/paging.ts
// for endpoints capped at 200 rows per call).
// ---------------------------------------------------------------------------

export type PageSize = number | "all";

export interface FacetSpec<T> {
  /** Column label shown before the chips, e.g. "Lifecycle". */
  label: string;
  /** Raw facet value for a row; null/undefined rows never match a chip. */
  getValue: (row: T) => string | null | undefined;
  /** Human label for a raw value (defaults to the raw value). */
  format?: (value: string) => string;
}

export interface SortSpec<T> {
  key: string;
  label: string;
  compare: (a: T, b: T) => number;
}

export interface ListControlsConfig<T> {
  rows: T[];
  /** Fields (or accessors) matched case-insensitively by the search box. */
  searchKeys?: ((row: T) => string | null | undefined)[];
  facet?: FacetSpec<T>;
  sorts?: SortSpec<T>[];
  /** Default sort key; when omitted the rows keep their incoming order. */
  defaultSort?: string;
  defaultPageSize?: number;
  pageSizes?: number[];
  /** Distinguishes several lists on one page in the URL, e.g. "pipe". */
  urlKey: string;
}

export interface FacetOption {
  value: string;
  label: string;
  count: number;
}

export interface ListControls<T> {
  visible: T[];
  total: number;
  filtered: number;
  query: string;
  setQuery: (q: string) => void;
  facet: FacetSpec<T> | null;
  facetValue: string | null;
  setFacetValue: (v: string | null) => void;
  facetOptions: FacetOption[];
  sorts: SortSpec<T>[];
  sortKey: string | null;
  setSortKey: (k: string | null) => void;
  page: number;
  pageCount: number;
  setPage: (p: number) => void;
  pageSize: PageSize;
  setPageSize: (s: PageSize) => void;
  pageSizes: number[];
  /** "1–25 of 312", "312 of 312", or "0 of 312" -- ready to render. */
  rangeLabel: string;
  /** True when a search or facet is narrowing the list. */
  isFiltered: boolean;
  clear: () => void;
}

const DEFAULT_PAGE_SIZES = [25, 50, 100];

export function useListControls<T>(config: ListControlsConfig<T>): ListControls<T> {
  const {
    rows,
    searchKeys = [],
    facet,
    sorts = [],
    defaultSort,
    defaultPageSize = 25,
    pageSizes = DEFAULT_PAGE_SIZES,
    urlKey,
  } = config;
  const [params, setParams] = useSearchParams();

  const qKey = `${urlKey}_q`;
  const fKey = `${urlKey}_f`;
  const sKey = `${urlKey}_s`;
  const pKey = `${urlKey}_p`;
  const nKey = `${urlKey}_n`;

  const query = params.get(qKey) ?? "";
  const facetValue = params.get(fKey);
  const sortKey = params.get(sKey) ?? defaultSort ?? null;
  const rawSize = params.get(nKey);
  const pageSize: PageSize =
    rawSize === "all" ? "all" : rawSize && Number(rawSize) > 0 ? Number(rawSize) : defaultPageSize;
  const requestedPage = Math.max(1, Number(params.get(pKey) ?? "1") || 1);

  const update = useCallback(
    (changes: Record<string, string | null>) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [k, v] of Object.entries(changes)) {
            if (v === null || v === "") next.delete(k);
            else next.set(k, v);
          }
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const facetOptions = useMemo<FacetOption[]>(() => {
    if (!facet) return [];
    const counts = new Map<string, number>();
    for (const row of rows) {
      const v = facet.getValue(row);
      if (v === null || v === undefined || v === "") continue;
      counts.set(v, (counts.get(v) ?? 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([value, count]) => ({ value, label: facet.format ? facet.format(value) : value, count }));
  }, [facet, rows]);

  const filteredRows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    let out = rows;
    if (needle && searchKeys.length > 0) {
      out = out.filter((row) =>
        searchKeys.some((get) => (get(row) ?? "").toString().toLowerCase().includes(needle)),
      );
    }
    if (facet && facetValue) {
      out = out.filter((row) => facet.getValue(row) === facetValue);
    }
    const sort = sortKey ? sorts.find((s) => s.key === sortKey) : undefined;
    if (sort) out = [...out].sort(sort.compare);
    return out;
  }, [rows, query, searchKeys, facet, facetValue, sortKey, sorts]);

  const filtered = filteredRows.length;
  const pageCount = pageSize === "all" ? 1 : Math.max(1, Math.ceil(filtered / pageSize));
  const page = Math.min(requestedPage, pageCount);
  const visible = useMemo(() => {
    if (pageSize === "all") return filteredRows;
    const start = (page - 1) * pageSize;
    return filteredRows.slice(start, start + pageSize);
  }, [filteredRows, page, pageSize]);

  const first = filtered === 0 ? 0 : pageSize === "all" ? 1 : (page - 1) * pageSize + 1;
  const last = filtered === 0 ? 0 : pageSize === "all" ? filtered : Math.min(filtered, page * pageSize);
  const rangeLabel =
    filtered === 0
      ? `0 of ${rows.length}`
      : first === 1 && last === filtered
        ? `${filtered} of ${rows.length}`
        : `${first}–${last} of ${filtered}${filtered !== rows.length ? ` (${rows.length} total)` : ""}`;

  return {
    visible,
    total: rows.length,
    filtered,
    query,
    setQuery: (q) => update({ [qKey]: q, [pKey]: null }),
    facet: facet ?? null,
    facetValue,
    setFacetValue: (v) => update({ [fKey]: v, [pKey]: null }),
    facetOptions,
    sorts,
    sortKey,
    setSortKey: (k) => update({ [sKey]: k === defaultSort ? null : k, [pKey]: null }),
    page,
    pageCount,
    setPage: (p) => update({ [pKey]: p <= 1 ? null : String(p) }),
    pageSize,
    setPageSize: (s) =>
      update({ [nKey]: s === "all" ? "all" : s === defaultPageSize ? null : String(s), [pKey]: null }),
    pageSizes,
    rangeLabel,
    isFiltered: Boolean(query.trim()) || Boolean(facet && facetValue),
    clear: () => update({ [qKey]: null, [fKey]: null, [pKey]: null }),
  };
}
