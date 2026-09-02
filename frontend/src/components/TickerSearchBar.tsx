import { useState } from "react";
import { useNavigate } from "react-router-dom";

function normalizeTicker(raw: string): string {
  return raw.trim().toUpperCase();
}

/** A compact ticker search box that navigates straight to that company's
 * real research workspace (/company/:ticker) -- the same destination the
 * full Search page's own hero form uses, just without the page-level
 * hero banner around it, so it can be dropped into any existing page
 * (Dashboard, Cross-Company Replay). */
export function TickerSearchBar({
  placeholder = "Search a ticker or company — e.g. NVDA, AAPL, COST…",
}: {
  placeholder?: string;
}) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const ticker = normalizeTicker(query);
    if (!ticker) return;
    navigate(`/company/${ticker}`);
  };

  return (
    <form className="search-hero-form" onSubmit={submit} style={{ marginBottom: 20 }}>
      <input
        className="search-hero-input"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder={placeholder}
        aria-label="Search ticker"
      />
      <button className="btn" type="submit">
        Research
      </button>
    </form>
  );
}
