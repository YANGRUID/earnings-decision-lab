import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import type { EarningsThesis } from "../../types/api";

const SECTIONS: { key: keyof EarningsThesis; label: string }[] = [
  { key: "business_context", label: "Business context" },
  { key: "historical_earnings_pattern", label: "Historical earnings pattern" },
  { key: "guidance_trend", label: "Guidance trend" },
  { key: "key_risks", label: "Key risks" },
  { key: "market_setup", label: "Market setup" },
];

export function ThesisTab({ ticker }: { ticker: string }) {
  const [thesis, setThesis] = useState<EarningsThesis | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const generate = async () => {
    setLoading(true);
    setError(null);
    try {
      setThesis(await api.getEarningsThesis(ticker));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Thesis generation failed.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div className="card">
        <p className="text-sm text-muted" style={{ marginTop: 0 }}>
          A grounded, cited pre-earnings thesis for {ticker}, synthesized only from real filing
          excerpts, real historical earnings, real guidance comparisons, and the real market
          setup already shown elsewhere in this workspace. This is not investment advice, and no
          outcome is ever guaranteed — see the disclaimer below.
        </p>
        <button className="btn" onClick={generate} disabled={loading}>
          {loading ? "Generating…" : thesis ? "Regenerate thesis" : "Generate thesis"}
        </button>
        {error && <div className="notice" style={{ marginTop: 12 }}>{error}</div>}
      </div>

      {thesis && (
        <>
          {SECTIONS.map(({ key, label }) => (
            <div className="card" key={key}>
              <h2>{label}</h2>
              <p style={{ whiteSpace: "pre-wrap", margin: 0 }}>{thesis[key] as string}</p>
            </div>
          ))}

          {thesis.citations.length > 0 && (
            <div className="card">
              <h2>Citations</h2>
              <ul className="citation-list">
                {thesis.citations.map((c) => (
                  <li key={c.marker} className="citation-item">
                    <span className="citation-marker">{c.marker}</span>
                    {c.ticker} {c.filing_type} filed {c.filing_date}
                    {c.section ? `, ${c.section}` : ""} —{" "}
                    <a href={c.source_url} target="_blank" rel="noreferrer">
                      source
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="notice">
            {thesis.disclaimer} Generated {new Date(thesis.generated_at).toLocaleString()} by{" "}
            {thesis.model}. Ask <Link to={`/research?ticker=${ticker}`}>AI Research</Link> to dig
            deeper into any specific filing or claim above.
          </div>
        </>
      )}
    </div>
  );
}
