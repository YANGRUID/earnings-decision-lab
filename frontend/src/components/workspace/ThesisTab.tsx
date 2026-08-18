import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../../api/client";
import { useAsync } from "../../hooks/useAsync";
import { Markdown } from "../Markdown";
import { providerLabel } from "../../lib/format";
import type { AIThesisVersion } from "../../types/api";

const SECTIONS: { key: keyof AIThesisVersion; label: string }[] = [
  { key: "business_context", label: "Business context" },
  { key: "historical_earnings_pattern", label: "Historical earnings pattern" },
  { key: "guidance_trend", label: "Guidance trend" },
  { key: "key_risks", label: "Key risks" },
  { key: "market_setup", label: "Market setup" },
];

function formatVersionLabel(version: AIThesisVersion): string {
  return new Date(version.created_at).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function ThesisTab({ ticker }: { ticker: string }) {
  const history = useAsync(() => api.getThesisHistory(ticker), [ticker]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Default the active version to the latest one once history loads.
  useEffect(() => {
    if (history.data && history.data.length > 0 && activeId === null) {
      setActiveId(history.data[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [history.data]);

  const active = history.data?.find((v) => v.id === activeId) ?? null;
  const isLatest = history.data && history.data.length > 0 && activeId === history.data[0].id;

  const generate = async () => {
    setGenerating(true);
    setError(null);
    try {
      await api.getEarningsThesis(ticker);
      history.reload();
      setActiveId(null); // re-default to the new latest once history reloads
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Thesis generation failed.");
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div>
      <div className="card">
        <p className="text-sm text-muted" style={{ marginTop: 0 }}>
          A grounded, cited pre-earnings thesis for {ticker}, synthesized only from real filing
          excerpts, real historical earnings, real guidance comparisons, and the real market
          setup already shown elsewhere in this workspace. This is not investment advice, and no
          outcome is ever guaranteed — see the disclaimer below. Every generation is saved as a
          new version; nothing is ever overwritten.
        </p>
        <button className="btn" onClick={generate} disabled={generating}>
          {generating ? "Generating…" : "Generate new thesis"}
        </button>
        {error && <div className="notice" style={{ marginTop: 12 }}>{error}</div>}
      </div>

      {history.data && history.data.length > 1 && (
        <div className="card">
          <h2>Version history</h2>
          <ul className="history-list">
            {history.data.map((v, i) => (
              <li
                key={v.id}
                className={`history-item ${v.id === activeId ? "active" : ""}`}
                onClick={() => setActiveId(v.id)}
              >
                <span className="history-item-question">
                  {i === 0 ? "Latest — " : ""}
                  {formatVersionLabel(v)}
                  {v.is_stale && i === 0 ? " · newer data available" : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {active && (
        <>
          {isLatest && active.is_stale && (
            <div className="notice">
              Newer consensus or options data is available since this thesis was generated.
              The version below is kept exactly as generated — nothing here is silently
              rewritten.{" "}
              <button className="btn-secondary" onClick={generate} disabled={generating}>
                Generate updated thesis
              </button>
            </div>
          )}

          {SECTIONS.map(({ key, label }) => (
            <div className="card" key={key}>
              <h2>{label}</h2>
              <Markdown>{active[key] as string}</Markdown>
            </div>
          ))}

          {active.citations.length > 0 && (
            <div className="card">
              <h2>Sources</h2>
              {active.citations.map((c) => (
                <div key={c.marker} className="source-item">
                  <span className="citation-badge">{c.marker}</span>
                  <div>
                    <div className="source-title">
                      {c.ticker} · {c.filing_type} · filed {c.filing_date}
                      {c.section ? ` · ${c.section}` : ""}
                    </div>
                    <a
                      className="text-link text-sm"
                      href={c.source_url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      View source
                    </a>
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="notice">
            {active.disclaimer} Generated {new Date(active.created_at).toLocaleString()} by{" "}
            {providerLabel(active.provider)} ({active.model}). Ask{" "}
            <Link className="text-link" to={`/research?ticker=${ticker}`}>
              AI Research
            </Link>{" "}
            to dig deeper into any specific filing or claim above.
          </div>
        </>
      )}
    </div>
  );
}
