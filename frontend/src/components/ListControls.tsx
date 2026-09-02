import { useEffect, useState } from "react";
import type { ListControls, PageSize } from "../hooks/useListControls";

// ---------------------------------------------------------------------------
// Presentation for useListControls (2026-09-02): one toolbar above a list,
// one pager below it, and a sticky outline for pages with many sections.
// Deliberately plain: the same controls on every long list, so a user who
// has learned one page has learned them all.
// ---------------------------------------------------------------------------

export function ListToolbar<T>({
  controls,
  placeholder = "Search…",
  testId,
  right,
}: {
  controls: ListControls<T>;
  placeholder?: string;
  testId?: string;
  right?: React.ReactNode;
}) {
  const [draft, setDraft] = useState(controls.query);
  // Keep the box in sync when the URL changes from elsewhere (back button,
  // "clear"), and debounce typing so each keystroke doesn't rewrite the URL.
  useEffect(() => setDraft(controls.query), [controls.query]);
  useEffect(() => {
    if (draft === controls.query) return;
    const t = setTimeout(() => controls.setQuery(draft), 180);
    return () => clearTimeout(t);
  }, [draft, controls]);

  return (
    <div className="list-toolbar" data-testid={testId}>
      <input
        type="search"
        className="list-search"
        value={draft}
        placeholder={placeholder}
        aria-label={placeholder}
        onChange={(e) => setDraft(e.target.value)}
      />
      {controls.facet && controls.facetOptions.length > 1 && (
        <div className="chip-row" role="group" aria-label={controls.facet.label}>
          <button
            type="button"
            className={`chip ${controls.facetValue === null ? "active" : ""}`}
            onClick={() => controls.setFacetValue(null)}
          >
            All <span className="chip-count">{controls.total}</span>
          </button>
          {controls.facetOptions.map((o) => (
            <button
              key={o.value}
              type="button"
              className={`chip ${controls.facetValue === o.value ? "active" : ""}`}
              onClick={() => controls.setFacetValue(controls.facetValue === o.value ? null : o.value)}
              title={`${controls.facet?.label}: ${o.label}`}
            >
              {o.label} <span className="chip-count">{o.count}</span>
            </button>
          ))}
        </div>
      )}
      {controls.sorts.length > 0 && (
        <label className="list-select">
          <span className="text-faint text-sm">Sort</span>
          <select
            value={controls.sortKey ?? ""}
            onChange={(e) => controls.setSortKey(e.target.value || null)}
            aria-label="Sort"
          >
            {!controls.sorts.some((s) => s.key === controls.sortKey) && <option value="">Default</option>}
            {controls.sorts.map((s) => (
              <option key={s.key} value={s.key}>{s.label}</option>
            ))}
          </select>
        </label>
      )}
      <div className="list-toolbar-right">
        {right}
        <span className="list-range mono" data-testid={testId ? `${testId}-range` : undefined}>
          {controls.rangeLabel}
        </span>
        {/* Always laid out, only shown while filtering: toggling a filter must
            never shift the chips a user is about to click. */}
        <button
          type="button"
          className="text-link text-sm"
          onClick={controls.clear}
          style={{ visibility: controls.isFiltered ? "visible" : "hidden" }}
          aria-hidden={!controls.isFiltered}
        >
          Clear
        </button>
      </div>
    </div>
  );
}

function parseSize(v: string): PageSize {
  return v === "all" ? "all" : Number(v);
}

export function Pager<T>({ controls, testId }: { controls: ListControls<T>; testId?: string }) {
  if (controls.filtered === 0) return null;
  const showSizes = controls.total > Math.min(...controls.pageSizes);
  if (!showSizes && controls.pageCount <= 1) return null;
  return (
    <div className="pager" data-testid={testId}>
      {showSizes && (
        <label className="list-select">
          <span className="text-faint text-sm">Rows</span>
          <select
            value={controls.pageSize === "all" ? "all" : String(controls.pageSize)}
            onChange={(e) => controls.setPageSize(parseSize(e.target.value))}
            aria-label="Rows per page"
          >
            {controls.pageSizes.map((n) => (
              <option key={n} value={n}>{n}</option>
            ))}
            <option value="all">All ({controls.filtered})</option>
          </select>
        </label>
      )}
      {controls.pageCount > 1 && (
        <div className="pager-nav">
          <button
            type="button"
            className="btn-secondary btn-small"
            disabled={controls.page <= 1}
            onClick={() => controls.setPage(controls.page - 1)}
          >
            ← Prev
          </button>
          <span className="mono text-sm">
            Page {controls.page} of {controls.pageCount}
          </span>
          <button
            type="button"
            className="btn-secondary btn-small"
            disabled={controls.page >= controls.pageCount}
            onClick={() => controls.setPage(controls.page + 1)}
          >
            Next →
          </button>
        </div>
      )}
      {controls.pageSize !== "all" && controls.pageCount > 1 && (
        <button type="button" className="text-link text-sm" onClick={() => controls.setPageSize("all")}>
          Show all {controls.filtered}
        </button>
      )}
    </div>
  );
}

// A sticky row of jump links for pages made of many cards. Each target is
// a card with an id; the link scrolls it into view. Only the ids that
// actually exist on the page are rendered, so optional sections can be
// listed unconditionally.
export function PageOutline({ sections }: { sections: { id: string; label: string }[] }) {
  const [present, setPresent] = useState<Set<string>>(new Set());
  useEffect(() => {
    const check = () => setPresent(new Set(sections.filter((s) => document.getElementById(s.id)).map((s) => s.id)));
    check();
    const t = setTimeout(check, 400);
    return () => clearTimeout(t);
  }, [sections]);
  const items = sections.filter((s) => present.has(s.id));
  if (items.length < 2) return null;
  return (
    <nav className="page-outline" aria-label="On this page" data-testid="page-outline">
      <span className="text-faint text-sm">On this page</span>
      {items.map((s) => (
        <a
          key={s.id}
          href={`#${s.id}`}
          onClick={(e) => {
            e.preventDefault();
            document.getElementById(s.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
          }}
        >
          {s.label}
        </a>
      ))}
    </nav>
  );
}
